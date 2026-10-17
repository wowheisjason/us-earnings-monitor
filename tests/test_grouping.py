from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.grouping import align_companion_periods, attach, event_id, infer_period, ready_for_analysis
from us_earnings_monitor.models import Disclosure, EarningsEvent


def disclosure(title: str, source_id: str = "a", **kwargs) -> Disclosure:
    return Disclosure("fixture", source_id, "SPCX", title, "2026-08-04T16:00:00-04:00", "https://example.test/a", **kwargs)


def test_multiple_documents_share_one_event():
    now = datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    first = disclosure("SpaceX Form 10-Q — Q2 2026 Financial Results", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    second = disclosure("SpaceX Form 8-K — Q2 2026 Earnings Presentation", "b", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    assert event_id(first) == "SPCX_2026-06-30_Q2"
    event = attach(None, first, now)
    event = attach(event, second, now)
    assert event is not None and event.event_id == "SPCX_2026-06-30_Q2"
    assert event.documents == ["fixture:a", "fixture:b"]


def test_period_and_missing_event_never_analyzes():
    assert infer_period(disclosure("FY2026 Q2 Financial Results")) == (2026, "Q2")
    assert not ready_for_analysis(None, datetime(2026, 8, 4, 17, 30, tzinfo=ZoneInfo("America/New_York")))
    assert not ready_for_analysis(None, datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York")))


def test_dell_worded_fiscal_period_title():
    assert infer_period(disclosure("Dell Technologies Fiscal Year 2027 Second Quarter Results")) == (2027, "Q2")


def test_event_requires_official_ir_check_instead_of_fixed_20h_clock():
    event = EarningsEvent("SPCX_2026-06-30_Q2", "SPCX", 2026, "Q2", "2026-08-04T16:00:00-04:00", period_end="2026-06-30")
    assert not ready_for_analysis(event, datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York")))
    event.collection_status = {
        "official_ir_checked_at": "2026-08-04T17:00:00-04:00",
        "transcript_status": "EXPECTED_NOT_YET_AVAILABLE",
    }
    assert ready_for_analysis(event, datetime(2026, 8, 4, 20, 1, tzinfo=ZoneInfo("America/New_York")))


def test_annual_period():
    assert infer_period(disclosure("FY2026 Annual Results")) == (2026, "Q4")


def test_same_day_earnings_release_uses_10q_anchor_period():
    result = disclosure("SpaceX Form 10-Q — Q2 2026 Financial Results", "result", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    release = disclosure("SpaceX Form 8-K — Earnings Release", "release")
    align_companion_periods([result, release])
    assert event_id(result) == "SPCX_2026-06-30_Q2"
    assert event_id(release) == "SPCX_2026-06-30_Q2"


def test_healthy_ir_check_with_pending_qa_is_ready_immediately():
    now = datetime(2026, 8, 4, 16, 5, tzinfo=ZoneInfo("America/New_York"))
    event = EarningsEvent("event", "SPCX", 2026, "Q2", "2026-08-04T16:00:00-04:00",
        collection_status={"official_ir_checked_at": now.isoformat(), "transcript_status": "EXPECTED_NOT_YET_AVAILABLE"})
    assert ready_for_analysis(event, now)


def _legacy_mu():
    event = EarningsEvent("MU_2026-09-30", "MU", 2027, None, "2026-09-30T16:00:00-04:00",
                          status="needs_human_review", period_end="2026-09-30")
    doc = Disclosure("sec_edgar", "mu", "MU", "Micron Earnings Release", event.first_seen_at,
        "https://www.sec.gov/Archives/a2026q4ex991-pressrelease.htm", fiscal_year=2027,
        document_kind="financial_results", metadata={"document_type": "EX-99.1"})
    return event, doc


def test_legacy_incomplete_period_repaired_from_explicit_sec_exhibit_without_identity_change():
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    event, doc = _legacy_mu()
    assert repair_incomplete_event_period(event, [doc], [_reported_mu(doc)])
    assert (event.fiscal_year, event.quarter) == (2026, "Q4")
    assert event.event_id == "MU_2026-09-30"
    assert event.period_end == "2026-09-30"  # No unproven end-date guessed.
    assert event.delivery == {} and event.report_version == 0
    assert event.collection_status["period_repair"]["period_end_verified"] is False


def test_period_repair_rejects_published_or_conflicting_or_guidance_only():
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    event, doc = _legacy_mu()
    event.report_version = 1
    assert not repair_incomplete_event_period(event, [doc])
    event.report_version = 0
    event.delivery = {"status": "sending"}
    assert not repair_incomplete_event_period(event, [doc])
    event.delivery = {}
    other = Disclosure("sec_edgar", "other", "MU", "Earnings Release", doc.published_at,
        "https://www.sec.gov/Archives/a2026q3ex991-pressrelease.htm", document_kind="financial_results",
        metadata={"document_type": "EX-99.1"})
    assert not repair_incomplete_event_period(event, [doc, other])
    doc.url = "https://www.sec.gov/Archives/guidance2026q4.htm"
    doc.title = "FY2026 Q4 Guidance"
    assert not repair_incomplete_event_period(event, [doc])


def test_repaired_legacy_event_keeps_new_companion_without_duplicate_event(tmp_path):
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    from us_earnings_monitor.state import StateStore
    from us_earnings_monitor import __main__ as runner
    event, doc = _legacy_mu()
    assert repair_incomplete_event_period(event, [doc], [_reported_mu(doc)])
    store = StateStore(tmp_path / "state.json")
    store.put_event(event)
    companion = Disclosure("official_ir", "call", "MU", "Micron earnings transcript", doc.published_at,
        "https://investors.micron.com/call", fiscal_year=2026, quarter="Q4",
        period_end="2026-09-30", document_kind="transcript")
    runner.ingest([companion], store, [], datetime.fromisoformat(event.first_seen_at))
    assert [item.event_id for item in store.all_events()] == [event.event_id]
    assert companion.key in store.get_event(event.event_id).documents


def _reported_mu(doc, text="Micron Technology Reports Results for the Fourth Quarter and Full Year of Fiscal 2026"):
    from us_earnings_monitor.models import Evidence
    return Evidence(doc.key, doc.title, doc.url, text)


def test_conflicting_year_requires_actual_reported_header_not_filename_or_guidance():
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    event, doc = _legacy_mu()
    assert not repair_incomplete_event_period(event, [doc])
    assert (event.fiscal_year, event.quarter) == (2027, None)
    guidance = _reported_mu(doc, "Guidance: We expect FQ4 2026 financial results to improve.")
    assert not repair_incomplete_event_period(event, [doc], [guidance])
    unrelated = _reported_mu(doc)
    unrelated.document_key = "different_document"
    assert not repair_incomplete_event_period(event, [doc], [unrelated])
    assert repair_incomplete_event_period(event, [doc], [_reported_mu(doc, "FQ4 2026 Financial Results")])
    assert event.collection_status["period_repair"]["official_header_corroboration"]


def test_corroborated_header_cannot_change_published_receipt():
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    event, doc = _legacy_mu()
    event.status, event.report_version = "published", 1
    event.delivery = {"status": "accepted", "message_id": 123}
    assert not repair_incomplete_event_period(event, [doc], [_reported_mu(doc)])
    assert event.fiscal_year == 2027 and event.delivery["message_id"] == 123


def test_actual_sec_hyphenated_results_heading_beats_forward_year_subtitle():
    from us_earnings_monitor.grouping import repair_incomplete_event_period
    event, doc = _legacy_mu()
    text = ('MICRON TECHNOLOGY, INC. REPORTS RECORD FISCAL\n'
            'FOURTH-QUARTER AND FULL-YEAR 2026 RESULTS\n'
            'AI-driven demand and strong operational execution position Micron for a record fiscal 2027')
    assert repair_incomplete_event_period(event, [doc], [_reported_mu(doc, text)])
    assert (event.fiscal_year, event.quarter) == (2026, 'Q4')
