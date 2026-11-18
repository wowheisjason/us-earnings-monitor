from datetime import datetime, timezone

from us_earnings_monitor.models import Disclosure, Evidence
from us_earnings_monitor.extract import EvidenceExtractor
from us_earnings_monitor.research_units import unitize_evidence
from us_earnings_monitor.investor_analysis_v5 import ProductionInvestorV5Client
from us_earnings_monitor.quality import source_manifest
from us_earnings_monitor import __main__ as runner
from us_earnings_monitor.state import StateStore


def test_media_retains_secondary_provenance_through_ingest_and_research(tmp_path):
    doc = Disclosure("credible_media_qa", "article", "SNOW", "Snowflake 2027 Q2 Q&A reporting",
        "2026-09-02T20:00:00+00:00", "https://www.reuters.com/article", fiscal_year=2027, quarter="Q2",
        document_kind="media_qa_report", metadata={"media_text": "An analyst asked about demand.",
        "provenance": "credible_media_qa", "qualitative_only": True,
        "retrieved_at": "2026-09-02T21:00:00+00:00", "outlet": "www.reuters.com"})
    store = StateStore(tmp_path / "state.json")
    runner.ingest([doc], store, ["earnings"], datetime(2026, 9, 2, 21, tzinfo=timezone.utc))
    assert store.all_events()
    assert doc.document_kind == "media_qa_report"
    assert not source_manifest([doc])["has_transcript_or_qa"]
    evidence = EvidenceExtractor().fetch(doc)
    units, _ = unitize_evidence([evidence])
    assert units[0]["phase"] == "media_qa_report"
    cards = ProductionInvestorV5Client._decorate_cards([{"unit_id": units[0]["unit_id"], "card_type": "qa",
        "quote": "An analyst asked about demand.", "metric": "revenue", "value": 999}], units)
    assert cards[0]["provenance"] == "credible_media_qa"
    assert cards[0]["url"] == doc.url
    assert cards[0]["published_at"] == doc.published_at
    assert cards[0]["retrieved_at"] == doc.metadata["retrieved_at"]
    assert cards[0]["outlet"] == "www.reuters.com"
    views = ProductionInvestorV5Client._compatibility_views(cards)
    assert not views["qa"] and not views["facts"]


def test_public_transcript_rejects_wrong_period():
    from us_earnings_monitor.sources.public_transcript import PublicTranscriptAdapter
    from us_earnings_monitor.models import Company, EarningsEvent
    company = Company("AVGO", "Broadcom Inc.", "cik")
    event = EarningsEvent("event", "AVGO", 2026, "Q3", "2026-09-02T20:00:00+00:00")
    wrong = "Broadcom AVGO 2025 Q2 Earnings Call Transcript Conference Call Operator " * 100
    assert not PublicTranscriptAdapter._looks_like_transcript(wrong, company, event)


def test_unavailable_official_qa_keeps_retry_and_invokes_actual_fallback(monkeypatch):
    from us_earnings_monitor import retrieval
    from us_earnings_monitor.models import Company, EarningsEvent
    from us_earnings_monitor.quality import update_collection_status, publication_gate
    now = datetime(2026, 9, 2, 21, tzinfo=timezone.utc)
    company = Company("SNOW", "Snowflake", "cik")
    bad = Disclosure("official_ir", "failed-qa", "SNOW", "Q&A", now.isoformat(),
                     "https://issuer.example/qa", document_kind="qa")
    primary = Disclosure("sec_edgar", "results", "SNOW", "Results", now.isoformat(),
                         "https://sec.example/results", document_kind="financial_results")
    media = Disclosure("credible_media_qa", "media", "SNOW", "Secondary Q&A reporting", now.isoformat(),
                       "https://www.reuters.com/article", document_kind="media_qa_report")
    event = EarningsEvent("event", "SNOW", 2027, "Q2", now.isoformat(), collection_status={
        "transcript_status": "FOUND", "official_ir_checked_at": now.isoformat(),
        "unavailable_evidence": [{"document_key": bad.key, "reason": "HTTPError"}],
    })
    update_collection_status(event, [primary, bad], now, official_ir_checked=False)
    assert event.collection_status["transcript_status"] != "FOUND"
    assert retrieval.should_attempt_ir(event, now)
    assert publication_gate(event, [primary, bad], now)[2]["has_transcript_or_qa"] is False
    calls = []
    class Research:
        def research_official_ir(self, *args):
            return [primary, bad], {"research_complete": True, "transcript_status": "FOUND"}
    class Public:
        def fetch(self, *args):
            calls.append("public_transcript")
            return [], {"reason": "not_found"}
    class Media:
        def fetch(self, *args):
            calls.append("credible_media")
            return [media], {"provenance": "credible_media_qa"}
    monkeypatch.delenv("ALPHA_VANTAGE_API_KEY", raising=False)
    monkeypatch.setattr(retrieval, "PublicTranscriptAdapter", Public)
    monkeypatch.setattr(retrieval, "CredibleMediaAdapter", Media)
    result = retrieval.IrRetrievalRouter(Research()).collect(company, event, now)
    assert calls == ["public_transcript", "credible_media"]
    assert media in result.documents
    assert result.status["transcript_status"] != "FOUND"
    # Discovery of a genuinely different transcript still stops fallback.
    good = Disclosure("official_ir", "working-transcript", "SNOW", "Transcript", now.isoformat(),
                      "https://issuer.example/good", document_kind="transcript")
    assert retrieval.IrRetrievalRouter._has_transcript([bad, good], event)
