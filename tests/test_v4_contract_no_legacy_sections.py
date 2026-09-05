from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_explicitly_overrides_legacy_template():
    assert "overrides every older" in V4_OUTPUT_CONTRACT
    assert "output EXACTLY these four top-level sections" in V4_OUTPUT_CONTRACT
