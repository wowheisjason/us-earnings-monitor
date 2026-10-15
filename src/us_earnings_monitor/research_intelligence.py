"""Evidence-first deep-research rules shared by extractor, analyst and auditor.

This module deliberately contains no broker credentials, network access, model
selection or delivery behavior. It can be reused by a future moomoo news intake.
"""

RESEARCH_EXTRACTION = """
SEMICONDUCTOR / INDUSTRIAL EARNINGS EVIDENCE (apply only when relevant):
- Capture supply vs demand explicitly: customer fab/cleanroom buildout, equipment/parts capacity, lead time, production ramp and the YEAR or quarter each becomes binding. Do not equate one bottleneck with another.
- For pricing: distinguish negotiated vs announced vs realized increases, ASP vs mix, margin timing, and whether price or volume drove an outcome.
- Record order quality and cancellation terms; distinguish management assurance from independent confirmation. Do not claim absence of double ordering from prepayments alone.
- Capture customer/technology milestones as design-in, qualification, pilot, high-volume manufacturing or roadmap, together with named customers, dates and attribution.
- Preserve analyst QUESTION vs management ANSWER, including hypothetical premises, denials and uncertainty. An analyst question is not evidence that its premise is true.
- Record the latest explicit comparison to prior quarter/year or prior guidance; if none exists, mark comparison unavailable.
"""

RESEARCH_PLAYBOOK = """
額外採用 Earnings Call Intelligence；依公司產業與證據量選取重點，不要強迫每家公司涵蓋全部項目：
1. SUPPLY / DEMAND：區分設備供應商自身產能、上游供應、客戶 fab/cleanroom 建置、交期、認證時程。精確註明已發生的限制與未來可能限制，不能混淆。
2. PRICING / PROFITABILITY：區分漲價已談定、實際生效、會計認列和完整反映的年度；ASP、產品組合及毛利率要分開。不能只由高毛利率推定 pricing power。
3. ORDER QUALITY：若 Q&A 涉及提前訂貨、取消罰則、預付款或 backlog，說明管理層回應、能證明的範圍與尚不能排除的風險。
4. TECHNOLOGY ADOPTION：將研發→驗證/qualification→試產→量產→擴量按具體客戶和時間排列；技術可用不代表已有量產訂單。
5. CUSTOMER / SUPPLY CHAIN：說明客戶/區域/Logic/Memory/產品組合的差異；只在具備客戶採購、訂單或可核對的用量證據時推斷特定上游供應商受益。
6. MANAGEMENT Q&A：優先具體且可能改變投資 thesis 的分析師質疑；準確呈現提問、答覆品質及有沒有回答核心問題，保留反證。
7. DELTA：已提供前值或前次指引才比較；沒有就說資料不足，不得自行斷言出現新的加速或轉折。
8. INVESTMENT HORIZON：重要催化劑與風險分清短期（<=3個月）、中期（3–12個月）、長期（超過12個月）；僅在有可核實時間點時納入，不得給交易指令。
產業資料不足時保留真正重要的財務/營運/Q&A，不要為了套框架杜撰產業鏈或客戶。維持既有 Telegram 四區及嚴格字數契約。
"""

RESEARCH_AUDIT_CHECKLIST = """
Earnings Call Intelligence 專項審核：
- 若分析聲稱「目前 cleanroom/產能/供應鏈已構成瓶頸」，來源必須直接確認目前瓶頸，不能把未來擴建風險寫成當前事實。
- 若談訂單沒有灌水，預付款、取消成本或管理層信心只是佐證/主張，不能冒充獨立證據。
- 若談供應商直接受益，須有明確採購、訂單或已驗證的供應關係；產業共同成長不足以推出確定性供應商收益。
- 客戶試用、qualification、pilot、量產的狀態與年份不得混寫；漲價協商、實現和完整反映年度也須區分。
- 檢查分母是否為公司總營收、系統營收或單一業務營收；forward guidance 不得寫成已實現。
- 分析師 question 的假設不等於管理層說法；不回答或部分回答不得改寫成肯定結論。
缺關鍵原始證據時要求刪除或加清楚限制，保留其他已驗證內容；既有來源與財務審核規則不可放寬。
"""
