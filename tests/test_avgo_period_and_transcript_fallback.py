from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.grouping import align_companion_periods, event_id
from us_earnings_monitor.models import Company, Disclosure, EarningsEvent
from us_earnings_monitor.quality import publication_gate
from us_earnings_monitor.sources.public_transcript import PublicTranscriptAdapter
from us_earnings_monitor.sources.sec import SecEdgarAdapter


class _TranscriptResponse:
    def __init__(self, html: str):
        self.content = html.encode()

    def raise_for_status(self):
        return None


class _TranscriptSession:
    def get(self, url, **kwargs):
        body = (
            "Broadcom Inc. Common Stock (AVGO) 2026 Q3 Earnings Call Transcript\n"
            "Conference call prepared remarks. "
            "Operator: We will now begin the Question-and-Answer Session. "
            "Analyst: Can you discuss supply and demand? "
            "Hock Tan: Demand is strong and we have secured supply. "
        ) * 40
        return _TranscriptResponse(f"<html><body><main>{body}</main></body></html>")


def _company():
    return Company("AVGO", "Broadcom Inc.", "0001730168")


def test_dated_earnings_exhibit_recovers_quarter_end_and_fiscal_quarter():
    adapter = SecEdgarAdapter()
    adapter._attachment_rows = lambda company, record: [
        ("8-K", "8-K", "https://www.sec.gov/x/avgo-20260902.htm"),
        ("EX-99.1", "EX-99.1", "https://www.sec.gov/x/avgo-20260802ex991.htm"),
    ]
    record = {
        "form": "8-K",
        "accessionNumber": "0001730168-26-000001",
        "filingDate": "2026-09-02",
        "reportDate": "2026-09-02",
        "acceptanceDateTime": "2026-09-02T16:05:00-04:00",
        "primaryDocDescription": "Current report",
        "_fiscalYearEnd": "1103",
    }
    docs = adapter._disclosures(_company(), record, [record])
    exhibit = next(doc for doc in docs if doc.metadata["document_type"] == "EX-99.1")
    assert exhibit.period_end == "2026-08-02"
    assert exhibit.fiscal_year == 2026
    assert exhibit.quarter == "Q3"
    assert exhibit.metadata["period_from_exhibit_filename"] == "2026-08-02"

    align_companion_periods(docs)
    assert {event_id(doc) for doc in docs} == {"AVGO_2026-08-02_Q3"}


def test_public_transcript_adapter_is_explicitly_third_party_and_qualitative_only():
    event = EarningsEvent(
        "AVGO_2026-08-02_Q3", "AVGO", 2026, "Q3",
        "2026-09-02T16:05:00-04:00", period_end="2026-08-02",
    )
    docs, status = PublicTranscriptAdapter(session=_TranscriptSession()).fetch(
        _company(), event, datetime(2026, 9, 2, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    )
    assert len(docs) == 1
    doc = docs[0]
    assert doc.source == "third_party_transcript"
    assert doc.document_kind == "transcript"
    assert doc.fiscal_year == 2026 and doc.quarter == "Q3"
    assert doc.metadata["provenance"] == "third_party_transcript"
    assert doc.metadata["qualitative_only"] is True
    assert "Question-and-Answer" in doc.metadata["transcript_text"]
    assert status["provenance"] == "third_party_transcript"


def test_transcript_enrichment_prevents_false_sec_only_publication_mode():
    now = datetime(2026, 9, 2, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    event = EarningsEvent(
        "AVGO_2026-08-02_Q3", "AVGO", 2026, "Q3",
        "2026-09-02T16:05:00-04:00", period_end="2026-08-02",
        collection_status={
            "official_ir_last_attempt_incomplete": True,
            "transcript_status": "FOUND",
        },
    )
    docs = [
        Disclosure(
            "sec_edgar", "release", "AVGO", "Broadcom earnings results",
            now.isoformat(), "https://sec.test/release", document_kind="financial_results",
            fiscal_year=2026, quarter="Q3", period_end="2026-08-02",
        ),
        Disclosure(
            "third_party_transcript", "t", "AVGO", "Broadcom Q3 transcript",
            now.isoformat(), "https://example.test/t", document_kind="transcript",
            fiscal_year=2026, quarter="Q3", period_end="2026-08-02",
            metadata={"provenance": "third_party_transcript", "qualitative_only": True},
        ),
    ]
    allowed, reasons, manifest = publication_gate(event, docs, now)
    assert allowed is True
    assert reasons == []
    assert manifest["transcript_status"] == "FOUND"
    assert manifest["publication_mode"] == "integrated_transcript_ir_pending"
