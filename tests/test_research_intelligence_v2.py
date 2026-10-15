from __future__ import annotations

from pathlib import Path

from us_earnings_monitor.research_intelligence import (
    RESEARCH_EXTRACTION, RESEARCH_PLAYBOOK, RESEARCH_AUDIT_CHECKLIST,
)


def test_research_contract_retains_key_verification_boundaries():
    for term in ("cleanroom", "ASP", "ORDER QUALITY", "TECHNOLOGY ADOPTION", "Q&A", "DELTA"):
        assert term.casefold() in (RESEARCH_EXTRACTION + RESEARCH_PLAYBOOK + RESEARCH_AUDIT_CHECKLIST).casefold()
    assert "分析師" in RESEARCH_AUDIT_CHECKLIST
    assert "前次指引" in RESEARCH_PLAYBOOK
    assert "不能" in RESEARCH_AUDIT_CHECKLIST


def test_monitor_schedule_checks_every_day():
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "monitor.yml").read_text(encoding="utf-8")
    assert 'cron: "5 0,4,8,12,16,20 * * *"' in workflow
    assert 'timezone: "' in workflow
