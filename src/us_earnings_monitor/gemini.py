from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlparse

import requests

from .models import Company, Disclosure, EarningsEvent, Evidence

EVIDENCE_TOTAL_MAX_CHARS = 48_000
LOG = logging.getLogger("us_earnings_monitor")
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}
_GROUNDED_IR_KINDS = {
    "earnings_release": "Earnings Release",
    "financial_tables": "Financial Tables",
    "performance_review": "Performance Review",
    "presentation": "Earnings Presentation",
    "prepared_remarks": "Prepared Remarks",
    "transcript": "Transcript",
    "qa": "Q&A",
    "supplement": "Supplement",
}


class GeminiClient:
    """REST client for grounded IR research, evidence extraction, analysis, and audit."""

    def __init__(self, api_key: str | None = None, model: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.explicit_model = model or os.getenv("GEMINI_MODEL")
        self.model = self.explicit_model
        self._models: list[str] | None = None
        self.usage = {"prompt_tokens": 0, "output_tokens": 0, "thought_tokens": 0, "total_tokens": 0, "calls": 0}
        self.session = session or requests.Session()
        if not self.api_key:
            raise RuntimeError("GEMINI_API_KEY is required for a non-dry-run analysis")

    @staticmethod
    def _model_rank(name: str) -> tuple:
        lower = name.casefold()
        excluded = any(word in lower for word in ("image", "live", "tts", "transcribe", "embedding"))
        flash = "flash" in lower and "flash-lite" not in lower
        flash_lite = "flash-lite" in lower
        pro = "pro" in lower
        stable = "preview" not in lower and "exp" not in lower
        versions = tuple(int(value) for value in re.findall(r"\d+", lower)[:2])
        versions = versions + (0,) * (2 - len(versions))
        return (not excluded, flash, stable, flash_lite, pro, versions)

    def _available_models(self) -> list[str]:
        if self.explicit_model:
            return [self.explicit_model.removeprefix("models/")]
        if self._models is not None:
            return self._models
        response = self.session.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            params={"key": self.api_key, "pageSize": 1000}, timeout=30,
        )
        response.raise_for_status()
        candidates = []
        for item in response.json().get("models", []):
            name = str(item.get("name", "")).removeprefix("models/")
            methods = item.get("supportedGenerationMethods", [])
            if name.startswith("gemini-") and "generateContent" in methods:
                candidates.append(name)
        if not candidates:
            raise RuntimeError("No Gemini model supporting generateContent is available for this API key")
        self._models = sorted(candidates, key=self._model_rank, reverse=True)
        return self._models

    def _resolve_model(self) -> str:
        return self._available_models()[0]

    def _record_usage(self, stage: str, model: str, payload: dict) -> None:
        usage = payload.get("usageMetadata", {})
        prompt = int(usage.get("promptTokenCount", 0) or 0)
        output = int(usage.get("candidatesTokenCount", 0) or 0)
        thoughts = int(usage.get("thoughtsTokenCount", 0) or 0)
        total = int(usage.get("totalTokenCount", prompt + output + thoughts) or 0)
        self.usage["prompt_tokens"] += prompt
        self.usage["output_tokens"] += output
        self.usage["thought_tokens"] += thoughts
        self.usage["total_tokens"] += total
        self.usage["calls"] += 1
        LOG.info("Gemini usage stage=%s model=%s prompt=%d output=%d thoughts=%d total=%d cumulative=%d",
                 stage, model, prompt, output, thoughts, total, self.usage["total_tokens"])

    @staticmethod
    def _candidate_text(payload: dict) -> str:
        parts = payload.get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(str(part.get("text", "")) for part in parts if part.get("text"))
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```$", "", text)
        return text

    @staticmethod
    def _grounding_metadata(payload: dict) -> dict[str, Any]:
        candidate = payload.get("candidates", [{}])[0]
        grounding = candidate.get("groundingMetadata", {}) or {}
        url_context = candidate.get("urlContextMetadata", {}) or candidate.get("url_context_metadata", {}) or {}
        urls: list[str] = []
        for chunk in grounding.get("groundingChunks", []) or []:
            web = chunk.get("web", {}) if isinstance(chunk, dict) else {}
            uri = web.get("uri")
            if uri:
                urls.append(str(uri))
        for item in url_context.get("urlMetadata", []) or url_context.get("url_metadata", []) or []:
            uri = item.get("retrievedUrl") or item.get("retrieved_url")
            if uri:
                urls.append(str(uri))
        return {
            "search_queries": grounding.get("webSearchQueries", []) or grounding.get("web_search_queries", []),
            "retrieved_urls": list(dict.fromkeys(urls)),
        }

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        last_error: Exception | None = None
        for model in self._available_models()[:5]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for attempt in range(2):
                body: dict[str, Any] = {
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
                }
                if tools:
                    body["tools"] = tools
                response = self.session.post(url, params={"key": self.api_key}, json=body, timeout=120)
                try:
                    response.raise_for_status()
                except requests.HTTPError as exc:
                    last_error = exc
                    if response.status_code not in TRANSIENT_STATUS_CODES:
                        break
                    LOG.warning("Gemini %s temporarily unavailable for %s (HTTP %d, attempt %d)",
                                model, stage, response.status_code, attempt + 1)
                    if attempt == 0:
                        time.sleep(2)
                    continue
                payload = response.json()
                self.model = model
                if self._models:
                    self._models = [model] + [item for item in self._models if item != model]
                self._record_usage(stage, model, payload)
                value = json.loads(self._candidate_text(payload))
                if tools:
                    value["_grounding"] = self._grounding_metadata(payload)
                return value
        if last_error:
            raise last_error
        raise RuntimeError("No available Gemini model completed the request")

    @staticmethod
    def _official_hosts(company: Company) -> set[str]:
        hosts: set[str] = set()
        values = [company.ir_index_url, *company.ir_additional_urls, *company.official_domains]
        for value in values:
            if not value:
                continue
            candidate = value if "://" in value else f"https://{value.lstrip('*.')}"
            host = (urlparse(candidate).hostname or "").casefold()
            if host:
                hosts.add(host)
        return hosts

    @classmethod
    def _official_url(cls, company: Company, url: str) -> bool:
        host = (urlparse(url).hostname or "").casefold()
        return bool(host) and any(host == allowed or host.endswith("." + allowed)
                                  for allowed in cls._official_hosts(company))

    def research_official_ir(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict[str, Any]]:
        """Use Google Search + URL Context to collect official issuer IR evidence.

        Search discovers heterogeneous issuer pages; URL Context reads the official
        HTML/PDF assets. Python still enforces the issuer allowlist before evidence
        can enter the event, so search results are never trusted by hostname alone.
        """
        period = f"FY{event.fiscal_year} {event.quarter}" if event.fiscal_year and event.quarter else event.event_id
        official_home = company.ir_index_url or ""
        prompt = f"""You are an evidence-retrieval agent for an automated US earnings monitor. Return JSON only.

Event: {event.event_id}
Issuer: {company.name} ({company.ticker})
Fiscal period: {period}
SEC event first detected: {event.first_seen_at}
Official IR home: {official_home}

Use Google Search to find THIS EXACT earnings event, then use URL Context to read the relevant official issuer pages/documents. Search broadly enough to find event-detail pages and assets, but RETURN ONLY issuer-official IR sources. Do not use SEC, news, aggregators, Seeking Alpha, Motley Fool, StockTitan, transcripts copied by third parties, or search-result snippets as evidence.

Collect every official item available for the event:
- earnings release / press release
- financial tables
- performance review / shareholder letter
- earnings presentation / supplemental slides
- prepared remarks
- official transcript
- official Q&A, including Q&A embedded in a transcript

For each source, evidence_text must contain a dense source-backed extract suitable for a second analyst model. Preserve reported numbers, guidance ranges, management wording, and all material analyst Q&A on demand, pricing, supply, margins, guidance, customers, inventory, competition, capex and risk. Do not add your own investment conclusions. For transcript/Q&A, include analyst and management speaker names when available and enough context to preserve the meaning of answers.

Also identify the earnings-call scheduled time/status if the official source states it. Do not claim that a transcript is officially not published unless an official source explicitly establishes that fact.

Schema:
{{
  "event_id": "{event.event_id}",
  "call": {{"scheduled_at": string|null, "status": "scheduled"|"completed"|"unknown"}},
  "transcript_status": "FOUND"|"EXPECTED_NOT_YET_AVAILABLE"|"CONFIRMED_NOT_PUBLISHED"|"UNKNOWN",
  "sources": [
    {{
      "kind": "earnings_release"|"financial_tables"|"performance_review"|"presentation"|"prepared_remarks"|"transcript"|"qa"|"supplement",
      "title": string,
      "url": string,
      "published_at": string|null,
      "evidence_text": string,
      "structured_facts": [object]
    }}
  ],
  "research_notes": [string]
}}
"""
        result = self._json(prompt, "ir_research", tools=[{"url_context": {}}, {"google_search": {}}])
        grounding = result.pop("_grounding", {})
        documents: list[Disclosure] = []
        rejected_urls: list[str] = []
        for source in result.get("sources", []) or []:
            if not isinstance(source, dict):
                continue
            kind = str(source.get("kind", "")).casefold()
            url = str(source.get("url", "")).strip()
            evidence_text = str(source.get("evidence_text", "")).strip()
            if kind not in _GROUNDED_IR_KINDS or not url or not evidence_text:
                continue
            if not self._official_url(company, url):
                rejected_urls.append(url)
                continue
            content_hash = hashlib.sha256(evidence_text.encode("utf-8")).hexdigest()[:12]
            source_id = hashlib.sha256(f"{url}|{content_hash}".encode("utf-8")).hexdigest()[:24]
            raw_title = str(source.get("title", "")).strip()
            label = _GROUNDED_IR_KINDS[kind]
            title = f"{label} — {raw_title}" if raw_title and label.casefold() not in raw_title.casefold() else (raw_title or label)
            published_at = str(source.get("published_at") or now.isoformat(timespec="seconds"))
            documents.append(Disclosure(
                source="gemini_grounded_ir",
                source_id=source_id,
                ticker=company.ticker,
                title=title,
                published_at=published_at,
                url=url,
                document_url=url,
                fiscal_year=event.fiscal_year,
                quarter=event.quarter,
                period_end=event.period_end,
                document_kind=kind,
                metadata={
                    "service": "gemini_grounded_ir",
                    "retrieval_method": "google_search+url_context",
                    "format": "grounded_text",
                    "grounded_evidence": evidence_text,
                    "structured_facts": source.get("structured_facts", []) or [],
                    "retrieved_at": now.isoformat(timespec="seconds"),
                    "content_hash": content_hash,
                },
            ))
        status = {
            "research_complete": True,
            "document_count": len(documents),
            "transcript_status": result.get("transcript_status", "UNKNOWN"),
            "call": result.get("call", {}) or {},
            "research_notes": result.get("research_notes", []) or [],
            "grounding": grounding,
            "rejected_unofficial_urls": rejected_urls,
        }
        LOG.info("%s grounded IR research: docs=%d transcript=%s rejected_unofficial=%d",
                 event.event_id, len(documents), status["transcript_status"], len(rejected_urls))
        return documents, status

    @staticmethod
    def _evidence(event: EarningsEvent, evidence: list[Evidence]) -> str:
        items = []
        per_document = EVIDENCE_TOTAL_MAX_CHARS // max(1, len(evidence))
        for item in evidence:
            items.append({"document_key": item.document_key, "title": item.title, "url": item.url,
                          "structured_facts": item.structured_facts, "excerpt": item.text[:per_document]})
        return json.dumps({"event_id": event.event_id, "documents": items}, ensure_ascii=False)

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        return self._json("""You are a strict evidence extractor for US-listed-company earnings. Return JSON only.
Use ONLY the official evidence below. Do not estimate, infer, invent, or silently normalize a company-defined metric. Missing values must be null.
Every extracted item must include evidence.document_key and a short supporting quote or structured concept.
Use URL Context on the supplied official evidence URLs when available to validate source-backed excerpts against the original document.

Rules:
1. Preserve GAAP, non-GAAP, adjusted, and company-defined metric labels exactly. Never call Adjusted FCF simply FCF.
2. Guidance must preserve the COMPLETE range when present: low, midpoint, high, unit, period, comparison, previous guidance, and change. A midpoint alone is not the full guidance if a range exists.
3. Cash flow must distinguish operating_cash_flow, standard_fcf, adjusted_fcf, capex, and other. If an adjusted metric has an official reconciliation, extract the adjustment components.
4. Extract reported current-period value and prior-period/comparison value separately when both are explicitly present; do not calculate YoY yourself.
5. Q&A may be embedded inside a Transcript. Extract it even when there is no separate Q&A document. Categorize each material Q&A as demand, pricing, supply, margin, guidance, customer, inventory, competition, capex, risk, or other.
6. Market consensus is NOT company guidance. market_consensus must be [] unless an explicit external consensus figure is actually included in the evidence.
7. Separate objective facts from management statements. Do not convert management opinion into an objective fact.

Schema:
{
  event_id,
  company_name,
  facts:[{metric,value,unit,period,comparison_value,comparison_period,reported_change,evidence:{document_key,quote}}],
  guidance:[{metric,period,low,midpoint,high,unit,reported_yoy,previous_low,previous_midpoint,previous_high,change,evidence:{document_key,quote}}],
  segments:[{name,metric,value,unit,period,comparison_value,reported_change,evidence:{document_key,quote}}],
  cash_flow_and_capex:[{metric,metric_type,value,unit,period,reported_change,reconciliation:[{item,value,unit}],evidence:{document_key,quote}}],
  industry_signals:[...],
  management_comments:[{topic,statement,evidence:{document_key,quote}}],
  qa:[{category,question_summary,answer_summary,analyst,management_speaker,evidence:{document_key,quote}}],
  market_consensus:[],
  unknowns:[...]
}
Evidence:\n""" + self._evidence(event, evidence), "facts", tools=[{"url_context": {}}])

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是專業機構投資人。請以台灣繁體中文撰寫並只回傳 JSON；專業術語與正式名稱保留英文原文。
只能使用 structured facts 與 collection_status。客觀事實、管理層說法、投資判讀必須分開，不提供目標價或買賣建議。

硬規則：
1. Consensus 與公司 guidance 分開。若沒有外部 consensus provider/data，固定寫「未納入外部市場共識，因此不判定 Beat/Miss」，不得寫成「官方資料未提供市場共識」。
2. Guidance 若 facts 同時有 low/midpoint/high，報告必須呈現完整 range，並可補 midpoint；禁止只把 midpoint 當完整 guidance。
3. GAAP/non-GAAP、standard FCF/Adjusted FCF 必須清楚標示。若 Adjusted FCF 與普通 FCF/OCF 差距大且 reconciliation 已提供，必須說明主要調整項，避免把 Adjusted FCF 當基礎現金轉換能力。
4. collection_status.transcript_status=FOUND：使用官方 Transcript/Q&A；=NOT_FOUND_AFTER_RETRY：只能寫「截至本次自動蒐集截止，未取得官方 Transcript/Q&A」，不得斷言公司沒有發布；=CONFIRMED_NOT_PUBLISHED 才可寫官方未發布；=EXPECTED_NOT_YET_AVAILABLE 時不得生成正式報告（正常情況會由 publish gate 阻擋）。
5. Q&A 若存在，優先整理對投資最有價值的 demand/pricing/supply/margin/guidance/customer/inventory/competition/capex/risk 訊號，而不是只摘要 prepared remarks。
6. 不得新增官方證據沒有的數字、因果關係、產業結論或外部資料。

Schema: {facts:[string], investor_interpretation:[string], investment_implications:[string], risks_and_unknowns:[string], confidence:0-100, telegram_draft:string}.
telegram_draft 必須少於 3200 字，嚴格使用以下順序；資料不足要用精確狀態描述，不能用籠統「官方資料未提供／無法確認」掩蓋 discovery 狀態。不要輸出資料來源段落，程式會附官方連結。
金額使用台灣常用兆、億、萬，可對已驗證美元數字做精確單位換算。

{公司/股票代碼} {FY季度}

💡 核心摘要: ...

📌 官方重點:
• ...

📢 財測與共識:
• 狀態: ...
• 共識: ...
• 調整: ...

📈 關鍵指標:
指標       | 本期       | YoY
-------------------------------
...

🏢 業務部門:
• ...
└ ...

🔮 未來展望:
• ...

🎙️ 法說 Q&A:
❓ ...
💬 ...

Structured facts:\n""" + json.dumps({"event_id": event.event_id, "facts": facts}, ensure_ascii=False), "analyst")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是該企業所屬產業的資深產業專家與 reject-oriented evidence auditor。請以台灣繁體中文撰寫並只回傳 JSON。
你的目標不是幫第一次分析找理由通過，而是主動找出足以拒絕發布的問題。逐項把數字、因果、財測、Q&A 與措辭對回 ORIGINAL official evidence；可使用 URL Context 重新檢查輸入中的官方 URL。

以下任何 critical issue 存在時 pass 必須為 false：
- unsupported number/claim 或證據對不上。
- guidance 有 range 卻只呈現 midpoint，或 midpoint 被描述成完整 guidance。
- GAAP 與 non-GAAP 混淆；standard FCF、Adjusted FCF、OCF 混淆或缺少重要 reconciliation 說明。
- Transcript/Q&A 已在 evidence 中但報告說沒有，或 collection_status=NOT_FOUND_AFTER_RETRY 卻斷言官方未發布。
- market consensus 與 company guidance 混為一談；沒有外部 consensus 卻宣稱 Beat/Miss。
- 重大 Q&A / supply / pricing / demand / margin 訊號被漏掉，導致投資人判讀失真。
- numerical_errors 非空。

corrected_telegram_draft 必須沿用完整標題、emoji、欄位順序與簡易表格格式，少於 3200 字；不要輸出資料來源段落。
Schema: {overall_score:0-100, industry_cross_check:[string], unsupported_claims:[string], numerical_errors:[string], missing_material_points:[string], misleading_inferences:[string], critical_issues:[string], pass:boolean, corrected_telegram_draft:string}.
Pass can only be true if overall_score>=90 and unsupported_claims, numerical_errors, and critical_issues are all empty.
Input:\n""" + json.dumps({"event_id": event.event_id, "facts": facts, "analysis": analysis,
                                      "evidence": json.loads(self._evidence(event, evidence))}, ensure_ascii=False),
                          "auditor", tools=[{"url_context": {}}])

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        return self._json("""依稽核結果修訂台灣繁體中文投資人分析。移除所有 unsupported/misleading claim，修正 guidance range/midpoint、GAAP/non-GAAP、FCF taxonomy、Consensus 與 Transcript 狀態措辭。只回傳相同 analyst JSON schema，不得增加新事實。telegram_draft 保留完整標題、emoji、欄位順序與簡易表格，少於 3200 字，不輸出資料來源段落。
Input:\n""" + json.dumps({"facts": facts, "analysis": analysis, "audit": audit}, ensure_ascii=False), "revision")

    def material_update(self, facts: dict, previous_count: int, current_count: int) -> bool:
        if current_count <= previous_count:
            return False
        decision = self._json("""Determine whether newly supplied official facts add material information that would change an existing earnings summary. Return JSON only: {material:boolean, reason:string}. A newly available official transcript/Q&A, changed guidance, key financial number, demand/order signal, supply/pricing detail, or management explanation is material. A duplicate or formatting-only document is not material.
Facts:\n""" + json.dumps(facts, ensure_ascii=False), "material_update")
        return bool(decision.get("material"))
