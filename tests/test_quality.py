from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.models import Disclosure, EarningsEvent
from us_earnings_monitor.quality import (
    TRANSCRIPT_EXPECTED,
    TRANSCRIPT_FOUND,
    TRANSCRIPT_NOT_FOUND,
    publication_gate,
    source_manifest,
    update_collection_status,
)

ET = ZoneInfo("America/New_York")


def result_doc() -> Disclosure:
    return Disclosure(
        "sec_edgar", "r1", "DELL", "Dell Q2 FY2027 Earnings Results",
        "2026-09-01T16:05:00-04:00", "https://sec.example/results.htm",
        document_kind="financial_results",
    )


def transcript_doc(source="official_ir") -> Disclosure:
    return Disclosure(
        source, "t1", "DELL", "Transcript",
        "2026-09-01T20:00:00-04:00", "https://ir.example/static-files/uuid",
        document_url="https://ir.example/static-files/uuid", fiscal_year=2027, quarter="Q2",
        document_kind="transcript",
    )


def event() -> EarningsEvent:
    return EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", "2026-09-01T16:05:00-04:00")


def test_missing_transcript_is_pending_during_collection_window_when_ir_is_healthy():
    current = event()
    now = datetime(2026, 9, 1, 20, 0, tzinfo=ET)
    update_collection_status(current, [result_doc()], now, official_ir_checked=True)
    assert current.collection_status["transcript_status"] == TRANSCRIPT_EXPECTED
    allowed, reasons, _ = publication_gate(current, [result_doc()], now)
    assert not allowed
    assert "transcript_collection_window_open" in reasons


def test_sec_first_v1_is_allowed_immediately_after_incomplete_ir_attempt():
    current = event()
    now = datetime(2026, 9, 1, 16, 10, tzinfo=ET)
    update_collection_status(current, [result_doc()], now, official_ir_checked=False)
    current.collection_status["official_ir_last_attempt_incomplete"] = now.isoformat()
    allowed, reasons, manifest = publication_gate(current, [result_doc()], now)
    assert allowed
    assert reasons == []
    assert manifest["publication_mode"] == "sec_only_v1_ir_pending"
    assert not manifest["has_official_ir"]


def test_sec_first_requires_primary_results_not_merely_failed_ir():
    current = event()
    now = datetime(2026, 9, 1, 16, 10, tzinfo=ET)
    current.collection_status["official_ir_last_attempt_incomplete"] = now.isoformat()
    allowed, reasons, _ = publication_gate(current, [], now)
    assert not allowed
    assert "missing_primary_results" in reasons


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


def test_openai_grounded_transcript_counts_as_official_ir():
    manifest = source_manifest([result_doc(), transcript_doc("openai_web_ir")])
    assert manifest["has_official_ir"] is True
    assert manifest["has_transcript_or_qa"] is True


def test_official_ir_artifact_forces_v2_after_sec_alert():
    current = event()
    current.status = "published_sec_pending"
    current.last_analyzed_document_count = 1
    docs = [result_doc(), transcript_doc()]
    assert requires_deterministic_enrichment_followup(current, docs)


def test_no_new_ir_artifact_does_not_force_duplicate_v2():
    current = event()
    current.status = "published_sec_pending"
    current.last_analyzed_document_count = 1
    assert not requires_deterministic_enrichment_followup(current, [result_doc()])
