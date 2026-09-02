import json
from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.models import Company, EarningsEvent
from us_earnings_monitor.openai_ir import OpenAIWebIrClient


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return FakeResponse({
            "output": [
                {"type": "web_search_call", "action": {"sources": [{"url": "https://investors.delltechnologies.com/"}]}},
                {"type": "message", "content": [{"type": "output_text", "text": json.dumps(self.result)}]},
            ]
        })


def test_openai_web_ir_is_domain_limited_and_rejects_unofficial_sources(monkeypatch):
    result = {
        "call": {"scheduled_at": "2026-09-01T16:30:00-04:00", "status": "completed"},
        "transcript_status": "FOUND",
        "sources": [
            {
                "kind": "transcript",
                "title": "Official Dell transcript",
                "url": "https://investors.delltechnologies.com/static-files/official",
                "published_at": "2026-09-01T18:00:00-04:00",
                "evidence_text": "Official Q&A evidence",
                "structured_facts": [],
            },
            {
                "kind": "transcript",
                "title": "Third-party transcript",
                "url": "https://example.com/transcript",
                "published_at": None,
                "evidence_text": "Reject me",
                "structured_facts": [],
            },
        ],
        "research_notes": [],
    }
    session = FakeSession(result)
    monkeypatch.setenv("OPENAI_IR_MODEL", "gpt-5.6-luna")
    client = OpenAIWebIrClient(api_key="test", session=session)
    company = Company(
        "DELL", "Dell Technologies", "0001571996",
        "https://investors.delltechnologies.com/",
        official_domains=["delltechnologies.com"],
    )
    event = EarningsEvent("DELL_FY2027_Q2", "DELL", 2027, "Q2", "2026-09-01T16:10:00-04:00")
    now = datetime(2026, 9, 2, 20, 30, tzinfo=ZoneInfo("America/New_York"))

    docs, status = client.research_official_ir(company, event, now)

    assert len(docs) == 1
    assert docs[0].source == "openai_web_ir"
    assert docs[0].metadata["retrieval_method"] == "openai_responses_web_search"
    assert status["provider"] == "openai_web_search"
    assert status["rejected_unofficial_urls"] == ["https://example.com/transcript"]
    url, kwargs = session.calls[0]
    assert url == "https://api.openai.com/v1/responses"
    tool = kwargs["json"]["tools"][0]
    assert tool["type"] == "web_search"
    assert "investors.delltechnologies.com" in tool["filters"]["allowed_domains"]
    assert "delltechnologies.com" in tool["filters"]["allowed_domains"]
    assert kwargs["json"]["model"] == "gpt-5.6-luna"
    assert kwargs["json"]["store"] is False
