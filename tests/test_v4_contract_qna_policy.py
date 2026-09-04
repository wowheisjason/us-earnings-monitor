from us_earnings_monitor.report_contract import V4_OUTPUT_CONTRACT


def test_v4_contract_requires_multiple_debate_topics_when_available():
    assert "2–3 DIFFERENT debate topics" in V4_OUTPUT_CONTRACT
