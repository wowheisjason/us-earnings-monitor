from us_earnings_monitor.validation import validate_extracted_facts


def test_valid_guidance_range_and_adjusted_fcf_reconciliation_pass():
    facts = {
        "market_consensus": [],
        "guidance": [{"low": 190.0, "midpoint": 192.0, "high": 194.0}],
        "cash_flow_and_capex": [{
            "metric": "Adjusted free cash flow",
            "metric_type": "adjusted_fcf",
            "reconciliation": [{"item": "financing receivables", "value": 6.667}],
        }],
    }
    assert validate_extracted_facts(facts) == []


def test_midpoint_and_adjusted_metric_issues_are_rejected():
    facts = {
        "market_consensus": [{"metric": "revenue", "value": 1}],
        "guidance": [{"low": 190.0, "midpoint": 193.0, "high": 194.0}],
        "cash_flow_and_capex": [{
            "metric": "Adjusted free cash flow",
            "metric_type": "standard_fcf",
            "reconciliation": [],
        }],
    }
    issues = validate_extracted_facts(facts)
    assert "market_consensus_present_without_external_consensus_provider" in issues
    assert "guidance[0]_midpoint_not_range_average" in issues
    assert "cash_flow_and_capex[0]_adjusted_metric_taxonomy_mismatch" in issues
    assert "cash_flow_and_capex[0]_adjusted_fcf_mislabeled_standard" in issues
    assert "cash_flow_and_capex[0]_adjusted_metric_missing_reconciliation" in issues
