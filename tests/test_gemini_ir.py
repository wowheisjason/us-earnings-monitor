import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from us_earnings_monitor.extract import EvidenceExtractor
from us_earnings_monitor.gemini import GeminiClient
from us_earnings_monitor.grouping import ready_for_analysis
from us_earnings_monitor.models import Company, EarningsEvent


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeGeminiSession:
    def __init__(self, research_payload):
        self.research_payload = research_payload
        self.posts = []

    def get(self, url, **kwargs):
        return FakeResponse({
            "models": [{
                "name": "models/gemini-3.7-flash",
                "supportedGenerationMethods": ["generateContent"],
            }]
        })

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return FakeResponse({
            "candidates": [{
                "content": {"parts": [{"text": json.dumps(self.research_payload)}]},
                "groundingMetadata": {
                    "webSearchQueries": ["Dell FY2027 Q2 official transcript"],
                    "groundingChunks": [{"web": {"uri": "https://investors.delltechnologies.com/"}}],
                },
                "urlContextMetadata": {
                    "urlMetadata": [{
                        "retrievedUrl": "https://investors.delltechnologies.com/static-files/transcript-uuid",
                        "urlRetrievalStatus": "URL_RETRIEVAL_STATUS_SUCCESS",
                    }]
                },
            }],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        })


class NoNetworkSession:
    def get(self, *args, **kwargs):
        raise AssertionError("grounded IR evidence must not be refetched by GitHub")


def dell_event(first_seen="2026-09-01T16:10:00-04:00"):
    return EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", first_seen, updated_at=first_seen)


def test_grounded_ir_research_uses_search_url_context_and_rejects_unofficial_hosts():
    payload = {
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
    session = FakeGeminiSession(payload)
    client = GeminiClient(api_key="test", session=session)
    company = Company("DELL", "Dell Technologies", "0001571996", "https://investors.delltechnologies.com/")
    now = datetime(2026, 9, 2, 9, 15, tzinfo=ZoneInfo("America/New_York"))

    docs, status = client.research_official_ir(company, dell_event(), now)

    assert len(docs) == 1
    assert docs[0].source == "gemini_grounded_ir"
    assert docs[0].metadata["grounded_evidence"].startswith("Analyst Q&A")
    assert status["transcript_status"] == "FOUND"
    assert status["rejected_unofficial_urls"] == ["https://example.com/dell-transcript"]
    body = session.posts[0][1]["json"]
    assert {tuple(tool.keys()) for tool in body["tools"]} == {("url_context",), ("google_search",)}


def test_grounded_evidence_extractor_never_refetches_issuer_site():
    payload = {
        "event_id": "DELL_FY2027_Q2",
        "call": {"scheduled_at": None, "status": "completed"},
        "transcript_status": "FOUND",
        "sources": [{
            "kind": "transcript",
            "title": "Transcript",
            "url": "https://investors.delltechnologies.com/static-files/transcript-uuid",
            "published_at": None,
            "evidence_text": "Complete grounded transcript extract with Q&A.",
            "structured_facts": [{"metric": "AI server orders", "value": "60.9B"}],
        }],
        "research_notes": [],
    }
    client = GeminiClient(api_key="test", session=FakeGeminiSession(payload))
    company = Company("DELL", "Dell Technologies", "0001571996", "https://investors.delltechnologies.com/")
    now = datetime(2026, 9, 2, 9, 15, tzinfo=ZoneInfo("America/New_York"))
    docs, _ = client.research_official_ir(company, dell_event(), now)

    evidence = EvidenceExtractor(session=NoNetworkSession()).fetch(docs[0])
    assert evidence.text == "Complete grounded transcript extract with Q&A."
    assert evidence.structured_facts[0]["metric"] == "AI server orders"


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
