from argparse import Namespace
from datetime import datetime
from zoneinfo import ZoneInfo
import pytest
import requests
from us_earnings_monitor import __main__ as runner, telegram
from us_earnings_monitor.models import Disclosure, EarningsEvent
from us_earnings_monitor.state import StateStore


def test_oversized_report_is_blocked_instead_of_losing_risks():
    doc = Disclosure("fixture", "1", "TEST", "Official evidence", "2026-10-02T00:00:00+00:00", "https://example.com/official")
    with pytest.raises(ValueError, match="Telegram budget"):
        runner._compose_report("• Evidence & context " * 400 + "\n⚠️ 風險、反證與待驗證:\n• 必須保留的風險", [doc])


def test_telegram_boolean_is_not_a_valid_receipt(monkeypatch):
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"ok": True, "result": {"message_id": True}}
    monkeypatch.setattr(telegram.requests, "post", lambda *a, **k: Response())
    with pytest.raises(RuntimeError, match="message_id"):
        telegram.send_report("test", token="token", chat_id="chat")


def test_transport_error_does_not_expose_bot_token(monkeypatch):
    def fail(*a, **k):
        raise requests.Timeout("https://api.telegram.org/botSECRET/sendMessage")
    monkeypatch.setattr(telegram.requests, "post", fail)
    with pytest.raises(RuntimeError) as info:
        telegram.send_report("test", token="SECRET", chat_id="chat")
    assert "SECRET" not in str(info.value)


def test_unknown_delivery_is_not_automatically_resent(tmp_path):
    store = StateStore(tmp_path / "state.json")
    event = EarningsEvent("test", "TEST", 2027, "Q1", "2026-10-02T20:30:00+00:00")
    event.delivery = {"status": "sending", "report_version": 1}
    outcome = runner._run_analysis(event, store, object(), False, datetime.now(ZoneInfo("America/New_York")))
    assert outcome == "delivery_unknown"


def test_scoped_run_ignores_other_persisted_events_and_surfaces_failure(tmp_path, monkeypatch):
    store = StateStore(tmp_path / "state.json")
    for ticker in ("TARGET", "OTHER"):
        event = EarningsEvent(ticker, ticker, 2027, "Q1", "2026-10-01T00:00:00+00:00", documents=["doc"])
        store.put_event(event)
    store.save()
    args = Namespace(at="2026-10-02T20:30:00-04:00", tickers="TARGET", watchlist="unused", state=str(store.path), fixture="fixture", dry_run=False, baseline=False, preview=False)
    monkeypatch.setattr(runner, "parse_args", lambda: args)
    monkeypatch.setattr(runner, "load_watchlist", lambda path: ([type("Company", (), {"ticker": "TARGET"})()], []))
    monkeypatch.setattr(runner, "discover", lambda *a: ([], {"fixture"}))
    monkeypatch.setattr(runner, "ingest", lambda *a: ([], 0))
    monkeypatch.setattr(runner, "ready_for_analysis", lambda *a: True)
    seen=[]
    def fail(event, *a):
        seen.append(event.ticker)
        return "needs_human_review"
    monkeypatch.setattr(runner, "_run_analysis", fail)
    monkeypatch.setattr(runner, "build_analysis_client", lambda: object())
    assert runner.main() == 1
    assert seen == ["TARGET"]


def test_disclosed_numeric_change_has_provenance_but_computed_rate_does_not():
    from us_earnings_monitor.report_contract import numeric_provenance_errors
    facts = {"facts": [{"reported_change": 155.1, "evidence": {"quote": "104,828 155.1 111,456 166.8"}}]}
    assert numeric_provenance_errors("年增 155.1%", facts) == []
    assert numeric_provenance_errors("貢獻 93.4%", facts) == ["unbacked_derived_rate:93.4%"]
    assert numeric_provenance_errors("年增 155.1%", {"facts": [{"reported_change": 155.1}]}) == ["unbacked_derived_rate:155.1%"]
    assert numeric_provenance_errors("年減 73.5%", {"quote": "73.5％減"}) == []


def test_unproven_hypothesis_is_not_a_positive_strong_claim():
    from us_earnings_monitor.report_contract import strong_claim_errors
    facts = {'facts': [{'metric': 'Revenue', 'value': 100}]}
    assert strong_claim_errors('仍不能判定市場是否存在長期定價權。', facts) == []
    assert strong_claim_errors('其長期定價權仍有待驗證。', facts) == []
    assert strong_claim_errors('公司已建立定價權，但長期需求仍待驗證。', facts)
    assert strong_claim_errors('不能判定是否具有定價權。公司已形成瓶頸供應商地位。', facts)
