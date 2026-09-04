from us_earnings_monitor.investor_analysis_runtime import _normalize_pending_transcript_audit
from us_earnings_monitor.report_contract import harden_audit_with_report_quality


def test_pending_transcript_can_publish_v4_without_fabricated_qa():
    draft = """Broadcom (AVGO) FY2026 Q3

💡 投資結論與邏輯:
• AI 半導體與基礎設施需求推升營收與獲利，但 SEC 快報尚未取得逐字稿。

📊 關鍵數據與財測:
• 外部市場共識未納入，本報告不判定 Beat/Miss。

🧭 營運動能與法說攻防:
• 官方逐字稿仍待取得；本版不推測 Q&A。

⚠️ 風險、反證與待驗證:
• 待官方 IR / transcript 補齊後驗證管理層對需求與毛利率的說法。
"""
    audit = {
        "overall_score": 82,
        "pass": False,
        "missing_material_points": ["缺少法說 Q&A，因逐字稿尚未提供"],
        "misleading_inferences": [],
        "critical_issues": ["transcript Q&A not available"],
        "corrected_telegram_draft": draft,
    }
    facts = {"qa": [], "collection_status": {"transcript_status": "EXPECTED_NOT_YET_AVAILABLE"}}
    normalized = _normalize_pending_transcript_audit(audit, facts)
    hardened = harden_audit_with_report_quality(normalized)
    assert hardened["pass"] is True
    assert hardened["critical_issues"] == []
