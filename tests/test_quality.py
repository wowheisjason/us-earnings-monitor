from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.models import Disclosure, EarningsEvent
from us_earnings_monitor.quality import (
    TRANSCRIPT_EXPECTED,
    TRANSCRIPT_FOUND,
    TRANSCRIPT_NOT_FOUND,
    publication_gate,
    update_collection_status,
)

ET = ZoneInfo("America/New_York")


def result_doc() -> Disclosure:
    return Disclosure(
        "sec_edgar", "r1", "DELL", "Dell Q2 FY2027 Earnings Results",
        "2026-09-01T16:05:00-04:00", "https://sec.example/results.htm",
        document_kind="financial_results",
    )


def transcript_doc() -> Disclosure:
    return Disclosure(
        "official_ir", "t1", "DELL", "Transcript",
        "2026-09-01T20:00:00-04:00", "https://ir.example/static-files/uuid",
        document_url="https://ir.example/static-files/uuid", fiscal_year=2027, quarter="Q2",
        document_kind="transcript",
    )


def event() -> EarningsEvent:
    return EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", "2026-09-01T16:05:00-04:00")


def test_missing_transcript_is_pending_during_collection_window():
    current = event()
    now = datetime(2026, 9, 1, 20, 0, tzinfo=ET)
    update_collection_status(current, [result_doc()], now, official_ir_checked=True)
    assert current.collection_status["transcript_status"] == TRANSCRIPT_EXPECTED
    allowed, reasons, _ = publication_gate(current, [result_doc()], now)
    assert not allowed
    assert "transcript_collection_window_open" in reasons


def test_missing_transcript_becomes_not_found_after_retry_window():
    current = event()
    now = datetime(2026, 9, 2, 20, 30, tzinfo=ET)
    update_collection_status(current, [result_doc()], now, official_ir_checked=True)
    assert current.collection_status["transcript_status"] == TRANSCRIPT_NOT_FOUND
    allowed, reasons, manifest = publication_gate(current, [result_doc()], now)
    assert allowed
    assert reasons == []
    assert manifest["transcript_status"] == TRANSCRIPT_NOT_FOUND


def test_found_transcript_allows_early_publication():
    current = event()
    now = datetime(2026, 9, 1, 20, 0, tzinfo=ET)
    docs = [result_doc(), transcript_doc()]
    update_collection_status(current, docs, now, official_ir_checked=True)
    assert current.collection_status["transcript_status"] == TRANSCRIPT_FOUND
    allowed, reasons, manifest = publication_gate(current, docs, now)
    assert allowed
    assert reasons == []
    assert manifest["has_transcript_or_qa"]
