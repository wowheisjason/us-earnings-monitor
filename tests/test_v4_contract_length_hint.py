from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_targets_concise_report():
    assert "900–1500" in V4_OUTPUT_CONTRACT
    assert "hard ceiling 1800" in V4_OUTPUT_CONTRACT
    assert "3–5 COMPACT METRIC CLUSTERS" in V4_OUTPUT_CONTRACT
