from __future__ import annotations

import json
from typing import Any

from .investor_analysis_v5 import ProductionInvestorV5Client as _BaseV5Client
from .models import EarningsEvent


_SPARSE_CARD_SCHEMA = r"""
{
  "processed_unit_ids":[string],
  "cards":[
    {
      "unit_id":string,
      "card_type":"fact"|"guidance"|"management_claim"|"qa"|"risk"|"customer"|"competitive"|"capital"|"other",
      "topic":"demand_revenue"|"guidance"|"margin_unit_economics"|"cash_capex"|"customer_usage"|"product_competition"|"supply_capacity"|"qa_management"|"risk"|"other",
      "fact_class":"objective_fact"|"management_claim"|"analyst_question"|"company_guidance"|"unknown",
      "statement":string,
      "materiality_candidate":1|2|3|4|5,
      "quote":string,
      "...ONLY_APPLICABLE_FIELDS...":"metric/value/unit/period/comparison_value/comparison_period/reported_change OR guidance low/midpoint/high/previous_* OR Q&A analyst/management_speaker/question_summary/answer_summary/answer_quality OR customer/product/use_case/outcome/quantified_result"
    }
  ]
}
"""


class ProductionInvestorV5Client(_BaseV5Client):
    """V5 mapper with sparse evidence-card serialization.

    The base V5 pipeline already guarantees full unit coverage. This override
    changes only mapper output density: irrelevant/null keys are forbidden, so a
    bounded evidence unit cannot exhaust the JSON output budget by repeating a
    wide schema for every fact.
    """

    def _extract_batch(self, event: EarningsEvent, units: list[dict[str, Any]], stage: str) -> dict:
        expected = [unit["unit_id"] for unit in units]
        return self._json(f"""You are a forensic earnings-evidence mapper for a professional US-equity investor. Return MINIMAL JSON only.

Event: {event.event_id}
Process EVERY supplied unit exactly once. Put every unit_id in processed_unit_ids even when that unit has zero material cards. Your job is source-backed evidence capture, not investment analysis.

CRITICAL OUTPUT COMPRESSION RULES:
- SPARSE JSON ONLY. OMIT every null, empty, unknown, duplicate, or inapplicable property. Never emit a key with null merely because the schema mentions it.
- Maximum 8 cards per unit. Within a unit prioritize: explicit guidance/change > forward demand/pricing/margin/supply evidence > material Q&A > customer/usage/ROI evidence > other financial facts. Do not emit low-value boilerplate.
- One card should represent one economic point. Combine tightly related values into statement when separating them would create duplicate cards, while keeping individual numeric fields only where needed downstream.
- Keep statement, question_summary, answer_summary and quote concise. quote must be a SHORT exact substring.

Evidence rules:
1. Use ONLY supplied text/structured facts. Never calculate a missing percentage, growth rate, margin, midpoint or comparison.
2. Preserve GAAP/non-GAAP/adjusted/company-defined labels and source USD $M/$B units exactly.
3. Guidance: preserve explicit low/high and prior range when present. Do not manufacture missing endpoints.
4. Distinguish objective_fact, company_guidance, management_claim and analyst_question. Prepared remarks are not Q&A.
5. Capture explicit forward drivers: demand/volume/price/mix, usage/adoption, orders/backlog/RPO, customer breadth/depth, margin/unit economics, cash/capex, capacity/supply, product/competition and risk.
6. Q&A: retain each material debate visible in the unit; answer_quality is direct/partial/evasive/unknown. Do not output procedural operator chatter.
7. Customer evidence: keep customer/use case/product/outcome/quantified result only when explicit; never infer ROI.
8. materiality_candidate is triage only: 5=forward earnings/thesis-changing; 4=material guidance/driver/Q&A; 3=useful corroboration. Normally omit 1-2 cards entirely.
9. Third-party transcript is qualitative management/Q&A evidence only; do not create primary financial fact cards from it.

Applicable optional fields by card type (OMIT all others):
- fact/capital/competitive/risk: metric,value,unit,period,comparison_value,comparison_period,reported_change
- guidance: metric,period,low,midpoint,high,unit,previous_low,previous_midpoint,previous_high,reported_change
- qa: analyst,management_speaker,question_summary,answer_summary,answer_quality
- customer: customer,product,use_case,outcome,quantified_result
- management_claim: speaker

Expected unit ids: {json.dumps(expected, ensure_ascii=False)}
Schema pattern:
{_SPARSE_CARD_SCHEMA}
Units:
{json.dumps(self._unit_payload(units), ensure_ascii=False)}
""", stage)
