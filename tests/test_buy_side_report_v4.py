from us_earnings_monitor.report_contract import (
    harden_audit_with_report_quality,
    redundancy_errors,
    stage_contract,
    structure_errors,
)


def _good_report() -> str:
    return """Snowflake (SNOW) FY2027 Q2

💡 投資結論與邏輯:
• 產品消費成長加速，同時 AI workload 拉高基礎設施成本；營收動能與毛利壓力同步上升。

📊 關鍵數據與財測:
產品營收 | $1.49B | +37% YoY
全年產品營收指引 | $6.07B | +36% YoY
Non-GAAP 營業利益率指引 | 14.5% | 上修 100 bps
• 外部市場共識未納入，本報告不判定 Beat/Miss。

🧭 營運動能與法說攻防:
• Q: 成長是否只由 AI-native 客戶驅動？ A: 管理層稱成長廣泛分布於大型企業；投資解讀是需求廣度優於單一客群敘事。
• Q: AI 成本是否持續壓毛利？ A: 管理層強調營運費用槓桿；仍需觀察產品毛利率是否止穩。

⚠️ 風險、反證與待驗證:
• AI compute 成本若持續快於產品效率改善，毛利率可能被長期壓低。
• 需驗證企業 optimization 是否降低後續單客戶消費強度。
"""


def test_v4_good_report_passes_deterministic_structure_gate():
    report = _good_report()
    assert structure_errors(report) == []
    assert redundancy_errors(report) == []


def test_legacy_fragmented_report_is_rejected():
    report = """SNOW FY2027 Q2
💡 核心投資結論:
• x
📈 關鍵指標:
• y
🎙️ 法說 Q&A:
• z
⚖️ 反證與未知:
• r
"""
    errors = structure_errors(report)
    assert any("missing_v4_sections" in item for item in errors)
    assert any("legacy_fragmented_sections" in item for item in errors)


def test_metric_value_repeated_across_three_sections_is_rejected():
    report = _good_report().replace(
        "• Q: 成長是否只由 AI-native 客戶驅動？",
        "• 產品營收 $1.49B +37% YoY。\n• Q: 成長是否只由 AI-native 客戶驅動？",
    ).replace(
        "• AI compute 成本若持續快於產品效率改善",
        "• 產品營收 $1.49B +37% YoY 並不保證持續。\n• AI compute 成本若持續快於產品效率改善",
    )
    errors = redundancy_errors(report)
    assert any("metric_repeated_across_sections" in item for item in errors)


def test_auditor_hardener_blocks_bad_structure():
    audit = {
        "overall_score": 97,
        "pass": True,
        "critical_issues": [],
        "corrected_telegram_draft": "SNOW FY2027 Q2\n\n💡 核心投資結論:\n• old format",
    }
    hardened = harden_audit_with_report_quality(audit)
    assert hardened["pass"] is False
    assert "deterministic_v4_gate:structure" in hardened["critical_issues"]


def test_stage_contract_applies_only_to_analysis_output_stages():
    assert "EXACTLY these four" in stage_contract("analyst_v3")
    assert "AUDITOR ADDENDUM" in stage_contract("auditor_v3")
    assert stage_contract("facts_chunk") == ""
