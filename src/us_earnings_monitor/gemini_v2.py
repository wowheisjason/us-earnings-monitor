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


class GeminiV2Client(GeminiClient):
    """Production Gemini client with explicit per-stage model routing.

    The legacy client enumerated and retried many models and used URL Context in
    analysis/audit. V2 pins a small model set, uses Google Search only for IR
    discovery, and analyzes the already-persisted evidence bundle without a
    second network dependency.
    """

    def _stage_models(self, stage: str) -> list[str]:
        if stage == "ir_research":
            primary = os.getenv("GEMINI_IR_MODEL", "gemini-3.6-flash")
            fallback = os.getenv("GEMINI_IR_FALLBACK_MODEL", "gemini-3.5-flash")
        else:
            primary = os.getenv("GEMINI_ANALYSIS_MODEL") or os.getenv("GEMINI_MODEL") or "gemini-3.5-flash-lite"
            fallback = os.getenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-flash-lite-latest")
        return list(dict.fromkeys(model.removeprefix("models/") for model in (primary, fallback) if model))

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if stage == "ir_research":
            tools = [{"google_search": {}}]
            timeout = int(os.getenv("GEMINI_IR_TIMEOUT_SECONDS", "60"))
            attempts = int(os.getenv("GEMINI_IR_ATTEMPTS", "2"))
        else:
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
                if tools:
                    body["tools"] = tools
                try:
                    response = self.session.post(url, params={"key": self.api_key}, json=body, timeout=timeout)
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = exc
                    status = exc.response.status_code if exc.response is not None else 0
                    if status not in _TRANSIENT:
                        LOG.warning("Gemini model=%s stage=%s rejected request HTTP %s", model, stage, status)
                        break
                    retry_after = 0.0
                    if exc.response is not None:
                        try:
                            retry_after = float(exc.response.headers.get("Retry-After", "0") or 0)
                        except ValueError:
                            retry_after = 0.0
                    LOG.warning("Gemini model=%s stage=%s transient HTTP %s attempt=%d", model, stage, status, attempt + 1)
                    if attempt + 1 < attempts:
                        time.sleep(min(5.0, max(1.0, retry_after)))
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
                value = json.loads(text)
                if tools:
                    value["_grounding"] = self._grounding_metadata(payload)
                return value

        if last_error:
            raise last_error
        raise RuntimeError(f"No configured Gemini model completed stage={stage}")

    def research_official_ir(self, company, event, now):
        documents, status = super().research_official_ir(company, event, now)
        for document in documents:
            document.metadata["retrieval_method"] = "google_search_grounding"
            document.metadata["retrieval_model"] = self.model
        status["model"] = self.model
        status["retrieval_method"] = "google_search_grounding"
        return documents, status
