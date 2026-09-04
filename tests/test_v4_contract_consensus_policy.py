from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_never_invents_consensus_or_valuation():
    lower = V4_OUTPUT_CONTRACT.lower()
    assert "never invent consensus" in lower
    assert "valuation" in lower
