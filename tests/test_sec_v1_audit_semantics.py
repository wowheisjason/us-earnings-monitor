from us_earnings_monitor.investor_analysis_runtime import _normalize_pending_transcript_audit, _remove_qa_section


def test_sec_only_pending_transcript_does_not_require_qa():
    audit = {
        "overall_score": 80,
        "pass": False,
        "unsupported_claims": [],
        "numerical_errors": [],
        "missing_material_points": ["缺少具投資意義的法說 Q&A 內容"],
        "misleading_inferences": [],
        "evidence_grade_errors": [],
        "materiality_score_errors": [],
        "cross_context_errors": [],
        "causal_chain_errors": [],
        "value_chain_errors": [],
        "critical_issues": ["Telegram 含目前未取得 Q&A 的佔位文字"],
        "corrected_telegram_draft": "AVGO\n\n🎙️ 法說 Q&A:\n目前取得的可驗證資料未包含 Q&A\n\n⚖️ 反證與未知:\n• IR still pending",
    }
    facts = {"qa": [], "collection_status": {"transcript_status": "EXPECTED_NOT_YET_AVAILABLE"}}
    normalized = _normalize_pending_transcript_audit(audit, facts)
    assert normalized["pass"] is True
    assert normalized["overall_score"] >= 90
    assert normalized["critical_issues"] == []
    assert normalized["missing_material_points"] == []
    assert "🎙️ 法說 Q&A" not in normalized["corrected_telegram_draft"]
    assert "⚖️ 反證與未知" in normalized["corrected_telegram_draft"]


def test_found_transcript_still_requires_qa():
    audit = {
        "overall_score": 80,
        "pass": False,
        "missing_material_points": ["缺少具投資意義的法說 Q&A 內容"],
        "critical_issues": ["缺少 Q&A"],
    }
    facts = {"qa": [], "collection_status": {"transcript_status": "FOUND"}}
    normalized = _normalize_pending_transcript_audit(audit, facts)
    assert normalized["pass"] is False
    assert normalized["critical_issues"]


def test_remove_qa_section_preserves_following_risk_section():
    text = "Core\n\n🎙️ 法說 Q&A：\n無資料\n\n⚖️ 反證與未知:\n• risk"
    cleaned = _remove_qa_section(text)
    assert "法說 Q&A" not in cleaned
    assert "⚖️ 反證與未知" in cleaned
