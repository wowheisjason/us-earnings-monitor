from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_targets_concise_report():
    assert "1900–2300" in V4_OUTPUT_CONTRACT
    assert "hard ceiling 2600" in V4_OUTPUT_CONTRACT
    assert "4–7 COMPACT METRIC CLUSTERS" in V4_OUTPUT_CONTRACT
