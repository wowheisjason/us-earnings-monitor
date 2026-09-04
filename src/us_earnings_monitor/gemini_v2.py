from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import requests

from .gemini import GeminiClient

LOG = logging.getLogger("us_earnings_monitor")
_TRANSIENT = {429, 500, 502, 503, 504}
_NO_UNIT_CONVERSION = """
\nHARD OUTPUT RULE: Preserve monetary units exactly as USD millions/billions from the verified facts. Prefer forms such as $46.971B, $16.4B, or $986M. NEVER convert USD billions into Chinese 億/兆 units inside the model. Do not write 億美元, 兆美元, 億美金, or 兆美金. Never self-correct a number in prose with wording such as '應為'; output only the validated value.
"""


class GeminiSearchUnavailable(RuntimeError):
    """Search-specific provider outage/quota state; inference may still be healthy."""

    def __init__(self, message: str, *, category: str = "search_unavailable"):
        super().__init__(message)
        self.category = category


def _safe_error_detail(response: requests.Response | None) -> str:
    if response is None:
        return ""
    try:
        payload = response.json()
    except ValueError:
        return response.text[:600].replace("\n", " ")
    error = payload.get("error", payload) if isinstance(payload, dict) else payload
    if isinstance(error, dict):
        compact = {
            "code": error.get("code"),
            "status": error.get("status"),
            "message": error.get("message"),
            "details": error.get("details"),
        }
        return json.dumps(compact, ensure_ascii=False)[:1200]
    return str(error)[:600]


def _shared_search_quota_failure(status: int, detail: str) -> bool:
    """Detect account/project-level Search Grounding quota blocks.

    Interactions currently often omits quota IDs, so the stable signal is the
    generic billing/quota 429 returned identically across Gemini 3.x models.
    Treat it as shared Search capability failure instead of wasting fallback
    model calls inside the same run.
    """
    value = detail.casefold()
    return status == 429 and (
        "check your plan and billing details" in value
        or "exceeded your current quota" in value
        or "resource_exhausted" in value
    )


def _interaction_text(payload: dict[str, Any]) -> str:
    chunks: list[str] = []
    for step in payload.get("steps", []) or []:
        if step.get("type") != "model_output":
            continue
        for item in step.get("content", []) or []:
            if item.get("type") == "text" and item.get("text"):
                chunks.append(str(item["text"]))
    return "".join(chunks).strip()


def _interaction_grounding(payload: dict[str, Any]) -> dict[str, Any]:
    queries: list[str] = []
    urls: list[str] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                if key.casefold() in {"url", "uri"} and isinstance(item, str) and item.startswith("http"):
                    urls.append(item)
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for step in payload.get("steps", []) or []:
        if step.get("type") == "google_search_call":
            for query in (step.get("arguments", {}) or {}).get("queries", []) or []:
                queries.append(str(query))
        elif step.get("type") == "google_search_result":
            walk(step.get("result"))
    return {"search_queries": list(dict.fromkeys(queries)), "retrieved_urls": list(dict.fromkeys(urls))}


class GeminiV2Client(GeminiClient):
    """Production Gemini client with explicit stage routing and modern IR search."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._search_circuit_reason: str | None = None

    def _stage_models(self, stage: str) -> list[str]:
        if stage == "ir_research":
            configured = (
                os.getenv("GEMINI_IR_MODEL", "gemini-3.6-flash"),
                os.getenv("GEMINI_IR_FALLBACK_MODEL", "gemini-3.5-flash"),
            )
        else:
            # Keep low-cost Flash-Lite as the normal path. V3 can make several
            # bounded map/reduce calls for long transcripts, which amplifies the
            # chance that a temporary Lite-capacity spike aborts an otherwise
            # healthy event. Only after both Lite endpoints are unavailable do
            # we escalate to a full Flash model.
            configured = (
                os.getenv("GEMINI_ANALYSIS_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite",
                os.getenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-flash-lite-latest"),
                os.getenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash"),
            )
        return list(dict.fromkeys(model.removeprefix("models/") for model in configured if model))

    def _interaction_json(self, prompt: str, stage: str) -> dict[str, Any]:
        if self._search_circuit_reason:
            raise GeminiSearchUnavailable(self._search_circuit_reason, category="search_quota_blocked")

        timeout = int(os.getenv("GEMINI_IR_TIMEOUT_SECONDS", "60"))
        attempts = int(os.getenv("GEMINI_IR_ATTEMPTS", "2"))
        last_error: Exception | None = None
        url = "https://generativelanguage.googleapis.com/v1beta/interactions"

        for model in self._stage_models(stage):
            for attempt in range(max(1, attempts)):
                body = {
                    "model": model,
                    "input": prompt,
                    "tools": [{"type": "google_search"}],
                    "response_format": {"type": "text", "mime_type": "application/json"},
                }
                try:
                    response = self.session.post(url, params={"key": self.api_key}, json=body, timeout=timeout)
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = exc
                    provider_response = exc.response
                    status = provider_response.status_code if provider_response is not None else 0
                    detail = _safe_error_detail(provider_response)
                    LOG.warning("Gemini Interactions model=%s stage=%s HTTP=%s attempt=%d detail=%s",
                                model, stage, status, attempt + 1, detail)
                    if _shared_search_quota_failure(status, detail):
                        self._search_circuit_reason = f"Gemini Search Grounding quota blocked: {detail[:500]}"
                        raise GeminiSearchUnavailable(self._search_circuit_reason, category="search_quota_blocked") from exc
                    if status not in _TRANSIENT:
                        break
                    if attempt + 1 < attempts:
                        time.sleep(1)
                    continue
                except requests.RequestException as exc:
                    last_error = exc
                    LOG.warning("Gemini Interactions model=%s stage=%s network failure attempt=%d: %s",
                                model, stage, attempt + 1, exc)
                    if attempt + 1 < attempts:
                        time.sleep(1)
                    continue

                payload = response.json()
                self.model = model
                text = _interaction_text(payload)
                if not text:
                    last_error = RuntimeError(f"Gemini Interactions returned no model_output for {stage}")
                    LOG.warning("Gemini Interactions model=%s stage=%s status=%s with no model text",
                                model, stage, payload.get("status"))
                    continue
                value = json.loads(text)
                value["_grounding"] = _interaction_grounding(payload)
                value["_interaction_id"] = payload.get("id")
                return value

        if last_error:
            raise last_error
        raise RuntimeError(f"No configured Gemini Interactions model completed stage={stage}")

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if stage == "ir_research":
            return self._interaction_json(prompt, stage)

        tools = None
        timeout = int(os.getenv("GEMINI_ANALYSIS_TIMEOUT_SECONDS", "90"))
        attempts = int(os.getenv("GEMINI_ANALYSIS_ATTEMPTS", "2"))
        if stage in {"analyst", "auditor", "revision"}:
            prompt += _NO_UNIT_CONVERSION

        last_error: Exception | None = None
        for model in self._stage_models(stage):
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for attempt in range(max(1, attempts)):
                body: dict[str, Any] = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
                }
                try:
                    response = self.session.post(url, params={"key": self.api_key}, json=body, timeout=timeout)
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = exc
                    provider_response = exc.response
                    status = provider_response.status_code if provider_response is not None else 0
                    detail = _safe_error_detail(provider_response)
                    if status not in _TRANSIENT:
                        LOG.warning("Gemini model=%s stage=%s rejected HTTP=%s detail=%s", model, stage, status, detail)
                        break
                    LOG.warning("Gemini model=%s stage=%s transient HTTP=%s attempt=%d detail=%s",
                                model, stage, status, attempt + 1, detail)
                    if attempt + 1 < attempts:
                        time.sleep(1)
                    continue
                except requests.RequestException as exc:
                    last_error = exc
                    LOG.warning("Gemini model=%s stage=%s network failure attempt=%d: %s", model, stage, attempt + 1, exc)
                    if attempt + 1 < attempts:
                        time.sleep(1)
                    continue

                payload = response.json()
                self.model = model
                self._record_usage(stage, model, payload)
                text = self._candidate_text(payload)
                if not text:
                    last_error = RuntimeError(f"Gemini returned empty JSON text for {stage}")
                    continue
                return json.loads(text)

        if last_error:
            raise last_error
        raise RuntimeError(f"No configured Gemini model completed stage={stage}")

    def research_official_ir(self, company, event, now):
        documents, status = super().research_official_ir(company, event, now)
        for document in documents:
            document.metadata["retrieval_method"] = "gemini_interactions_google_search"
            document.metadata["retrieval_model"] = self.model
        status["model"] = self.model
        status["retrieval_method"] = "gemini_interactions_google_search"
        return documents, status
