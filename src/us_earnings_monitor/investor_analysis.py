from __future__ import annotations

import json

from .gemini_v2 import GeminiV2Client
from .models import EarningsEvent, Evidence


class InvestorFrameworkGeminiClient(GeminiV2Client):
    """Buy-side oriented analysis layer with change detection and evidence grading.

    Retrieval and publication gates remain unchanged. This class upgrades only
    the evidence schema, investment reasoning, Telegram report structure, and
    skeptical audit criteria.
    """

    def extract_facts(self, event: EarningsEvent, evidence: list[Evidence]) -> dict:
        return self._json("""You are a strict evidence extractor for US-listed-company earnings. Return JSON only.
Use ONLY the supplied official evidence. Do not estimate, infer, invent, or silently normalize company-defined metrics. Missing values must be null.
Every extracted item must include evidence.document_key and a short supporting quote or structured concept.

The purpose is to give a second-stage professional investor enough structured evidence to identify what CHANGED, what is economically MATERIAL, and what remains unproven.

Rules:
1. Preserve GAAP, non-GAAP, adjusted, and company-defined metric labels exactly. Never call Adjusted FCF simply FCF.
2. Guidance must preserve the COMPLETE range when present: low, midpoint, high, unit, period, comparison, previous guidance, and reported change. A midpoint alone is not the full guidance if a range exists.
3. Cash flow must distinguish operating_cash_flow, standard_fcf, adjusted_fcf, capex, and other. Extract official reconciliation components for adjusted metrics when present.
4. Extract current-period and prior-period/comparison values separately when explicitly disclosed. Do not calculate missing YoY/QoQ yourself.
5. change_signals may compare current vs prior period or current vs prior guidance ONLY when both are explicitly supported by the supplied evidence. Never invent a cross-quarter management-language change when prior-quarter language is not in evidence.
6. quantitative_evidence must capture not only financial metrics but also adoption, usage, customer count, transaction/record scale, migration speed, cost savings, productivity/time savings, and other explicit ROI evidence.
7. customer_cases must separate customer/problem, workload or use case, product used, outcome, and quantified result. Do not infer ROI that the company did not quantify.
8. Separate objective facts from management claims. A statement such as 'AI is a structural multiplier' is a management_claim unless independently quantified in the evidence.
9. Q&A may be embedded inside a Transcript. Extract material analyst questions on demand, pricing, supply, margin, guidance, customer, inventory, competition, capex, unit economics, model/product mix, risk, or other thesis-relevant topics. Preserve analyst and management speaker names when available.
10. Market consensus is NOT company guidance. market_consensus must be [] unless explicit external consensus data is actually present in the supplied evidence.

Schema:
{
  event_id,
  company_name,
  facts:[{metric,value,unit,period,comparison_value,comparison_period,reported_change,evidence:{document_key,quote}}],
  guidance:[{metric,period,low,midpoint,high,unit,reported_yoy,previous_low,previous_midpoint,previous_high,change,evidence:{document_key,quote}}],
  segments:[{name,metric,value,unit,period,comparison_value,reported_change,evidence:{document_key,quote}}],
  cash_flow_and_capex:[{metric,metric_type,value,unit,period,reported_change,reconciliation:[{item,value,unit}],evidence:{document_key,quote}}],
  quantitative_evidence:[{category:"financial"|"adoption"|"usage"|"roi"|"scale"|"productivity"|"other",metric,value,unit,period,reported_change,evidence:{document_key,quote}}],
  change_signals:[{dimension,current_state,prior_state,reported_change,direction:"improving"|"deteriorating"|"mixed"|"unchanged"|"unknown",evidence:{document_key,quote}}],
  customer_cases:[{customer,problem,use_case,product,outcome,quantified_result,evidence:{document_key,quote}}],
  industry_signals:[...],
  management_claims:[{topic,claim,evidence:{document_key,quote}}],
  management_comments:[{topic,statement,evidence:{document_key,quote}}],
  qa:[{category,question_summary,answer_summary,analyst,management_speaker,evidence:{document_key,quote}}],
  market_consensus:[],
  unknowns:[...]
}
Evidence:\n""" + self._evidence(event, evidence), "facts")

    def analyze(self, event: EarningsEvent, facts: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是專業 buy-side 科技股投資人兼該企業所屬產業專家。請以台灣繁體中文撰寫並只回傳 JSON；正式公司、產品與產業術語保留英文原文。

你的工作不是做一般財報摘要，而是找出會改變未來 1–3 年 revenue growth、margin structure、competitive position、capital intensity 或 earnings power 的高 materiality 訊號。只能使用 structured facts 與 collection_status；不得加入外部資料、目標價或買賣建議。

分析框架：
1. FACT → CHANGE → MATERIALITY → CAUSAL CHAIN → SKEPTICAL CHECK。客觀事實、management claim、analyst inference 必須明確分開。
2. Change Detection：優先比較本期 vs evidence 中明確存在的 prior period / prior guidance。若沒有跨季 management wording evidence，不得宣稱管理層語氣轉強或轉弱。
3. Materiality Filter：只分析本季有新證據的維度；不要為了填欄位而強迫產生 moat/TAM/catalyst insight。一般 PR、產品清單與客套話除非有實質經濟含義，否則刪除。
4. Quantitative Evidence：優先保留 adoption、usage、customer count、ROI、cost saving、migration/productivity、規模等可驗證數字，而不只 P&L headline。
5. Customer Proof：用 Problem → Workload/Product → Outcome → Quantified ROI 的方式判斷案例是否真正證明 economic value；單一案例不得外推成整體趨勢。
6. Causal Chain：只有 evidence 足以支撐時，才建立 Product/Adoption → Workload/Usage → Revenue/Consumption → Cost → GM/OM/FCF 的因果鏈。相關性不得直接寫成因果。
7. Q&A Priority：若有 Q&A，優先挑最能揭露 unit economics、demand durability、pricing、competition、model/product mix、margin、guidance、customer behavior 的 1–3 組，而不是平均摘要所有問答。
8. Driver / Lever / Catalyst / Risk 必須分清楚：Driver=基本面驅動因子；Lever=管理層可操作的改善手段；Catalyst=可能觸發市場重新定價的未來事件；Risk=thesis failure condition。不可把 cost-optimization lever 誤稱 catalyst。
9. Evidence Strength：A=官方量化且直接支持；B=多項官方證據相互支持但未完全量化；C=單一 management/customer anecdote；D=analyst inference。推論的 grade 取決於最弱證據，不可過度評級。
10. Skeptical Check：每個重大 management claim 都要問：證據是什麼？還缺什麼？是否存在 alternative explanation？「plausible」不可寫成「proven」。
11. Consensus 與公司 guidance 分開。若沒有外部 consensus provider/data，固定寫「未納入外部市場共識，因此不判定 Beat/Miss」。
12. Guidance 若 facts 同時有 low/midpoint/high，必須呈現完整 range；GAAP/non-GAAP、standard FCF/Adjusted FCF/OCF 必須清楚區分。
13. collection_status.transcript_status=FOUND：使用官方 Transcript/Q&A；=NOT_FOUND_AFTER_RETRY：只能寫「截至本次自動蒐集截止，未取得官方 Transcript/Q&A」；=CONFIRMED_NOT_PUBLISHED 才可寫官方未發布；=EXPECTED_NOT_YET_AVAILABLE 時不得生成正式報告。

Schema:
{
  facts:[string],
  material_signals:[{signal,materiality:"high"|"medium"|"low",evidence_grade:"A"|"B"|"C"|"D",why_it_matters}],
  change_detection:[{change,evidence_grade:"A"|"B"|"C"|"D",investment_effect}],
  causal_chains:[{chain,evidence_grade:"A"|"B"|"C"|"D",weak_link}],
  evidence_assessment:[{claim,evidence,grade:"A"|"B"|"C"|"D",missing_evidence}],
  investor_interpretation:[string],
  investment_implications:[string],
  risks_and_unknowns:[string],
  confidence:0-100,
  telegram_draft:string
}

telegram_draft 必須少於 3200 字、資訊密度高、以 analyst earnings notebook 風格呈現；每區最多保留最 material 的內容。沒有證據就明確寫不足，不要硬補 insight。不要輸出資料來源段落，程式會附官方連結。美元金額維持 facts 中驗證過的 USD million/billion 單位，不轉換為中文億/兆。

嚴格使用以下順序與標題：

{公司/股票代碼} {FY季度}

💡 核心投資結論:
• [A/B/C/D] ...

🔄 本季變化:
• [A/B/C/D] ...

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

🏢 業務部門:
• ...
└ 客戶/ROI: ...

🔗 因果鏈與單位經濟:
• ... → ... → ...

🔮 未來展望:
• [Driver] ...
• [Lever] ...
• [Catalyst] ...

🎙️ 法說 Q&A:
❓ ...
💬 ...

⚖️ 反證與未知:
• [Claim→Evidence] ...
• [Missing] ...
• [Risk] ...

Structured facts:\n""" + json.dumps({"event_id": event.event_id, "facts": facts}, ensure_ascii=False), "analyst")

    def audit(self, event: EarningsEvent, facts: dict, analysis: dict, evidence: list[Evidence]) -> dict:
        return self._json("""你是該企業所屬產業的資深產業專家與 reject-oriented buy-side evidence auditor。請以台灣繁體中文撰寫並只回傳 JSON。
你的任務是主動找出足以拒絕發布的問題，而不是替第一次分析找理由通過。逐項把數字、change detection、因果鏈、customer ROI、財測、Q&A、evidence grade 與措辭對回 ORIGINAL official evidence。

以下任何 critical issue 存在時 pass 必須為 false：
- unsupported number/claim、引用證據不匹配，或 management claim 被寫成客觀事實。
- change detection 使用 evidence 中不存在的 prior-quarter 數字、prior guidance 或管理層措辭。
- 由相關性直接推成因果，或 causal chain 有未揭露的中間假設卻以確定語氣呈現。
- A/B/C/D evidence grade 明顯高估；單一 anecdote 被標 A/B，或 inference 被包裝成 proven fact。
- customer ROI / cost saving / productivity 數字沒有 official evidence，或把單一 customer case 外推成整體趨勢。
- Guidance 有 range 卻只呈現 midpoint；GAAP/non-GAAP、standard FCF/Adjusted FCF/OCF 混淆；重要 reconciliation 遺漏。
- Driver / Lever / Catalyst / Risk taxonomy 顛倒，且會影響投資判讀（例如把 margin optimization lever 當作已確定 catalyst）。
- Transcript/Q&A 已在 evidence 中但報告說沒有，或 collection status 措辭錯誤。
- market consensus 與 company guidance 混為一談；沒有外部 consensus 卻宣稱 Beat/Miss。
- 高 materiality 的 demand/pricing/margin/competition/unit-economics Q&A 或 quantified evidence 被漏掉，導致結論失真。
- risks_and_unknowns 沒有指出重大 management claim 尚缺的驗證資料，造成 plausible 被寫成 proven。
- numerical_errors 非空。

corrected_telegram_draft 必須沿用完整標題、emoji、欄位順序與簡易表格格式，少於 3200 字；只留下最 material 的 2–4 個訊號，不要以更多文字掩蓋證據不足。不要輸出資料來源段落。
Schema: {overall_score:0-100, industry_cross_check:[string], unsupported_claims:[string], numerical_errors:[string], missing_material_points:[string], misleading_inferences:[string], evidence_grade_errors:[string], causal_chain_errors:[string], critical_issues:[string], pass:boolean, corrected_telegram_draft:string}.
Pass can only be true if overall_score>=90 and unsupported_claims, numerical_errors, evidence_grade_errors, causal_chain_errors, and critical_issues are all empty.
Input:\n""" + json.dumps({"event_id": event.event_id, "facts": facts, "analysis": analysis,
                                      "evidence": json.loads(self._evidence(event, evidence))}, ensure_ascii=False), "auditor")

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        return self._json("""依稽核結果修訂台灣繁體中文 buy-side earnings analysis。移除 unsupported/misleading claim，修正 change detection、因果鏈、evidence grade、customer ROI、Driver/Lever/Catalyst/Risk taxonomy、guidance range/midpoint、GAAP/non-GAAP、FCF taxonomy、Consensus 與 Transcript 狀態措辭。不得增加新事實。
只回傳相同 analyst JSON schema。telegram_draft 必須保留指定完整標題、emoji、欄位順序與簡易表格，少於 3200 字，不輸出資料來源段落；證據不足時降級措辭或刪除，不要硬湊 insight。
Input:\n""" + json.dumps({"facts": facts, "analysis": analysis, "audit": audit}, ensure_ascii=False), "revision")
