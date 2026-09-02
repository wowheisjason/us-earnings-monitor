from datetime import date, datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.models import Company, Disclosure, EarningsEvent
from us_earnings_monitor.sources.official_ir import OfficialIrAdapter, active_events_for_ir
from us_earnings_monitor.state import StateStore


class FakeResponse:
    def __init__(self, html: str): self.content = html.encode()
    def raise_for_status(self): return None


class FakeSession:
    def __init__(self, html: str | dict[str, str]): self.html, self.calls = html, []
    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.html, dict):
            return FakeResponse(self.html[url])
        return FakeResponse(self.html)


def event(updated="2026-08-04T16:00:00-04:00"):
    return EarningsEvent("SPCX_2026-06-30_Q2", "SPCX", 2026, "Q2", updated, period_end="2026-06-30", updated_at=updated)


def test_ir_is_event_triggered_period_matched_and_host_allowlisted():
    page = """<h2>Q2 2026 Financial Results</h2><a href='/ir/q2-presentation.pdf'>Earnings Presentation</a>
    <p>2026-08-04 <a href='/ir/q2-qa.pdf'>Q&amp;A</a></p><a href='https://tracker.example/q2.pdf'>Financial results</a>
    <h2>Q4 2025 Financial Results</h2><a href='/ir/old.pdf'>Earnings Presentation</a>"""
    session = FakeSession(page)
    company = Company("SPCX", "SpaceX", "0001181412", "https://ir.example/ir/")
    docs = OfficialIrAdapter([event()], session=session).discover([company], date(2026, 8, 4))
    assert len(docs) == 2
    assert all((doc.fiscal_year, doc.quarter) == (2026, "Q2") for doc in docs)
    assert all(doc.url.startswith("https://ir.example/") for doc in docs)


def test_ir_accepts_extensionless_labelled_transcript():
    page = """<h1>Q2 2026 Financial Results</h1>
    <a href='/static-files/a24f97e4-63b2-460d-a4be-44738826e8ce'>Transcript</a>"""
    company = Company("SPCX", "SpaceX", "0001181412", "https://ir.example/")
    docs = OfficialIrAdapter([event()], session=FakeSession(page)).discover([company], date(2026, 8, 4))
    assert len(docs) == 1
    assert docs[0].document_kind == "other"  # classification happens during ingest
    assert docs[0].title == "Transcript"
    assert docs[0].document_url.endswith("a24f97e4-63b2-460d-a4be-44738826e8ce")
    assert docs[0].metadata["format"] == "unknown"


def test_ir_follows_one_same_host_event_page_for_assets():
    index = "https://ir.example/"
    detail = "https://ir.example/events/q2-2026-results"
    session = FakeSession({
        index: "<a href='/events/q2-2026-results'>Q2 2026 Financial Results</a>",
        detail: "<h1>Q2 2026 Financial Results</h1><a href='/static-files/transcript-uuid'>Transcript</a>",
    })
    company = Company("SPCX", "SpaceX", "0001181412", index)
    docs = OfficialIrAdapter([event()], session=session).discover([company], date(2026, 8, 4))
    assert [doc.title for doc in docs] == ["Transcript"]
    assert any(call[0] == detail for call in session.calls)


def test_ir_does_not_make_network_request_without_primary_event():
    session = FakeSession("<a href='/earnings.pdf'>Earnings Presentation</a>")
    company = Company("SPCX", "SpaceX", "0001181412", "https://ir.example/ir/")
    assert OfficialIrAdapter([], session=session).discover([company], date(2026, 8, 4)) == []
    assert session.calls == []


def test_active_event_window_expires():
    now = datetime(2026, 8, 24, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    assert active_events_for_ir([event("2026-08-04T16:00:00-04:00")], now) == []
    assert active_events_for_ir([event("2026-08-14T20:00:00-04:00")], now)


def test_ir_mirror_of_primary_title_is_suppressed(tmp_path):
    store = StateStore(tmp_path / "state.json")
    primary = Disclosure("sec_edgar", "p1", "SPCX", "Form 10-Q — Q2 2026 Financial Results", "2026-08-04T16:00:00-04:00", "https://primary.example/p1.htm")
    store.add_document(primary)
    current = event(); current.documents.append(primary.key)
    mirror = Disclosure("official_ir", "i1", "SPCX", "Form 10-Q — Q2 2026 Financial Results", "2026-08-04T16:10:00-04:00", "https://ir.example/i1.htm")
    assert store.equivalent_primary_document(mirror, current)
