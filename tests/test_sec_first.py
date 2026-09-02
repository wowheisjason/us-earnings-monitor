from datetime import datetime
from zoneinfo import ZoneInfo

from us_earnings_monitor.grouping import ready_for_analysis
from us_earnings_monitor.models import EarningsEvent

ET = ZoneInfo("America/New_York")


def test_incomplete_ir_attempt_makes_event_ready_for_sec_first_analysis():
    now = datetime(2026, 9, 2, 16, 10, tzinfo=ET)
    event = EarningsEvent("AVGO_FY2026_Q3", "AVGO", 2026, "Q3", now.isoformat())
    event.collection_status = {
        "transcript_status": "EXPECTED_NOT_YET_AVAILABLE",
        "official_ir_last_attempt_incomplete": now.isoformat(),
    }
    assert ready_for_analysis(event, now)
