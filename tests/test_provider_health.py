from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from us_earnings_monitor.__main__ import _provider_is_blocked, _record_provider_health
from us_earnings_monitor.state import StateStore

ET = ZoneInfo("America/New_York")


def test_search_quota_failure_persists_six_hour_circuit(tmp_path):
    now = datetime(2026, 9, 2, 12, 0, tzinfo=ET)
    store = StateStore(tmp_path / "state.json")
    attempts = [{
        "provider": "gemini_search",
        "ok": False,
        "category": "search_quota_blocked",
        "error": "GeminiSearchUnavailable: quota blocked",
    }]

    _record_provider_health(store, attempts, now)

    health = store.get_provider_health("gemini_search")
    assert health["status"] == "search_quota_blocked"
    assert _provider_is_blocked(store, "gemini_search", now + timedelta(hours=5, minutes=59))
    assert not _provider_is_blocked(store, "gemini_search", now + timedelta(hours=6, seconds=1))


def test_provider_success_closes_circuit(tmp_path):
    now = datetime(2026, 9, 2, 12, 0, tzinfo=ET)
    store = StateStore(tmp_path / "state.json")
    _record_provider_health(store, [{
        "provider": "gemini_search", "ok": False, "category": "search_quota_blocked", "error": "429"
    }], now)
    assert _provider_is_blocked(store, "gemini_search", now + timedelta(hours=1))

    _record_provider_health(store, [{"provider": "gemini_search", "ok": True}], now + timedelta(hours=1))
    assert not _provider_is_blocked(store, "gemini_search", now + timedelta(hours=1, minutes=1))
