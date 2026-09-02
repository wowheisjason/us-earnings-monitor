from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.__main__ import _run_analysis
from us_earnings_monitor.models import Disclosure, EarningsEvent, Evidence
from us_earnings_monitor.state import StateStore
from us_earnings_monitor.validation import merge_cash_flow_reconciliation_repair, validate_extracted_facts


def _facts():
    return {"cash_flow_and_capex": [{"metric": "Adjusted free cash flow", "metric_type": "adjusted_fcf", "value": 8.149, "reconciliation": []}]}


def test_focused_repair_merges_reconciliation_without_overwriting_value():
    assert validate_extracted_facts(_facts()) == ["cash_flow_and_capex[0]_adjusted_metric_missing_reconciliation"]
    repaired = merge_cash_flow_reconciliation_repair(_facts(), {"cash_flow_and_capex": [{
        "index": 0, "metric_type": "adjusted_fcf", "reconciliation": [{"item": "financing receivables", "value": 6.667}],
    }]})
    assert validate_extracted_facts(repaired) == []
    assert repaired["cash_flow_and_capex"][0]["value"] == 8.149


def test_needs_review_saves_validation_and_audit_diagnostics(monkeypatch, tmp_path):
    class Client:
        def extract_facts(self, *args): return _facts()
        def repair_cash_flow_reconciliation(self, *args): return {"cash_flow_and_capex": []}
        def analyze(self, *args): return {"telegram_draft": "draft"}
        def audit(self, *args): return {"overall_score": 80, "pass": False, "critical_issues": ["reconciliation missing"], "numerical_errors": ["mismatch"], "unsupported_claims": ["claim"]}
        def revise(self, facts, analysis, audit): return analysis

    import us_earnings_monitor.__main__ as app
    monkeypatch.setattr(app, "build_analysis_client", lambda: Client())
    monkeypatch.setattr(app, "publication_gate", lambda *args: (True, [], {}))
    monkeypatch.setattr(app.EvidenceExtractor, "fetch", lambda self, doc: Evidence(doc.key, doc.title, doc.url, "official evidence"))
    store = StateStore(tmp_path / "state.json")
    doc = Disclosure("official_ir", "one", "DELL", "Transcript", "2026-09-01T00:00:00-04:00", "https://example.test/t", document_url="https://example.test/t")
    store.add_document(doc)
    now = datetime(2026, 9, 2, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    event = EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", now.isoformat(), documents=[doc.key])
    assert _run_analysis(event, store, dry_run=False, now=now) == "needs_human_review"
    status = store.get_event(event.event_id).collection_status
    assert status["deterministic_issues"] == ["cash_flow_and_capex[0]_adjusted_metric_missing_reconciliation"]
    assert status["audit_critical_issues"] == ["reconciliation missing"]
    assert status["audit_numerical_errors"] == ["mismatch"]
    assert status["audit_unsupported_claims"] == ["claim"]
