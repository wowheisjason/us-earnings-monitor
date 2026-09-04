from __future__ import annotations

import json

from .evidence_architecture import (
    balanced_evidence_payload,
    extraction_groups,
    merge_partial_extractions,
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


class InvestorFrameworkV3Client(InvestorFrameworkGeminiClient):
    """Institutional V3: section-aware extraction + cross-context synthesis + 2D ranking."""

    def _extract_group(self, event: EarningsEvent, payload: str, *, partial: bool) -> dict:
        scope = (
            "This is ONE SECTION GROUP from a larger earnings corpus. Extract every material fact in this group; "
            "do not assume omitted sections are absent from the full event."
            if partial else
            "This payload contains the complete bounded evidence corpus for this event."
        )
        return self._json(f"""You are a strict evidence extractor for US-listed-company earnings. Return JSON only.
{scope}
Use ONLY the supplied official evidence sections. Do not estimate, infer, invent, or silently normalize company-defined metrics. Missing values must be null.
Every extracted item must carry evidence.document_key and a SHORT SOURCE QUOTE, not a paraphrase.

Extraction rules:
1. Preserve GAAP/non-GAAP/adjusted/company-defined labels exactly. Adjusted FCF is never plain FCF.
2. Preserve complete guidance ranges and previous guidance when explicitly disclosed.
3. Extract current and comparison values separately. Never calculate a missing comparison.
4. Capture non-P&L evidence: adoption, usage, customer count, transactions/records, orders/backlog, pricing, migration speed, productivity, cost savings and ROI.
5. Customer case = Problem -> Workload/Product -> Outcome -> Quantified result. Do not infer ROI.
6. Separate objective facts from management claims.
7. Q&A is high priority. Preserve analyst/management speaker names and economically relevant answers on demand, pricing, competition, mix, unit economics, margins, guidance, capex and risk.
8. change_signals require BOTH current and prior state in supplied evidence.
9. Headline financial metrics are evidence, but do not suppress them; later stages decide materiality.
10. market_consensus=[] unless external consensus is explicitly inside the supplied evidence.

Schema:
{_EXTRACTION_SCHEMA}
Evidence sections:
{payload}
""", "facts_chunk" if partial else "facts")

    def _consolidate_extractions(self, event: EarningsEvent, merged: dict) -> dict:
        return self._json(f"""You are consolidating partial extractions from ONE earnings event. Return JSON only.
The input already contains source-backed extracted items. Deduplicate equivalent items, preserve the most complete version, and keep distinct Q&A/customer cases. Do not introduce any new fact, number, comparison, inference or quote.
Do not manufacture cross-quarter changes: change_signals require explicit current+prior evidence already present.
Schema:
{_EXTRACTION_SCHEMA}
Input:
{json.dumps({"event_id": event.event_id, "partial_extractions": merged}, ensure_ascii=False)}
""", "facts_consolidation")

    def _cross_context_clusters(self, facts: dict) -> list[dict]:
        nodes = []
        for key in ("qa", "management_claims", "management_comments", "customer_cases", "guidance", "quantitative_evidence"):
            for item in facts.get(key) or []:
                nodes.append({"source_type": key, **item} if isinstance(item, dict) else {"source_type": key, "value": item})
        if len(nodes) < 3:
            return []
        result = self._json("""You are a cross-context earnings-call evidence clusterer. Return JSON only.
Do NOT write an investment conclusion. Group evidence nodes from different Q&A turns, executives or document sections when they address the SAME economic mechanism.
The purpose is to prevent isolated Q&A summaries and surface complementary/contradictory evidence.

Rules:
- Prefer clusters supported by >=2 distinct evidence nodes.
- Preserve speaker/analyst identity when present.
- A synthesis_candidate may connect the dots, but must be phrased as a hypothesis unless the causal link is directly stated.
- Explicitly list contradictions and the weakest/unproven link.
- Do not combine unrelated topics merely to create a cluster.

Schema: {clusters:[{topic,evidence_nodes:[{source_type,document_key,speaker_or_analyst,summary}],shared_mechanism,synthesis_candidate,contradictions:[string],weak_link:string}]}.
Input:\n""" + json.dumps({"nodes": nodes}, ensure_ascii=False), "cross_context")
        return result.get("clusters") or []

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        groups = extraction_groups(evidence)
        if len(groups) <= 1:
            facts = self._extract_group(event, sections_json(groups[0] if groups else []), partial=False)
        else:
            partials = [self._extract_group(event, sections_json(group), partial=True) for group in groups]
            facts = self._consolidate_extractions(event, merge_partial_extractions(partials))
        facts["quote_validation_issues"] = quote_validation_issues(facts, evidence)
        facts["cross_context_clusters"] = self._cross_context_clusters(facts)
        facts["extraction_mode"] = "section_map_reduce" if len(groups) > 1 else "single_bounded_corpus"
        facts["extraction_group_count"] = len(groups)
        return facts

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是專業 buy-side 科技股投資人兼產業專家。以台灣繁體中文撰寫並只回傳 JSON；正式名稱保留英文。

目標不是 summary，而是找出真正會改變未來 1–3 年 earnings power 的訊號。只能使用 structured facts；不得加入外部資料、目標價或買賣建議。

核心方法：FACT -> CHANGE -> MATERIALITY -> CROSS-CONTEXT SYNTHESIS -> CAUSAL CHAIN -> SKEPTICAL CHECK。

硬規則：
1. Headline De-prioritization：Revenue/EPS 等 headline 預設只是 supporting evidence；只有在明顯改變 growth/margin/guidance/cash generation/segment mix/thesis 時才升格成核心結論。不可完全忽略 headline。
2. Change Detection：只能比較 structured facts 中明確存在的 current vs prior period / prior guidance；沒有 prior management wording 就不能宣稱語氣變化。
3. Cross-Context：優先使用 cross_context_clusters，把不同高管、不同 Q&A 對同一經濟機制的互補/矛盾證據一起分析；不可把 cluster 的 hypothesis 當 management stated fact。
4. Customer Proof：Problem -> Product/Workload -> Outcome -> Quantified ROI。單一案例不能外推全體。
5. Causal Chain：只有證據足夠才建立 Adoption/Product -> Usage/Workload -> Revenue -> Cost -> GM/OM/FCF。每條 chain 必須列 weak_link。
6. Optional Value-Chain Shift：只有 evidence 顯示公司 control point/value capture layer 真正上移或下移時才輸出；沒有就空陣列，不得硬找。
7. Driver/Lever/Catalyst/Risk 嚴格分開。
8. Evidence Grade：A=官方量化直接支持；B=多項官方證據相互支持但未完全量化；C=單一 management/customer anecdote；D=analyst inference。
9. Materiality Score 1-5：5=明確改變 earnings power/guidance/structural economics/competitive position；4=重要 leading indicator/unit economics/demand/margin/moat evidence 明顯改變；3=有資訊價值但尚不足改變 thesis；2=單一 anecdote/普通產品 update/weak evidence；1=boilerplate/PR/repeated info。Telegram 原則上只保留 M4-M5；若 M5 但 evidence=C/D，可保留但必須明顯標示低證據強度。
10. Materiality 與 Evidence Strength 是兩個獨立軸，不可互相替代。
11. Skeptical Check：每個重大 management claim 都問 Evidence? Missing? Alternative explanation? Plausible 不可寫成 proven。
12. quote_validation_issues 非空時，相關 evidence 不可升到 A，並在風險/未知中揭露驗證問題。
13. 沒有外部 consensus 時固定寫「未納入外部市場共識，因此不判定 Beat/Miss」。完整 guidance range、GAAP/non-GAAP、FCF taxonomy 必須保留。

Schema:
{
  facts:[string],
  material_signals:[{signal,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",why_it_matters}],
  change_detection:[{change,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",investment_effect}],
  cross_context_synthesis:[{topic,synthesis,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",supporting_nodes:[string],contradiction_or_weak_link:string}],
  causal_chains:[{chain,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D",weak_link}],
  value_chain_position_change:[{from_control_point,to_control_point,economic_implication,materiality_score:1|2|3|4|5,evidence_grade:"A"|"B"|"C"|"D"}],
  evidence_assessment:[{claim,evidence,grade:"A"|"B"|"C"|"D",missing_evidence}],
  investor_interpretation:[string],
  investment_implications:[string],
  risks_and_unknowns:[string],
  confidence:0-100,
  telegram_draft:string
}

telegram_draft <3200字、高資訊密度 analyst earnings notebook。優先輸出 M4-M5；M1-M3 只有在理解 M4-M5 必要時才保留。不要資料來源段落，程式會附官方連結。美元金額保持 facts 的 USD million/billion 單位。

格式：
{公司/股票代碼} {FY季度}

💡 核心投資結論:
• [M5｜A] ...

🔄 本季變化:
• [M4｜B] ...

🧩 跨段 / 跨高管合成:
• [M5｜B] ...
└ Weak link: ...

📌 關鍵論述與證據:
• Claim: ... | Evidence: ...

📢 財測與共識:
• 狀態: ...
• 共識: ...
• 調整: ...

📈 關鍵指標:
指標       | 本期       | YoY/QoQ
----------------------------------
...

🏢 業務部門 / 客戶ROI:
• ...

🔗 因果鏈與單位經濟:
• [M5｜B] ... -> ... -> ...

🧭 Value Chain Shift:
• 若有 material 新證據才顯示；否則整區省略。

🔮 未來展望:
• [Driver] ...
• [Lever] ...
• [Catalyst] ...

🎙️ 法說 Q&A:
❓ ...
💬 ...

⚖️ 反證與未知:
• [Missing] ...
• [Alternative] ...
• [Risk] ...

Structured facts:\n""" + json.dumps({"event_id": event.event_id, "facts": facts}, ensure_ascii=False), "analyst_v3")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是 reject-oriented institutional evidence auditor。以台灣繁體中文撰寫並只回傳 JSON。
你的目標是找出足以阻擋發布的問題，不是幫 analyst 過關。

逐項檢查 ORIGINAL evidence、structured facts、cross-context cluster、M1-M5、A-D、causal chain、value-chain shift、customer ROI、guidance/Q&A。

任何以下情況都必須 critical_issues 非空且 pass=false：
- unsupported number/claim 或 management claim 被當客觀事實。
- change detection 使用不存在的 prior evidence。
- cross-context synthesis 把不同主題錯誤拼接，或把 hypothesis 寫成 management stated fact。
- correlation 被寫成 causation、causal chain 隱藏 material assumption。
- Materiality 5/4 明顯不符合 rubric；boilerplate/普通 update 被升格 M4-M5。
- Evidence grade 高估；單一 anecdote 標 A/B，或 analyst inference 被包成 proven fact。
- Materiality 與 Evidence grade 混為一談。
- customer ROI/scale/productivity 缺官方證據，或單一案例被外推。
- value-chain shift 沒有 control-point/value-capture evidence 卻硬推。
- Driver/Lever/Catalyst/Risk taxonomy 顛倒。
- guidance range、GAAP/non-GAAP、FCF taxonomy、consensus/transcript status 錯誤。
- quote_validation_issues 指向核心結論卻沒有降級/刪除。
- 遺漏 M4-M5 的重大 demand/pricing/margin/competition/unit-economics Q&A。
- numerical_errors 非空。

corrected_telegram_draft <3200字，維持 V3 標題/順序；只保留真正 M4-M5 的高價值訊號。不要資料來源段落。
Schema: {overall_score:0-100,industry_cross_check:[string],unsupported_claims:[string],numerical_errors:[string],missing_material_points:[string],misleading_inferences:[string],evidence_grade_errors:[string],materiality_score_errors:[string],cross_context_errors:[string],causal_chain_errors:[string],value_chain_errors:[string],critical_issues:[string],pass:boolean,corrected_telegram_draft:string}.
Pass=true only if overall_score>=90 and ALL error/critical arrays are empty.
Input:\n""" + json.dumps({
            "event_id": event.event_id,
            "facts": facts,
            "analysis": analysis,
            "evidence": balanced_evidence_payload(evidence),
        }, ensure_ascii=False), "auditor_v3")

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        return self._json("""依 institutional auditor 結果修訂 V3 buy-side earnings analysis。只使用既有 facts；不得新增事實。
移除 unsupported/misleading claim，修正 M1-M5、A-D、cross-context synthesis、causal chain、value-chain shift、customer ROI、Driver/Lever/Catalyst/Risk、guidance/accounting/consensus/Q&A。證據不足就降級或刪除。
只回傳與 V3 analyst 相同 schema；telegram_draft 保留 V3 結構、<3200字，不輸出資料來源。
Input:\n""" + json.dumps({"facts": facts, "analysis": analysis, "audit": audit}, ensure_ascii=False), "revision_v3")
