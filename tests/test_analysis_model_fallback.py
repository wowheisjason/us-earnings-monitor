import json

from us_earnings_monitor.gemini_v2 import GeminiV2Client, _generation_config


def test_analysis_models_include_heterogeneous_36_quaternary(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-flash-lite-latest")
    monkeypatch.setenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_ANALYSIS_QUATERNARY_MODEL", "gemini-3.6-flash")
    client = GeminiV2Client(api_key="test")
    assert client._stage_models("facts_chunk") == [
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.5-flash",
        "gemini-3.6-flash",
    ]


def test_analysis_models_deduplicate_configured_models(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash")
    monkeypatch.setenv("GEMINI_ANALYSIS_QUATERNARY_MODEL", "gemini-3.5-flash")
    client = GeminiV2Client(api_key="test")
    assert client._stage_models("analyst") == ["gemini-3.5-flash-lite", "gemini-3.5-flash"]


def test_generation_config_omits_removed_sampling_parameter_for_36_plus(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MAX_OUTPUT_TOKENS", "8192")
    assert _generation_config("gemini-3.5-flash") == {
        "responseMimeType": "application/json",
        "maxOutputTokens": 8192,
        "temperature": 0.1,
    }
    assert _generation_config("gemini-3.6-flash") == {
        "responseMimeType": "application/json",
        "maxOutputTokens": 8192,
    }
    assert _generation_config("gemini-3.7-flash") == {
        "responseMimeType": "application/json",
        "maxOutputTokens": 8192,
    }


class _Response:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": self._text}]},
            }],
            "usageMetadata": {},
        }


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_malformed_json_retries_exact_prompt_without_heuristic_repair(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_QUATERNARY_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_ATTEMPTS", "2")
    monkeypatch.setenv("GEMINI_ANALYSIS_BACKOFF_SECONDS", "0")
    session = _Session([
        _Response('{"facts":[{"value":123},]}'),
        _Response(json.dumps({"facts": [{"value": 123}]})),
    ])
    client = GeminiV2Client(api_key="test", session=session)

    value = client._json("same source-backed prompt", "facts_chunk")

    assert value == {"facts": [{"value": 123}]}
    assert len(session.calls) == 2
    assert session.calls[0][1]["json"]["contents"] == session.calls[1][1]["json"]["contents"]
