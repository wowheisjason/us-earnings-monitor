from us_earnings_monitor.report_contract import structure_errors


def test_fullwidth_colons_are_accepted_for_v4_headings():
    report = """AVGO FY2026 Q3
💡 投資結論與邏輯：
• x
📊 關鍵數據與財測：
• y
🧭 營運動能與法說攻防：
• z
⚠️ 風險、反證與待驗證：
• r
"""
    assert structure_errors(report) == []
