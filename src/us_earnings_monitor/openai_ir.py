from __future__ import annotations

import hashlib
import json
import logging
import os
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

from .models import Company, Disclosure, EarningsEvent

LOG = logging.getLogger("us_earnings_monitor")
_KIND_LABELS = {
    "earnings_release": "Earnings Release",
    "financial_tables": "Financial Tables",
    "performance_review": "Performance Review",
    "presentation": "Earnings Presentation",
    "prepared_remarks": "Prepared Remarks",
    "transcript": "Transcript",
    "qa": "Q&A",
    "supplement": "Supplement",
}


def _hosts(company: Company) -> list[str]:
    values = [company.ir_index_url, *company.ir_additional_urls, *company.official_domains]
    hosts: list[str] = []
    for value in values:
        if not value:
            continue
        candidate = value if "://" in value else f"https://{value.lstrip('*.')}"
        host = (urlparse(candidate).hostname or "").casefold()
        if host and host not in hosts:
            hosts.append(host)
    return hosts


def _official(company: Company, url: str) -> bool:
    host = (urlparse(url).hostname or "").casefold()
    return bool(host) and any(host == allowed or host.endswith("." + allowed) for allowed in _hosts(company))


def _response_text(payload: dict[str, Any]) -> str:
    if payload.get("output_text"):
        return str(payload["output_text"]).strip()
    chunks: list[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "message":
            continue
        for content in item.get("content", []) or []:
            if content.get("type") == "output_text" and content.get("text"):
                chunks.append(str(content["text"]))
    return "".join(chunks).strip()


def _sources(payload: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for item in payload.get("output", []) or []:
        if item.get("type") != "web_search_call":
            continue
        action = item.get("action", {}) or {}
        for source in action.get("sources", []) or []:
            url = source.get("url") if isinstance(source, dict) else None
            if url:
                urls.append(str(url))
    return list(dict.fromkeys(urls))


class OpenAIWebIrClient:
    """Optional issuer-domain-only web-search fallback.

    It is intentionally retrieval-only. Gemini (or another configured analysis
    provider) still performs fact extraction and investment analysis after the
    official evidence bundle is persisted.
    """

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.getenv("OPENAI_API_KEY")
        self.model = os.getenv("OPENAI_IR_MODEL", "gpt-5.6-luna")
        self.session = session or requests.Session()
        if not self.api_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAI IR fallback")

    def research_official_ir(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict[str, Any]]:
        hosts = _hosts(company)
        if not hosts:
            return [], {"research_complete": False, "provider": "openai_web_search", "reason": "no_official_domains"}
        period = f"FY{event.fiscal_year} {event.quarter}" if event.fiscal_year and event.quarter else event.event_id
        prompt = f"""Find the exact official investor-relations materials for this earnings event and return JSON only.
Issuer: {company.name} ({company.ticker})
Event: {event.event_id}
Fiscal period: {period}
First detected: {event.first_seen_at}

Search ONLY the issuer domains supplied by the web-search tool. Collect every official item available for this exact event: earnings release, financial tables, performance review/shareholder letter, earnings presentation/supplement, prepared remarks, official transcript, and official Q&A including Q&A embedded in a transcript.

For each source return a dense, source-backed evidence_text preserving reported numbers, complete guidance ranges, management wording, and material analyst Q&A. Do not add investment conclusions. Do not return SEC, news, aggregators, or third-party transcript copies.

Schema:
{{
  "call": {{"scheduled_at": string|null, "status": "scheduled"|"completed"|"unknown"}},
  "transcript_status": "FOUND"|"EXPECTED_NOT_YET_AVAILABLE"|"CONFIRMED_NOT_PUBLISHED"|"UNKNOWN",
  "sources": [{{"kind":"earnings_release"|"financial_tables"|"performance_review"|"presentation"|"prepared_remarks"|"transcript"|"qa"|"supplement","title":string,"url":string,"published_at":string|null,"evidence_text":string,"structured_facts":[object]}}],
  "research_notes": [string]
}}
"""
        body = {
            "model": self.model,
            "reasoning": {"effort": "low"},
            "input": prompt,
            "tools": [{"type": "web_search", "filters": {"allowed_domains": hosts}}],
            "tool_choice": "auto",
            "include": ["web_search_call.action.sources"],
            "max_tool_calls": int(os.getenv("OPENAI_IR_MAX_TOOL_CALLS", "3")),
            "store": False,
        }
        timeout = int(os.getenv("OPENAI_IR_TIMEOUT_SECONDS", "60"))
        response = self.session.post(
            "https://api.openai.com/v1/responses",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json=body,
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        text = _response_text(payload)
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        result = json.loads(text)
        documents: list[Disclosure] = []
        rejected: list[str] = []
        for source in result.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            kind = str(source.get("kind", "")).casefold()
            url = str(source.get("url", "")).strip()
            evidence_text = str(source.get("evidence_text", "")).strip()
            if kind not in _KIND_LABELS or not url or not evidence_text:
                continue
            if not _official(company, url):
                rejected.append(url)
                continue
            content_hash = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()[:12]
            source_id = hashlib.sha256(f"{url}|{content_hash}".encode("utf-8")).hexdigest()[:24]
            raw_title = str(source.get("title", "")).strip()
            label = _KIND_LABELS[kind]
            title = raw_title or label
            documents.append(Disclosure(
                source="openai_web_ir",
                source_id=source_id,
                ticker=company.ticker,
                title=title,
                published_at=str(source.get("published_at") or now.isoformat(timespec="seconds")),
                url=url,
                document_url=url,
                fiscal_year=event.fiscal_year,
                quarter=event.quarter,
                period_end=event.period_end,
                document_kind=kind,
                metadata={
                    "service": "openai_web_ir",
                    "retrieval_method": "openai_responses_web_search",
                    "retrieval_model": self.model,
                    "format": "grounded_text",
                    "grounded_evidence": evidence_text,
                    "structured_facts": source.get("structured_facts", []) or [],
                    "retrieved_at": now.isoformat(timespec="seconds"),
                    "content_hash": content_hash,
                },
            ))
        status = {
            "research_complete": bool(documents),
            "document_count": len(documents),
            "provider": "openai_web_search",
            "model": self.model,
            "transcript_status": result.get("transcript_status", "UNKNOWN"),
            "call": result.get("call", {}) or {},
            "research_notes": result.get("research_notes", []) or [],
            "grounding": {"retrieved_urls": _sources(payload)},
            "rejected_unofficial_urls": rejected,
        }
        LOG.info("%s OpenAI IR fallback: docs=%d transcript=%s", event.event_id, len(documents), status["transcript_status"])
        return documents, status
