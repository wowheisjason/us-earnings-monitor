from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import requests

from .models import EarningsEvent, Evidence

EVIDENCE_TOTAL_MAX_CHARS = 48_000
LOG = logging.getLogger("us_earnings_monitor")
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


class GeminiClient:
    """REST client with strict JSON outputs and no implicit numerical guessing."""

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
        """Prefer the newest ordinary Flash text model, then other text models."""
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
            params={"key": self.api_key, "pageSize": 1000},
            timeout=30,
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
        LOG.info(
            "Gemini usage stage=%s model=%s prompt=%d output=%d thoughts=%d total=%d cumulative=%d",
            stage, model, prompt, output, thoughts, total, self.usage["total_tokens"],
        )

    def _json(self, prompt: str, stage: str) -> dict[str, Any]:
        last_error: Exception | None = None
        # Five fallbacks are ample and avoid probing every special-purpose model.
        for model in self._available_models()[:5]:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
            for attempt in range(2):
                response = self.session.post(url, params={"key": self.api_key}, json={
                    "contents": [{"role": "user", "parts": [{"text": prompt}]}],
                    "generationConfig": {"responseMimeType": "application/json", "temperature": 0.1},
                }, timeout=120)
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
                text = payload["candidates"][0]["content"]["parts"][0]["text"]
                return json.loads(text)
        if last_error:
            raise last_error
        raise RuntimeError("No available Gemini model completed the request")

    @staticmethod
    def _evidence(event: EarningsEvent, evidence: list[Evidence]) -> str:
        items = []
        per_document = EVIDENCE_TOTAL_MAX_CHARS // max(1, len(evidence))
        for item in evidence:
            items.append({"document_key": item.document_key, "title": item.title, "url": item.url,
                          "structured_facts": item.structured_facts, "excerpt": item.text[:per_document]})
        return json.dumps({"event_id": event.event_id, "documents": items}, ensure_ascii=False)

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        return self._json("""You are an evidence extractor for US-listed-company earnings. Return JSON only.
Use ONLY the official evidence below. Never infer, calculate, or fill missing values: use null.
For every fact, include exact evidence document_key and a short supporting quote or structured concept.
Extract reported results, YoY/QoQ, company guidance, segment results, cash flow/capex, industry or demand signals, and Q&A if present.
Market consensus/expectations must be null unless explicitly present in the official evidence. A company forecast is not market consensus.
Schema: {event_id, company_name, facts:[{metric,value,unit,period,comparison,evidence:{document_key,quote}}], guidance:[...], segments:[...], cash_flow_and_capex:[...], industry_signals:[...], management_comments:[...], qa:[...], market_consensus:[...], unknowns:[...]}.
Evidence:\n""" + self._evidence(event, evidence), "facts")

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是專業機構投資人（professional investor），這是第一次分析。請以台灣繁體中文撰寫並只回傳 JSON；專業術語與正式名稱保留英文原文。
只能使用 structured facts。分開呈現客觀事實、投資判讀及未知事項，不提供目標價或買賣建議。官方資料沒有市場共識時，必須明寫「官方資料未提供市場共識，無法判定 Beat/Miss」，禁止自行補入預期值。
Schema: {facts:[string], investor_interpretation:[string], investment_implications:[string], risks_and_unknowns:[string], confidence:0-100, telegram_draft:string}.
telegram_draft 必須少於 3200 字，且嚴格使用以下順序與標題；沒有資料的欄位保留並寫「官方資料未提供／無法確認」，不可杜撰。不要輸出「資料來源」段落，程式會以官方文件建立可點擊連結。
金額一律使用台灣常用的兆、億、萬，不得使用「百萬」；可對已驗證美元數字做精確單位換算（1 million 美元=100 萬美元、100 million 美元=1 億美元、1 trillion 美元=1 兆美元），合理保留最多兩位小數。
刪除「預期對比」整個區塊，也不要在關鍵指標表格放「預期」欄。

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

Structured facts:\n""" + json.dumps({
            "event_id": event.event_id,
            "facts": facts,
        }, ensure_ascii=False), "analyst")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是該美國上市企業所屬產業的資深產業專家（industry expert），負責第二次分析與交叉比對，同時是嚴格的 evidence auditor。請以台灣繁體中文撰寫並只回傳 JSON；專業術語與正式名稱保留英文原文。
逐項把第一次投資人分析中的數字、因果關係、產業判讀、財測、共識及 Q&A 對回 ORIGINAL official evidence。可信但沒有證據的說法仍是 unsupported。檢查是否遺漏重大產業訊號，並修正容易誤導投資人的推論。
corrected_telegram_draft 必須沿用第一次分析的完整標題、emoji、欄位順序與簡易表格格式，少於 3200 字；缺資料明寫「官方資料未提供／無法確認」。不要輸出資料來源段落。刪除「預期對比」區塊與關鍵指標的「預期」欄。金額一律精確換算為兆、億、萬，不得使用「百萬」。
Schema: {overall_score:0-100, industry_cross_check:[string], unsupported_claims:[string], numerical_errors:[string], missing_material_points:[string], misleading_inferences:[string], pass:boolean, corrected_telegram_draft:string}.
Pass can only be true if overall_score>=90 and unsupported_claims is empty.
Input:\n""" + json.dumps({"event_id": event.event_id, "facts": facts, "analysis": analysis,
                                      "evidence": json.loads(self._evidence(event, evidence))}, ensure_ascii=False), "auditor")

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        return self._json("""依產業專家稽核結果修訂台灣繁體中文投資人分析，移除所有 unsupported 或 misleading claim。只回傳相同的 analyst JSON schema，不得增加新事實。telegram_draft 必須保留指定的完整標題、emoji、欄位順序與簡易表格格式，少於 3200 字，且不要輸出資料來源段落。刪除「預期對比」區塊與關鍵指標的「預期」欄，金額一律使用兆、億、萬而非百萬。
Input:\n""" + json.dumps({"facts": facts, "analysis": analysis, "audit": audit}, ensure_ascii=False), "revision")

    def material_update(self, facts: dict, previous_count: int, current_count: int) -> bool:
        if current_count <= previous_count:
            return False
        decision = self._json("""Determine whether newly supplied official facts add material information that would change an existing earnings summary. Return JSON only: {material:boolean, reason:string}. Material means changed guidance, key financial number, demand/order signal, or management explanation; a duplicate or formatting-only document is not material.
Facts:\n""" + json.dumps(facts, ensure_ascii=False), "material_update")
        return bool(decision.get("material"))

