from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_targets_concise_report():
    assert "<=2600" in V4_OUTPUT_CONTRACT
    assert "at most two sections" in V4_OUTPUT_CONTRACT
