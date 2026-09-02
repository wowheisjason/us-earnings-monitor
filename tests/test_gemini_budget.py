import json

import requests

from us_earnings_monitor.gemini import EVIDENCE_TOTAL_MAX_CHARS, GeminiClient
from us_earnings_monitor.models import EarningsEvent, Evidence


class FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "models": [
                {"name": "models/gemini-3.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.7-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-4.0-flash-image", "supportedGenerationMethods": ["generateContent"]},
            ]
        }


class FakeSession:
    def get(self, *args, **kwargs):
        return FakeResponse()


class GenerationResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {
            "candidates": [{"content": {"parts": [{"text": '{"ok": true}'}]}}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 2, "totalTokenCount": 12},
        }


class TimeoutThenFallbackSession(FakeSession):
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append(url)
        if "gemini-3.7-flash" in url:
            raise requests.ReadTimeout("simulated timeout")
        return GenerationResponse()


def test_evidence_budget_is_shared_across_all_documents():
    event = EarningsEvent("SPCX_2026-06-30_Q2", "SPCX", 2026, "Q2", "2026-08-04T16:00:00-04:00", period_end="2026-06-30")
    evidence = [Evidence(str(i), f"doc {i}", f"https://example.test/{i}", "x" * 60_000) for i in range(3)]
    payload = json.loads(GeminiClient._evidence(event, evidence))
    excerpts = [item["excerpt"] for item in payload["documents"]]
    assert len(excerpts) == 3
    assert sum(map(len, excerpts)) <= EVIDENCE_TOTAL_MAX_CHARS


def test_model_is_selected_from_models_available_to_the_api_key():
    client = GeminiClient(api_key="test-key", session=FakeSession())
    assert client._resolve_model() == "gemini-3.7-flash"


def test_explicit_model_skips_discovery():
    client = GeminiClient(api_key="test-key", model="models/gemini-custom", session=FakeSession())
    assert client._resolve_model() == "gemini-custom"


def test_usage_metadata_is_accumulated_for_reporting():
    client = GeminiClient(api_key="test-key", model="gemini-custom", session=FakeSession())
    client._record_usage("facts", "gemini-custom", {"usageMetadata": {
        "promptTokenCount": 100, "candidatesTokenCount": 20,
        "thoughtsTokenCount": 30, "totalTokenCount": 150,
    }})
    assert client.usage == {
        "prompt_tokens": 100, "output_tokens": 20,
        "thought_tokens": 30, "total_tokens": 150, "calls": 1,
    }


def test_network_timeout_falls_back_to_next_available_model(monkeypatch):
    monkeypatch.setattr("us_earnings_monitor.gemini.time.sleep", lambda _: None)
    session = TimeoutThenFallbackSession()
    client = GeminiClient(api_key="test-key", session=session)
    result = client._json("test", "facts")
    assert result == {"ok": True}
    assert client.model == "gemini-3.5-flash-lite"
    assert sum("gemini-3.7-flash" in url for url in session.posts) == 2
    assert any("gemini-3.5-flash-lite" in url for url in session.posts)
