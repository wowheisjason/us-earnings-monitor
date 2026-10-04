from us_earnings_monitor.investor_analysis_v5 import ProductionInvestorV5Client
from us_earnings_monitor.validation import validate_extracted_facts
from us_earnings_monitor.models import EarningsEvent


def test_adjusted_cash_projection_preserves_reconciliation():
    cards = [{"card_type": "capital", "topic": "cash_capex", "metric": "Adjusted FCF",
              "value": 8.149, "unit": "$B", "metric_type": "adjusted_fcf",
              "reconciliation": [{"item": "financing receivables", "value": 6.667}]}]
    facts = ProductionInvestorV5Client._compatibility_views(cards)
    assert facts["cash_flow_and_capex"][0]["reconciliation"] == cards[0]["reconciliation"]
    assert validate_extracted_facts(facts) == []


def test_one_sided_guidance_requires_explicit_source_wording():
    row = {"low": 50, "guidance_type": "lower_bound", "evidence": {"quote": "revenue at least $50B"}}
    assert validate_extracted_facts({"guidance": [row]}) == []
    row["evidence"]["quote"] = "revenue $50B"
    assert validate_extracted_facts({"guidance": [row]}) == ["guidance[0]_incomplete_range"]


def test_model_cannot_overrule_unverified_quotes():
    client = ProductionInvestorV5Client(api_key="test")
    client._json = lambda *a, **k: {"pass": True, "overall_score": 100, "critical_issues": []}
    facts = {"research_packet": {"coverage": {"complete": True, "coverage_ratio": 1}},
             "quote_validation_issues": ["quote_not_found"]}
    event = EarningsEvent("test", "TEST", 2027, "Q1", "2026-10-02T20:00:00+00:00")
    audit = client.audit(event, facts, {}, [])
    assert audit["pass"] is False
    assert "deterministic_v5_gate:unverified_source_quotes" in audit["critical_issues"]
