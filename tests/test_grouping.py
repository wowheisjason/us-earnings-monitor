from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.grouping import align_companion_periods, attach, event_id, infer_period, ready_for_analysis
from us_earnings_monitor.models import Disclosure, EarningsEvent


def disclosure(title: str, source_id: str = "a", **kwargs) -> Disclosure:
    return Disclosure("fixture", source_id, "SPCX", title, "2026-08-04T16:00:00-04:00", "https://example.test/a", **kwargs)


def test_multiple_documents_share_one_event():
    now = datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York"))
    first = disclosure("SpaceX Form 10-Q — Q2 2026 Financial Results", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    second = disclosure("SpaceX Form 8-K — Q2 2026 Earnings Presentation", "b", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    assert event_id(first) == "SPCX_2026-06-30_Q2"
    event = attach(None, first, now)
    event = attach(event, second, now)
    assert event is not None and event.event_id == "SPCX_2026-06-30_Q2"
    assert event.documents == ["fixture:a", "fixture:b"]


def test_period_and_final_window():
    assert infer_period(disclosure("FY2026 Q2 Financial Results")) == (2026, "Q2")
    assert not ready_for_analysis(None, datetime(2026, 8, 4, 17, 30, tzinfo=ZoneInfo("America/New_York")))
    assert ready_for_analysis(None, datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York")))


def test_only_daily_20h_run_closes_event_aggregation_window():
    event = EarningsEvent("SPCX_2026-06-30_Q2", "SPCX", 2026, "Q2", "2026-08-04T16:00:00-04:00", period_end="2026-06-30")
    assert not ready_for_analysis(event, datetime(2026, 8, 4, 17, 30, tzinfo=ZoneInfo("America/New_York")))
    assert ready_for_analysis(event, datetime(2026, 8, 4, 20, 0, tzinfo=ZoneInfo("America/New_York")))


def test_annual_period():
    assert infer_period(disclosure("FY2026 Annual Results")) == (2026, "Q4")


def test_same_day_earnings_release_uses_10q_anchor_period():
    result = disclosure("SpaceX Form 10-Q — Q2 2026 Financial Results", "result", fiscal_year=2026, quarter="Q2", period_end="2026-06-30")
    release = disclosure("SpaceX Form 8-K — Earnings Release", "release")
    align_companion_periods([result, release])
    assert event_id(result) == "SPCX_2026-06-30_Q2"
    assert event_id(release) == "SPCX_2026-06-30_Q2"

