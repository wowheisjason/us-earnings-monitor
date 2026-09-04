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


def test_generation_config_omits_removed_sampling_parameter_for_36_plus():
    assert _generation_config("gemini-3.5-flash") == {
        "responseMimeType": "application/json",
        "temperature": 0.1,
    }
    assert _generation_config("gemini-3.6-flash") == {"responseMimeType": "application/json"}
    assert _generation_config("gemini-3.7-flash") == {"responseMimeType": "application/json"}
