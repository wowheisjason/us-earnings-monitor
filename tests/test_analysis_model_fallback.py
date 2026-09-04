from us_earnings_monitor.gemini_v2 import GeminiV2Client


def test_analysis_models_include_full_flash_tertiary(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-flash-lite-latest")
    monkeypatch.setenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash")
    client = GeminiV2Client(api_key="test")
    assert client._stage_models("facts_chunk") == [
        "gemini-3.5-flash-lite",
        "gemini-flash-lite-latest",
        "gemini-3.5-flash",
    ]


def test_analysis_models_deduplicate_configured_models(monkeypatch):
    monkeypatch.setenv("GEMINI_ANALYSIS_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_ANALYSIS_TERTIARY_MODEL", "gemini-3.5-flash")
    client = GeminiV2Client(api_key="test")
    assert client._stage_models("analyst") == ["gemini-3.5-flash-lite", "gemini-3.5-flash"]
