from __future__ import annotations

import json

from .evidence_architecture import (
    balanced_evidence_payload,
    extraction_groups,
    merge_partial_extractions,
    qa_evidence_payload,
    quote_validation_issues,
    sections_json,
)
from .investor_analysis import InvestorFrameworkGeminiClient
from .models import EarningsEvent, Evidence


_EXTRACTION_SCHEMA = r"""
{
  event_id,
  company_name,
  facts:[{metric,value,unit,period,comparison_value,comparison_period,reported_change,evidence:{document_key,quote}}],
  guidance:[{metric,period,low,midpoint,high,unit,reported_yoy,previous_low,previous_midpoint,previous_high,change,evidence:{document_key,quote}}],
  segments:[{name,metric,value,unit,period,comparison_value,reported_change,evidence:{document_key,quote}}],
  cash_flow_and_capex:[{metric,metric_type,value,unit,period,reported_change,reconciliation:[{item,value,unit}],evidence:{document_key,quote}}],
  quantitative_evidence:[{category:"financial"|"adoption"|"usage"|"roi"|"scale"|"productivity"|"orders"|"pricing"|"other",metric,value,unit,period,reported_change,evidence:{document_key,quote}}],
  change_signals:[{dimension,current_state,prior_state,reported_change,direction:"improving"|"deteriorating"|"mixed"|"unchanged"|"unknown",evidence:{document_key,quote}}],
  customer_cases:[{customer,problem,use_case,product,outcome,quantified_result,evidence:{document_key,quote}}],
  industry_signals:[{topic,signal,evidence:{document_key,quote}}],
  management_claims:[{topic,claim,evidence:{document_key,quote}}],
  management_comments:[{topic,statement,evidence:{document_key,quote}}],
  qa:[{category,question_summary,answer_summary,analyst,management_speaker,evidence:{document_key,quote}}],
  market_consensus:[],
  unknowns:[...]
}
"""

_LIST_LIMITS = {
    "facts": 80, "guidance": 24, "segments": 48, "cash_flow_and_capex": 30,
    "quantitative_evidence": 72, "change_signals": 40, "customer_cases": 24,
    "industry_signals": 36, "management_claims": 40, "management_comments": 40,
    "qa": 36, "market_consensus": 12, "unknowns": 24,
}


def _bounded_facts(value: dict) -> dict:
    output = dict(value)
    for key, limit in _LIST_LIMITS.items():
        if isinstance(output.get(key), list):
            output[key] = output[key][:limit]
    return output


class InvestorFrameworkV3Client(InvestorFrameworkGeminiClient):
    """Institutional V3 with deterministic document compression and Q&A priority."""

    def _extract_group(self, event: EarningsEvent, payload: str, *, partial: bool) -> dict:
        scope = (
            "This is ONE bounded section group from a larger earnings corpus. Do not assume omitted sections are absent."
            if partial else "This payload contains the complete bounded evidence corpus for this event."
        )
        return self._json(f"""You are a strict evidence extractor for US-listed-company earnings. Return COMPACT JSON only.
{scope}
Use ONLY supplied evidence. Do not estimate, infer, invent, or silently normalize company-defined metrics. Missing values=null.
Every extracted item must carry evidence.document_key and a SHORT EXACT SOURCE QUOTE.

Rules:
1. Preserve GAAP/non-GAAP/adjusted/company-defined labels exactly. Adjusted FCF is never plain FCF.
2. Preserve complete guidance ranges and previous guidance only when explicit.
3. Extract current/comparison values separately; never calculate missing comparisons.
4. Capture adoption/usage/customer count/orders/backlog/pricing/migration/productivity/cost savings/ROI/capacity and supply evidence.
5. Customer case = Problem -> Workload/Product -> Outcome -> Quantified result; never infer ROI.
6. Separate objective facts from management claims.
7. Q&A is high priority: retain analyst/management names and material answers on demand, pricing, competition, mix, margins, guidance, capex, supply/capacity and risk.
8. change_signals require BOTH current and prior state.
9. market_consensus=[] unless external consensus is explicitly inside supplied evidence.
10. Be selective and do not output duplicate variants of the same fact.

Schema:
{_EXTRACTION_SCHEMA}
Evidence sections:
{payload}
""", "facts_chunk" if partial else "facts")

    def _extract_qa(self, event: EarningsEvent, evidence: list[Evidence]) -> list[dict]:
        payload = qa_evidence_payload(evidence)
        if not payload.get("sections"):
            return []
        result = self._json(f"""You are a focused US earnings-call Q&A extractor. Return JSON only.
Event: {event.event_id}. Use ONLY supplied Q&A sections. Select at most 12 investment-material exchanges.
Prioritize demand, forward guidance, pricing, gross margin, supply/capacity, competition, customer deployment/ROI, capex and risks.
Do not turn prepared remarks into Q&A. Preserve analyst and management speaker names. Each item needs a short exact quote.
Schema: {{qa:[{{category,question_summary,answer_summary,analyst,management_speaker,evidence:{{document_key,quote}}}}]}}.
Evidence:\n{json.dumps(payload, ensure_ascii=False)}
""", "qa_extract")
        return (result.get("qa") or [])[:12]

    def _cross_context_clusters(self, facts: dict) -> list[dict]:
        nodes = []
        for key in ("qa", "management_claims", "management_comments", "customer_cases", "guidance", "quantitative_evidence"):
            for item in (facts.get(key) or [])[:30]:
                nodes.append({"source_type": key, **item} if isinstance(item, dict) else {"source_type": key, "value": item})
        if len(nodes) < 3:
            return []
        result = self._json("""You are a cross-context earnings evidence clusterer. Return COMPACT JSON only.
Group nodes from different Q&A turns, executives or document sections only when they address the SAME economic mechanism. Preserve contradictions. synthesis_candidate is a hypothesis unless directly stated. Identify the weakest/unproven link.
Schema: {clusters:[{topic,evidence_nodes:[{source_type,document_key,speaker_or_analyst,summary}],shared_mechanism,synthesis_candidate,contradictions:[string],weak_link:string}]}.
Input:\n""" + json.dumps({"nodes": nodes}, ensure_ascii=False), "cross_context")
        return (result.get("clusters") or [])[:12]

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        groups = extraction_groups(evidence)
        if len(groups) <= 1:
            facts = self._extract_group(event, sections_json(groups[0] if groups else []), partial=False)
        else:
            partials = [self._extract_group(event, sections_json(group), partial=True) for group in groups]
            # Deterministic union replaces the former LLM consolidation call.
            # This removes a high-token/high-failure stage without rewriting source facts.
            facts = _bounded_facts(merge_partial_extractions(partials))
        if not facts.get("qa"):
            focused_qa = self._extract_qa(event, evidence)
            if focused_qa:
                facts["qa"] = focused_qa
        facts = _bounded_facts(facts)
        facts["quote_validation_issues"] = quote_validation_issues(facts, evidence)
        facts["cross_context_clusters"] = self._cross_context_clusters(facts)
        facts["extraction_mode"] = "deterministic_map_merge" if len(groups) > 1 else "single_bounded_corpus"
        facts["extraction_group_count"] = len(groups)
        return facts

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是專業 buy-side 科技股投資人兼產業專家。所有解釋、結論、風險與 Telegram 文字都必須使用台灣繁體中文；只有公司名、產品名、技術名、財務 metric 等正式名稱可保留英文。只回傳 JSON。

目標不是 summary，而是找出會改變未來 1–3 年 earnings power 的訊號。只能使用 structured facts；不得加入外部資料、目標價或買賣建議。
方法：FACT -> CHANGE -> MATERIALITY -> CROSS-CONTEXT SYNTHESIS -> CAUSAL CHAIN -> SKEPTICAL CHECK。

硬規則：
1. Headline 只有在改變 growth/margin/guidance/cash generation/segment mix/thesis 時才升格核心結論。
2. Change Detection 只使用 explicit current vs prior period/prior guidance。
3. Cross-Context 不得把 hypothesis 當 management fact。
4. Customer Proof = Problem -> Product/Workload -> Outcome -> Quantified ROI；單一案例不可外推。
5. Causal Chain 只有 evidence 足夠才建立，每條列 weak_link。
6. Value-Chain Shift 沒有 material control-point/value-capture evidence 就省略。
7. Driver/Lever/Catalyst/Risk 與 Materiality 1-5、Evidence A-D 是內部分析欄位；Telegram 不得顯示這些 tag。
8. quote_validation_issues 非空時相關 evidence 不可升 A。
9. 沒有外部 consensus 時固定寫「未納入外部市場共識，因此不判定 Beat/Miss」。
10. structured facts 有 qa 時，「法說 Q&A」必須列 1-3 個真正 material Q&A；沒有 qa 時明寫「目前取得的可驗證資料未包含 Q&A」。
11. 完整 guidance range、GAAP/non-GAAP、FCF taxonomy 必須保留。

Schema:
{facts:[string],material_signals:[{signal,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",why_it_matters}],change_detection:[{change,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",investment_effect}],cross_context_synthesis:[{topic,synthesis,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",supporting_nodes:[string],contradiction_or_weak_link:string}],causal_chains:[{chain,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",weak_link}],value_chain_position_change:[{from_control_point,to_control_point,economic_implication,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D"}],evidence_assessment:[{claim,evidence,grade:"A"|"B"|"C"|"D",missing_evidence}],investor_interpretation:[string],investment_implications:[string],risks_and_unknowns:[string],confidence:0-100,telegram_draft:string}

telegram_draft <3200字、高資訊密度，全文自然繁體中文，不顯示任何 [M5｜A]、[Driver]、[Risk] 等內部 tag，不要資料來源段落。美元金額保持 facts 的 USD million/billion 單位。
格式：
{公司/股票代碼} {FY季度}

💡 核心投資結論:
• ...

🔄 本季變化:
• ...

🧩 跨段資訊整合:
• ...
└ 尚未確認: ...

📌 關鍵論述與證據:
• ...

📢 財測與共識:
• ...

📈 關鍵指標:
指標 | 本期 | YoY/QoQ
...

🏢 業務部門 / 客戶ROI:
• ...

🔗 因果鏈與單位經濟:
• ...

🔮 未來展望:
• ...

🎙️ 法說 Q&A:
❓ ...
💬 ...

⚖️ 反證與未知:
• ...

Structured facts:\n""" + json.dumps({"event_id": event.event_id, "facts": facts}, ensure_ascii=False), "analyst_v3")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是 reject-oriented institutional evidence auditor。所有敘述與 corrected_telegram_draft 必須使用台灣繁體中文；正式名稱可保留英文。只回傳 JSON。
目標是找出足以阻擋發布的問題，不是幫 analyst 過關。

以下任一情況都必須 critical_issues 非空且 pass=false：unsupported number/claim；不存在的 prior evidence；錯誤 cross-context；hypothesis 當 management fact；correlation 當 causation；Materiality/Evidence grade 高估；unsupported ROI/value-chain shift；guidance/GAAP/non-GAAP/FCF/consensus/transcript status 錯誤；quote validation issue 被忽略；遺漏 material Q&A；numerical error；Telegram 大段使用英文敘述；Telegram 顯示 [M5|A]、[Driver]、[Risk] 等內部 tag。
如果 facts.qa 非空，corrected_telegram_draft 必須包含至少一個有投資意義的法說 Q&A。

corrected_telegram_draft <3200字，乾淨繁體中文，不顯示內部評分/分類 tag，不要資料來源段落。
Schema: {overall_score:0-100,industry_cross_check:[string],unsupported_claims:[string],numerical_errors:[string],missing_material_points:[string],misleading_inferences:[string],evidence_grade_errors:[string],materiality_score_errors:[string],cross_context_errors:[string],causal_chain_errors:[string],value_chain_errors:[string],critical_issues:[string],pass:boolean,corrected_telegram_draft:string}.
Pass=true only if overall_score>=90 and ALL error/critical arrays are empty.
Input:\n""" + json.dumps({"event_id": event.event_id, "facts": facts, "analysis": analysis, "evidence": balanced_evidence_payload(evidence)}, ensure_ascii=False), "auditor_v3")

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        return self._json("""依 institutional auditor 結果修訂 V3 buy-side earnings analysis。只使用既有 facts，不得新增事實。
所有解釋、結論、風險與 telegram_draft 必須使用台灣繁體中文；正式名稱可保留英文。移除 unsupported/misleading claim，修正 materiality/evidence、cross-context、causal chain、value-chain shift、ROI、guidance/accounting/consensus/Q&A。若 facts.qa 非空，telegram_draft 必須保留 1-3 個 material Q&A。Telegram 不顯示 [M5|A]、[Driver]、[Lever]、[Catalyst]、[Risk]、[Missing] 等內部標記。
只回傳與 V3 analyst 相同 schema；telegram_draft <3200字，不輸出資料來源。
Input:\n""" + json.dumps({"facts": facts, "analysis": analysis, "audit": audit}, ensure_ascii=False), "revision_v3")
