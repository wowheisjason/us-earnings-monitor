from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from us_earnings_monitor import __main__ as runner
from us_earnings_monitor.checkpointing import evidence_fingerprint, prepare_checkpoint
from us_earnings_monitor.models import Disclosure, EarningsEvent, Evidence
from us_earnings_monitor.state import StateStore


EMPTY_FACTS = {
    "facts": [],
    "guidance": [],
    "segments": [],
    "cash_flow_and_capex": [],
    "quantitative_evidence": [],
    "change_signals": [],
    "customer_cases": [],
    "industry_signals": [],
    "management_claims": [],
    "management_comments": [],
    "qa": [],
    "market_consensus": [],
    "unknowns": [],
}

PASS_AUDIT = {
    "overall_score": 95,
    "unsupported_claims": [],
    "numerical_errors": [],
    "missing_material_points": [],
    "misleading_inferences": [],
    "evidence_grade_errors": [],
    "materiality_score_errors": [],
    "cross_context_errors": [],
    "causal_chain_errors": [],
    "value_chain_errors": [],
    "critical_issues": [],
    "pass": True,
    "corrected_telegram_draft": "SNOW FY2027 Q2\n\n💡 核心投資結論:\n• 測試報告",
}


class FakeExtractor:
    def fetch(self, document):
        return Evidence(
            document_key=document.key,
            title=document.title,
            url=document.url,
            text="official evidence body",
            structured_facts=[],
        )


class AuditFailsOnceClient:
    def __init__(self):
        self.extract_calls = 0
        self.analyze_calls = 0
        self.audit_calls = 0
        self.revise_calls = 0

    def extract_facts(self, event, evidence):
        self.extract_calls += 1
        return dict(EMPTY_FACTS)

    def analyze(self, event, facts, evidence):
        self.analyze_calls += 1
        return {"telegram_draft": PASS_AUDIT["corrected_telegram_draft"]}

    def audit(self, event, facts, analysis, evidence):
        self.audit_calls += 1
        if self.audit_calls == 1:
            raise RuntimeError("503 auditor unavailable")
        return dict(PASS_AUDIT)

    def revise(self, facts, analysis, audit):
        self.revise_calls += 1
        return analysis

    def material_update(self, facts, previous_count, current_count):
        return True


class RevisionAuditFailsOnceClient(AuditFailsOnceClient):
    def audit(self, event, facts, analysis, evidence):
        self.audit_calls += 1
        if self.audit_calls == 1:
            return {
                **PASS_AUDIT,
                "overall_score": 80,
                "critical_issues": ["needs revision"],
                "pass": False,
            }
        if self.audit_calls == 2:
            raise RuntimeError("503 revision auditor unavailable")
        return dict(PASS_AUDIT)

    def revise(self, facts, analysis, audit):
        self.revise_calls += 1
        return {"telegram_draft": PASS_AUDIT["corrected_telegram_draft"] + " revised"}


def build_store(tmp_path):
    state_path = tmp_path / "state.json"
    store = StateStore(state_path)
    doc = Disclosure(
        source="sec_edgar",
        source_id="snow-q2",
        ticker="SNOW",
        title="Snowflake financial results",
        published_at="2026-09-02T16:08:29-04:00",
        url="https://www.sec.gov/example",
        document_kind="financial_results",
        fiscal_year=2027,
        quarter="Q2",
        period_end="2026-07-31",
    )
    event = EarningsEvent(
        event_id="SNOW_2026-07-31_Q2",
        ticker="SNOW",
        fiscal_year=2027,
        quarter="Q2",
        first_seen_at="2026-09-02T16:08:29-04:00",
        period_end="2026-07-31",
        documents=[doc.key],
        collection_status={
            "official_ir_checked_at": "2026-09-02T20:30:00-04:00",
            "transcript_status": "NOT_FOUND_AFTER_RETRY",
        },
    )
    store.add_document(doc)
    store.put_event(event)
    return store, event


def test_state_store_reads_legacy_state_without_checkpoint_key(tmp_path):
    path = tmp_path / "legacy.json"
    path.write_text('{"schema_version":1,"documents":{},"events":{}}', encoding="utf-8")
    store = StateStore(path)
    assert store.get_analysis_checkpoint("missing") == {}
    assert store.data["analysis_checkpoints"] == {}


def test_evidence_fingerprint_invalidates_same_url_when_content_changes():
    first = [Evidence("doc", "Title", "https://example.com/report", "version one", [])]
    second = [Evidence("doc", "Title", "https://example.com/report", "version two", [])]
    first_hash = evidence_fingerprint(first)
    second_hash = evidence_fingerprint(second)
    assert first_hash != second_hash
    checkpoint, invalidated = prepare_checkpoint(
        {"pipeline_version": 1, "evidence_fingerprint": first_hash, "stages": {"analysis": {"payload": {}}}},
        second_hash,
    )
    assert invalidated is True
    assert checkpoint["stages"] == {}


def test_auditor_provider_failure_resumes_without_repeating_upstream_stages(tmp_path, monkeypatch):
    store, event = build_store(tmp_path)
    client = AuditFailsOnceClient()
    monkeypatch.setattr(runner, "EvidenceExtractor", FakeExtractor)
    sent = []
    monkeypatch.setattr(runner, "send_report", lambda text, parse_mode=None: sent.append(text) or 42)
    now = datetime(2026, 9, 2, 20, 30, tzinfo=ZoneInfo("America/New_York"))

    with pytest.raises(RuntimeError, match="503 auditor"):
        runner._run_analysis(event, store, client, False, now)

    checkpoint = store.get_analysis_checkpoint(event.event_id)
    assert set(checkpoint["stages"]) == {"facts", "analysis"}
    assert client.extract_calls == 1
    assert client.analyze_calls == 1
    assert client.audit_calls == 1

    outcome = runner._run_analysis(event, store, client, False, now)
    assert outcome == "published"
    assert client.extract_calls == 1
    assert client.analyze_calls == 1
    assert client.audit_calls == 2
    assert client.revise_calls == 0
    assert len(sent) == 1
    assert store.get_analysis_checkpoint(event.event_id) == {}


def test_revision_auditor_failure_resumes_from_revision_only(tmp_path, monkeypatch):
    store, event = build_store(tmp_path)
    client = RevisionAuditFailsOnceClient()
    monkeypatch.setattr(runner, "EvidenceExtractor", FakeExtractor)
    monkeypatch.setattr(runner, "send_report", lambda text, parse_mode=None: 99)
    now = datetime(2026, 9, 2, 20, 30, tzinfo=ZoneInfo("America/New_York"))

    with pytest.raises(RuntimeError, match="503 revision auditor"):
        runner._run_analysis(event, store, client, False, now)

    checkpoint = store.get_analysis_checkpoint(event.event_id)
    assert set(checkpoint["stages"]) == {"facts", "analysis", "audit", "revision_analysis"}
    assert client.extract_calls == 1
    assert client.analyze_calls == 1
    assert client.revise_calls == 1
    assert client.audit_calls == 2

    outcome = runner._run_analysis(event, store, client, False, now)
    assert outcome == "published"
    assert client.extract_calls == 1
    assert client.analyze_calls == 1
    assert client.revise_calls == 1
    assert client.audit_calls == 3
