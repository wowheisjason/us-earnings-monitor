from __future__ import annotations

from datetime import datetime


def is_likely_trading_day(now: datetime) -> bool:
    """Weekends are skipped. SEC holidays normally produce zero filings safely."""
    return now.weekday() < 5

