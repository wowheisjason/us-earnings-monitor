import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from us_earnings_monitor.extract import EvidenceExtractor
from us_earnings_monitor.gemini_v2 import GeminiV2Client
from us_earnings_monitor.grouping import ready_for_analysis
from us_earnings_monitor.models import Company, EarningsEvent
from us_earnings_monitor.retrieval import schedule_next_ir_retry, should_attempt_ir
from us_earnings_monitor.validation import validate_report_text


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {}
        self.text = json.dumps(payload)

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGeminiSession:
    def __init__(self, research_payload):
        self.research_payload = research_payload
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/interactions"):
            return FakeResponse({
                "id": "int_test",
                "status": "completed",
                "steps": [
                    {"type": "google_search_call", "arguments": {"queries": ["Dell FY2027 Q2 official transcript"]}},
                    {"type": "google_search_result", "result": [{"url": "https://investors.delltechnologies.com/"}]},
                    {"type": "model_output", "content": [{"type": "text", "text": json.dumps(self.research_payload)}]},
                ],
            })
        return FakeResponse({
            "candidates": [{"content": {"parts": [{"text": json.dumps(self.research_payload)}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        })


class NoNetworkSession:
    def get(self, *args, **kwargs):
        raise AssertionError("grounded IR evidence must not be refetched by GitHub")


def dell_event(first_seen="2026-09-01T16:10:00-04:00"):
    return EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", first_seen, updated_at=first_seen)


def research_payload():
    return {
        "event_id": "DELL_FY2027_Q2",
        "call": {"scheduled_at": "2026-09-01T16:30:00-04:00", "status": "completed"},
        "transcript_status": "FOUND",
        "sources": [
            {
                "kind": "transcript",
                "title": "Dell Technologies Fiscal Year 2027 Second Quarter Results Transcript",
                "url": "https://investors.delltechnologies.com/static-files/transcript-uuid",
                "published_at": "2026-09-01T18:00:00-04:00",
                "evidence_text": "Analyst Q&A: AI server orders and backlog discussion.",
                "structured_facts": [],
            },
            {
                "kind": "transcript",
                "title": "Third party transcript",
                "url": "https://example.com/dell-transcript",
                "published_at": None,
                "evidence_text": "This must be rejected.",
                "structured_facts": [],
            },
        ],
        "research_notes": [],
    }


def test_v2_ir_research_uses_interactions_search_and_rejects_unofficial_hosts(monkeypatch):
    monkeypatch.setenv("GEMINI_IR_MODEL", "gemini-3.6-flash")
    session = FakeGeminiSession(research_payload())
    client = GeminiV2Client(api_key="test", session=session)
    company = Company("DELL", "Dell Technologies", "0001571996", "https://investors.delltechnologies.com/",
                      official_domains=["delltechnologies.com"])
    now = datetime(2026, 9, 2, 9, 15, tzinfo=ZoneInfo("America/New_York"))

    docs, status = client.research_official_ir(company, dell_event(), now)

    assert len(docs) == 1
    assert docs[0].source == "gemini_grounded_ir"
    assert docs[0].metadata["retrieval_method"] == "gemini_interactions_google_search"
    assert status["model"] == "gemini-3.6-flash"
    assert status["transcript_status"] == "FOUND"
    assert status["rejected_unofficial_urls"] == ["https://example.com/dell-transcript"]
    url, kwargs = session.posts[0]
    assert url.endswith("/interactions")
    assert kwargs["json"]["model"] == "gemini-3.6-flash"
    assert kwargs["json"]["tools"] == [{"type": "google_search"}]
    assert kwargs["json"]["response_format"]["mime_type"] == "application/json"


def test_grounded_evidence_extractor_never_refetches_issuer_site():
    client = GeminiV2Client(api_key="test", session=FakeGeminiSession(research_payload()))
    company = Company("DELL", "Dell Technologies", "0001571996", "https://investors.delltechnologies.com/",
                      official_domains=["delltechnologies.com"])
    now = datetime(2026, 9, 2, 9, 15, tzinfo=ZoneInfo("America/New_York"))
    docs, _ = client.research_official_ir(company, dell_event(), now)

    evidence = EvidenceExtractor(session=NoNetworkSession()).fetch(docs[0])
    assert evidence.text.startswith("Analyst Q&A")


def test_event_clock_analyzes_immediately_when_transcript_found():
    now = datetime(2026, 9, 1, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    event = dell_event("2026-09-01T16:10:00-04:00")
    event.collection_status = {"transcript_status": "FOUND"}
    assert ready_for_analysis(event, now)


def test_event_clock_waits_for_short_collection_window_then_allows_v1():
    first_seen = datetime(2026, 9, 1, 7, 0, tzinfo=ZoneInfo("America/New_York"))
    event = dell_event(first_seen.isoformat())
    event.collection_status = {
        "transcript_status": "EXPECTED_NOT_YET_AVAILABLE",
        "official_ir_checked_at": first_seen.isoformat(),
    }
    assert not ready_for_analysis(event, first_seen + timedelta(hours=2))
    assert ready_for_analysis(event, first_seen + timedelta(hours=4, minutes=1))


def test_event_local_retry_clock_stops_after_transcript():
    now = datetime(2026, 9, 1, 17, 0, tzinfo=ZoneInfo("America/New_York"))
    event = dell_event("2026-09-01T16:10:00-04:00")
    event.collection_status = {"transcript_status": "EXPECTED_NOT_YET_AVAILABLE"}
    schedule_next_ir_retry(event, now)
    assert not should_attempt_ir(event, now)
    assert should_attempt_ir(event, now + timedelta(hours=1))
    event.collection_status["transcript_status"] = "FOUND"
    event.collection_status["official_ir_checked_at"] = now.isoformat()
    schedule_next_ir_retry(event, now)
    assert "next_ir_retry_at" not in event.collection_status
    assert not should_attempt_ir(event, now + timedelta(days=1))


def test_report_validator_rejects_llm_generated_chinese_currency_conversion():
    assert validate_report_text("AI backlog 95 億美元") == ["report_contains_model_generated_currency_unit_conversion"]
    assert validate_report_text("AI backlog $95B") == []
    assert "report_contains_self_correction_language" in validate_report_text("營收 46.971 億美元（應為 469.71 億美元）")
