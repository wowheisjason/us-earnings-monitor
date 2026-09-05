from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from .checkpointing import get_stage
from .evidence_architecture import quote_validation_issues
from .investor_analysis import InvestorFrameworkGeminiClient
from .models import EarningsEvent, Evidence
from .research_packet import build_research_packet
from .research_units import batch_units, coverage_result, unitize_evidence

_CARD_SCHEMA = r"""
{
  "processed_unit_ids":[string],
  "cards":[{
    "unit_id":string,
    "card_type":"fact"|"guidance"|"management_claim"|"qa"|"risk"|"customer"|"competitive"|"capital"|"other",
    "topic":"demand_revenue"|"guidance"|"margin_unit_economics"|"cash_capex"|"customer_usage"|"product_competition"|"supply_capacity"|"qa_management"|"risk"|"other",
    "fact_class":"objective_fact"|"management_claim"|"analyst_question"|"company_guidance"|"unknown",
    "statement":string,
    "metric":string|null,
    "value":number|string|null,
    "unit":string|null,
    "period":string|null,
    "comparison_value":number|string|null,
    "comparison_period":string|null,
    "reported_change":string|null,
    "low":number|string|null,
    "midpoint":number|string|null,
    "high":number|string|null,
    "previous_low":number|string|null,
    "previous_midpoint":number|string|null,
    "previous_high":number|string|null,
    "materiality_candidate":1|2|3|4|5,
    "speaker":string|null,
    "analyst":string|null,
    "management_speaker":string|null,
    "question_summary":string|null,
    "answer_summary":string|null,
    "answer_quality":"direct"|"partial"|"evasive"|"unknown"|null,
    "customer":string|null,
    "product":string|null,
    "use_case":string|null,
    "outcome":string|null,
    "quantified_result":string|null,
    "quote":string
  }]
}
"""


class ProductionInvestorV5Client(InvestorFrameworkGeminiClient):
    """Full-coverage earnings research pipeline.

    Every relevant source unit is read once by a low-cost extraction stage. The
    higher-quality analyst and auditor operate only on a compact, provenance-rich
    research packet, never on the raw long-form transcript again.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._checkpoint: dict | None = None
        self._persist_checkpoint_stage: Callable[[str, dict], None] | None = None

    def configure_analysis_checkpoint(self, checkpoint: dict, persist_stage: Callable[[str, dict], None]) -> None:
        self._checkpoint = checkpoint
        self._persist_checkpoint_stage = persist_stage

    def _checkpoint_payload(self, stage: str) -> dict | None:
        if self._checkpoint is None:
            return None
        value = get_stage(self._checkpoint, stage)
        return value if isinstance(value, dict) else None

    def _persist(self, stage: str, payload: dict) -> None:
        if self._persist_checkpoint_stage is not None:
            self._persist_checkpoint_stage(stage, payload)

    @staticmethod
    def _unit_payload(units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "unit_id": unit["unit_id"],
                "document_key": unit["document_key"],
                "title": unit["title"],
                "source": unit.get("source", ""),
                "document_kind": unit.get("document_kind", ""),
                "phase": unit.get("phase", ""),
                "position": unit.get("position", 0),
                "text": unit.get("text", ""),
            }
            for unit in units
        ]

    def _extract_batch(self, event: EarningsEvent, units: list[dict[str, Any]], stage: str) -> dict:
        expected = [unit["unit_id"] for unit in units]
        return self._json(f"""You are a forensic earnings-evidence mapper for a professional US-equity investor. Return COMPACT JSON only.

Event: {event.event_id}
You are seeing a bounded batch from a larger event. Process EVERY supplied unit exactly once and acknowledge every unit_id in processed_unit_ids. Do not infer that omitted units or documents are absent.

Your job is evidence capture, NOT final investment analysis. Extract every economically material point from these units while staying compact. A unit may legitimately produce zero cards, but it still must be acknowledged.

Evidence rules:
1. Use ONLY the supplied text/structured facts. Never calculate a missing percentage, margin, growth rate, midpoint or comparison.
2. Preserve GAAP/non-GAAP/adjusted/company-defined labels and USD million/billion units exactly.
3. Guidance must preserve the full disclosed range and prior guidance when present. If only one value or a percentage-of-revenue target is disclosed, represent only that value; do not manufacture low/high.
4. Separate objective fact, company guidance, management claim and analyst question. Prepared remarks are not Q&A.
5. Capture explicit drivers of revenue/demand, volume, price, mix, adoption/usage, backlog/orders/RPO, customer breadth/depth, margin/unit economics, cash/capex, capacity/supply, product/competition and risks.
6. In Q&A, capture EACH material debate visible in the unit. Preserve analyst and management speakers when available. answer_quality=direct only if management actually answers the economic question; use partial/evasive/unknown otherwise.
7. Customer proof must separate customer/use case/product/outcome/quantified result; never infer ROI.
8. materiality_candidate is only a triage hint: 5=could change forward earnings power or thesis; 4=material driver/guidance/Q&A; 3=useful corroboration; 1-2=low-value detail.
9. quote must be a SHORT exact source substring, not a paraphrase. The program validates it deterministically.
10. Third-party transcript evidence, if source metadata says so, is qualitative management/Q&A evidence only; do not treat its financial numbers as primary facts.

Expected unit ids: {json.dumps(expected, ensure_ascii=False)}
Schema:
{_CARD_SCHEMA}
Units:
{json.dumps(self._unit_payload(units), ensure_ascii=False)}
""", stage)

    @staticmethod
    def _decorate_cards(cards: list[dict[str, Any]], units: list[dict[str, Any]]) -> list[dict[str, Any]]:
        by_id = {unit["unit_id"]: unit for unit in units}
        output: list[dict[str, Any]] = []
        for index, raw in enumerate(cards):
            if not isinstance(raw, dict):
                continue
            unit_id = str(raw.get("unit_id") or "")
            unit = by_id.get(unit_id)
            if unit is None:
                continue
            card = dict(raw)
            card["card_id"] = f"c{index + 1}:{unit_id}"
            card["document_key"] = unit["document_key"]
            card["source"] = unit.get("source", "")
            card["provenance"] = (
                "third_party_transcript"
                if "third_party" in str(unit.get("source", "")).casefold()
                else "primary_or_official"
            )
            card["position"] = unit.get("position", 0)
            quote = str(card.get("quote") or "").strip()
            card["quote"] = quote
            card["evidence"] = {"document_key": unit["document_key"], "quote": quote}
            output.append(card)
        return output

    @staticmethod
    def _compatibility_views(cards: list[dict[str, Any]]) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        guidance: list[dict[str, Any]] = []
        cash: list[dict[str, Any]] = []
        qa: list[dict[str, Any]] = []
        for card in cards:
            evidence = card.get("evidence") or {}
            card_type = str(card.get("card_type") or "")
            if card_type == "guidance":
                guidance.append({
                    "metric": card.get("metric"), "period": card.get("period"),
                    "low": card.get("low"), "midpoint": card.get("midpoint"), "high": card.get("high"),
                    "unit": card.get("unit"), "previous_low": card.get("previous_low"),
                    "previous_midpoint": card.get("previous_midpoint"), "previous_high": card.get("previous_high"),
                    "change": card.get("reported_change"), "evidence": evidence,
                })
            if card_type == "qa":
                qa.append({
                    "category": card.get("topic"), "question_summary": card.get("question_summary"),
                    "answer_summary": card.get("answer_summary"), "analyst": card.get("analyst"),
                    "management_speaker": card.get("management_speaker"), "answer_quality": card.get("answer_quality"),
                    "evidence": evidence,
                })
            if card.get("metric") not in (None, "") and card_type != "guidance":
                row = {
                    "metric": card.get("metric"), "value": card.get("value"), "unit": card.get("unit"),
                    "period": card.get("period"), "comparison_value": card.get("comparison_value"),
                    "comparison_period": card.get("comparison_period"), "reported_change": card.get("reported_change"),
                    "evidence": evidence,
                }
                facts.append(row)
                if str(card.get("topic") or "") == "cash_capex":
                    cash.append({**row, "metric_type": "other", "reconciliation": []})
        return {"facts": facts[:120], "guidance": guidance[:30], "cash_flow_and_capex": cash[:30], "qa": qa[:40]}

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        units, manifest = unitize_evidence(evidence)
        batches = batch_units(units)
        all_cards: list[dict[str, Any]] = []
        processed: list[str] = []

        for index, batch in enumerate(batches, start=1):
            stage = f"v5_extract_batch_{index}"
            result = self._checkpoint_payload(stage)
            if result is None:
                result = self._extract_batch(event, batch, "v5_extract")
                self._persist(stage, result)
            batch_ids = {unit["unit_id"] for unit in batch}
            acknowledged = [str(value) for value in (result.get("processed_unit_ids") or []) if str(value) in batch_ids]
            missing = [unit for unit in batch if unit["unit_id"] not in set(acknowledged)]
            cards = self._decorate_cards(result.get("cards") or [], batch)

            if missing:
                repair_stage = f"v5_extract_missing_{index}"
                repair = self._checkpoint_payload(repair_stage)
                if repair is None:
                    repair = self._extract_batch(event, missing, "v5_extract_repair")
                    self._persist(repair_stage, repair)
                missing_ids = {unit["unit_id"] for unit in missing}
                repaired_ack = [str(value) for value in (repair.get("processed_unit_ids") or []) if str(value) in missing_ids]
                acknowledged.extend(repaired_ack)
                cards.extend(self._decorate_cards(repair.get("cards") or [], missing))

            processed.extend(acknowledged)
            all_cards.extend(cards)

        coverage = coverage_result(manifest, processed)
        if not coverage["complete"]:
            raise RuntimeError(
                "V5 evidence coverage incomplete: "
                f"ratio={coverage['coverage_ratio']} missing={coverage['missing_unit_ids'][:8]} "
                f"source_truncated={coverage['source_truncated_documents']}"
            )

        quote_issues = quote_validation_issues({"cards": all_cards}, evidence)
        packet = build_research_packet(all_cards, coverage)
        views = self._compatibility_views(all_cards)
        return {
            "research_pipeline_version": 5,
            "coverage": coverage,
            "research_packet": packet,
            "cards": all_cards,
            **views,
            "market_consensus": [],
            "quote_validation_issues": quote_issues,
            "extraction_batch_count": len(batches),
        }

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        packet = facts.get("research_packet") or {}
        return self._json(f"""你是負責科技股的資深 buy-side analyst。請只回傳 JSON，所有解釋與投資判斷使用台灣繁體中文；公司、產品、技術與正式財務 metric 可保留英文。

你收到的是已完成 100% relevant-unit coverage 的 Research Packet。每一份原始財報/簡報/逐字稿都已先由 evidence mapper 逐段閱讀；你的工作不是再摘要文件，而是把證據轉成可做投資決策的判斷。

核心目標：找出本季資訊如何改變未來 1–3 年 earnings power，以及哪些部分仍未被證明。

分析順序：
1. WHAT CHANGED：只使用 explicit current vs prior period / prior guidance。沒有 prior evidence 就不要自行宣稱加速、惡化或 management tone 改變。
2. EARNINGS POWER BRIDGE：
   - Revenue/demand：volume / price / mix / usage / adoption / orders / backlog / RPO / customer breadth/depth。
   - Margin/unit economics：product mix / pricing / utilization / scale / opex / cost / supply constraints；沒有 bridge evidence 不得硬歸因。
   - Cash/capital intensity：CFO / FCF / capex / working capital / capacity investment，並說明可持續性。
3. GUIDANCE：完整呈現 range/company-defined units、前次 guidance（若有）與其改變。分清楚公司 guidance 與外部 consensus；本系統若未提供 consensus，不判定 Beat/Miss。
4. DEMAND QUALITY：把 customer count、large-customer cohorts、NRR/RPO/orders/usage 等證據交叉看，區分 breadth、depth、expansion quality 與 concentration；證據不足就明寫不能判定。
5. Q&A ADVERSARIAL READ：優先選 2–4 個最重要 debate，呈現「分析師真正追問什麼 → management 怎麼答 → direct/partial/evasive → 對投資 thesis 的含義」。不要把 Q&A 當逐題摘要。
6. MANAGEMENT CREDIBILITY：只有 Research Packet 含 prior commitment / prior guidance evidence 才評估。沒有歷史 evidence 就不要做人格或可信度推論。
7. COMPETITIVE / VALUE CHAIN：只有直接 evidence 足以支持 pricing power、control point、share gain/loss、switching cost 或 value capture 時才下結論；高成長本身不是 moat。
8. SKEPTICAL CHECK：每個重大 thesis 都列 alternative explanation 與可驗證的 disconfirming indicator。

證據層級必須分開：objective fact ≠ management claim ≠ analyst inference。第三方 transcript 如有，只能支撐 qualitative Q&A/management wording；數字需由 primary/official cards 支持。

請輸出：
{{
  "investment_thesis":[{{"signal":string,"why_it_matters":string,"fact_basis":[string],"inference":string,"confidence":"high"|"medium"|"low"}}],
  "earnings_power_bridge":{{"revenue_demand":[string],"margin_unit_economics":[string],"cash_capital_intensity":[string]}},
  "guidance_assessment":[string],
  "demand_quality":[string],
  "qa_readthrough":[{{"debate":string,"management_answer":string,"answer_quality":"direct"|"partial"|"evasive"|"unknown","investment_readthrough":string}}],
  "competitive_value_chain":[string],
  "risks_and_disconfirming_evidence":[string],
  "watch_items":[string],
  "confidence":0-100,
  "telegram_draft":string
}}

Telegram draft 目標 1900–2600 字，禁止為了塞數字而寫 KPI inventory；只保留能改變投資判斷的內容。固定四區：
{event.ticker} {('FY'+str(event.fiscal_year)+' '+str(event.quarter)) if event.fiscal_year and event.quarter else event.event_id}

💡 投資結論與邏輯:
📊 關鍵數據與財測:
🧭 營運動能與法說攻防:
⚠️ 風險、反證與待驗證:

每個重要數字後都要有 read-through，但不要重複同一數字。美元維持來源的 $M/$B 等單位，禁止轉成中文億/兆美元。沒有外部 consensus 時只寫一次「外部市場共識未納入，本報告不判定 Beat/Miss。」

Research Packet:
{json.dumps(packet, ensure_ascii=False)}
""", "v5_analyst")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        packet = facts.get("research_packet") or {}
        quote_issues = facts.get("quote_validation_issues") or []
        result = self._json(f"""你是 reject-oriented institutional earnings research auditor。只回傳 JSON，使用台灣繁體中文。

你只需審核 Research Packet 與 analyst output；不要重新閱讀長篇原始 transcript。原始 corpus 已由 deterministic coverage manifest 保證逐 unit 處理，quote validation 由程式完成。

Publication blockers：
- Research Packet coverage.complete 不是 true 或 coverage_ratio < 1.0。
- unsupported number / unsupported causal claim / management claim 被寫成 objective fact。
- 遺漏 packet 中 materiality 4–5 的 thesis-changing evidence，導致投資結論方向失真。
- guidance range、GAAP/non-GAAP、FCF/capex taxonomy 錯誤。
- 沒有外部 consensus 卻寫 Beat/Miss。
- Q&A 存在 material debate 卻只做 prepared-remarks summary，或把 partial/evasive answer 寫成已證實。
- customer anecdote 被外推為整體 demand、pricing power、moat/control point 等強結論。
- 第三方 transcript 的數字未經 primary evidence corroboration。
- 重大 alternative explanation / disconfirming indicator 被省略，造成 plausible 被寫成 proven。

格式微小差異（標題編號、標點、emoji spacing）不是 semantic blocker，不應要求重新做整份 research。corrected_telegram_draft 可以直接修正格式與冗詞。

輸出：
{{
  "overall_score":0-100,
  "unsupported_claims":[string],
  "numerical_errors":[string],
  "missing_material_points":[string],
  "causal_reasoning_errors":[string],
  "qa_interpretation_errors":[string],
  "accounting_guidance_errors":[string],
  "critical_issues":[string],
  "pass":boolean,
  "corrected_telegram_draft":string
}}
Pass=true 僅限 overall_score>=90 且上述 error/critical arrays 全空。

Coverage:
{json.dumps(packet.get('coverage', {}), ensure_ascii=False)}
Quote validation issues:
{json.dumps(quote_issues, ensure_ascii=False)}
Research Packet:
{json.dumps(packet, ensure_ascii=False)}
Analyst output:
{json.dumps(analysis, ensure_ascii=False)}
""", "v5_auditor")

        coverage = packet.get("coverage") or {}
        critical = list(result.get("critical_issues") or [])
        if coverage.get("complete") is not True or float(coverage.get("coverage_ratio", 0) or 0) < 1.0:
            critical.append("deterministic_v5_gate:incomplete_research_coverage")
        if critical:
            result["critical_issues"] = list(dict.fromkeys(critical))
            result["pass"] = False
            result["overall_score"] = min(int(result.get("overall_score", 0) or 0), 80)
        return result

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        packet = facts.get("research_packet") or {}
        errors = {
            key: audit.get(key) or []
            for key in (
                "unsupported_claims", "numerical_errors", "missing_material_points", "causal_reasoning_errors",
                "qa_interpretation_errors", "accounting_guidance_errors", "critical_issues",
            )
        }
        return self._json(f"""你是 buy-side senior analyst，現在只做 TARGETED semantic repair。不要重新摘要全部文件，也不要新增 Research Packet 以外的事實。

依 auditor errors 修正目前 analysis。保留已正確的 thesis，只修改被點名的 unsupported/missing/causal/Q&A/accounting 問題。若錯誤只是格式或冗詞，直接修 Telegram，不要重建經濟論述。

回傳與原 analyst 完全相同 JSON schema。Telegram 維持四區、1900–2600 字、台灣繁體中文、美元來源單位、不判定無來源 consensus 的 Beat/Miss。

Auditor errors:
{json.dumps(errors, ensure_ascii=False)}
Current analysis:
{json.dumps(analysis, ensure_ascii=False)}
Research Packet:
{json.dumps(packet, ensure_ascii=False)}
""", "v5_repair")
