from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from us_earnings_monitor.__main__ import ingest, load_fixture, mark_baseline
from us_earnings_monitor.config import load_watchlist
from us_earnings_monitor.state import StateStore

ROOT = Path(__file__).parent.parent


def test_filter_dedup_and_event_grouping(tmp_path):
    store = StateStore(tmp_path / "state.json")
    _, patterns = load_watchlist(ROOT / "watchlist.yaml")
    now = datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    events, ignored = ingest(load_fixture(str(ROOT / "fixtures/disclosures.json")), store, patterns, now)
    assert ignored == 1 and len(events) == 1
    event = store.get_event("SPCX_2026-06-30_Q2")
    assert event is not None and len(event.documents) == 2
    events, ignored = ingest(load_fixture(str(ROOT / "fixtures/disclosures.json")), store, patterns, now)
    assert events == [] and ignored == 1


def test_full_watchlist_has_ciks_and_ir_allowlist():
    companies, _ = load_watchlist(ROOT / "watchlist.yaml")
    assert len(companies) == 36
    assert {company.ticker for company in companies} >= {"SPCX", "CBRS", "SKHY", "NVDA", "MSFT"}
    assert all(company.cik.isdigit() and len(company.cik) == 10 for company in companies)
    assert all(company.ir_index_url and company.ir_index_url.startswith("https://") for company in companies)


def test_baseline_marks_existing_documents_processed(tmp_path):
    store = StateStore(tmp_path / "state.json")
    _, patterns = load_watchlist(ROOT / "watchlist.yaml")
    now = datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    ingest(load_fixture(str(ROOT / "fixtures/disclosures.json")), store, patterns, now)
    assert mark_baseline(store, now) == 1
    event = store.get_event("SPCX_2026-06-30_Q2")
    assert event is not None and event.status == "published"
    assert event.last_analyzed_document_count == 2

