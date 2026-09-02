from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from .models import Disclosure, EarningsEvent, now_iso

_FY = re.compile(r"(?:(?:FY\s*)|(?:Fiscal\s+Year\s+))?(20\d{2})(?:年?\d{1,2}月期)?", re.IGNORECASE)
_Q = re.compile(r"(?:第?([1-4])四半期|Q([1-4])|([1-4])Q|([1-4])(?:st|nd|rd|th)\s+quarter)", re.IGNORECASE)
_Q_WORD = re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.IGNORECASE)
_Q_WORD_MAP = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
_RESULT_ANCHORS = ("form 10-q", "form 10-k", "form 20-f", "financial results", "earnings results", "quarter results")


def classify_document(title: str) -> str:
    lower = title.casefold()
    if "q&a" in lower or "質疑" in title:
        return "qa"
    if "transcript" in lower or "逐語" in title:
        return "transcript"
    if "prepared remarks" in lower:
        return "prepared_remarks"
    if "financial tables" in lower:
        return "financial_tables"
    if "performance review" in lower:
        return "performance_review"
    if "説明会" in title or "presentation" in lower:
        return "presentation"
    if "補足" in title or "supplementary" in lower or "supplement" in lower:
        return "supplement"
    if "決算" in title or "financial results" in lower or "earnings" in lower or "press release" in lower:
        return "financial_results"
    return "other"


def infer_period(disclosure: Disclosure) -> tuple[int | None, str | None]:
    if disclosure.fiscal_year and disclosure.quarter:
        return disclosure.fiscal_year, disclosure.quarter.upper()
    title = unicodedata.normalize("NFKC", disclosure.title)
    fy_match = _FY.search(title)
    q_match = _Q.search(title)
    q_word_match = _Q_WORD.search(title)
    fiscal_year = disclosure.fiscal_year or (int(fy_match.group(1)) if fy_match else None)
    quarter = disclosure.quarter or next((g for g in (q_match.groups() if q_match else ()) if g), None)
    if not quarter and q_word_match:
        quarter = _Q_WORD_MAP[q_word_match.group(1).casefold()]
    if not quarter and fiscal_year and any(marker in title.casefold() for marker in ("annual", "full year", "year ended", "通期")):
        quarter = "4"
    return fiscal_year, f"Q{quarter}" if quarter and not str(quarter).upper().startswith("Q") else quarter


def event_id(disclosure: Disclosure) -> str | None:
    fiscal_year, quarter = infer_period(disclosure)
    if not disclosure.ticker:
        return None
    if disclosure.period_end:
        suffix = f"_{quarter}" if quarter else ""
        return f"{disclosure.ticker}_{disclosure.period_end}{suffix}"
    if not fiscal_year or not quarter:
        return None
    return f"{disclosure.ticker}_FY{fiscal_year}_{quarter}"


def title_is_earnings(title: str, patterns: list[str]) -> bool:
    value = title.casefold()
    if classify_document(title) != "other":
        return True
    return any(pattern in value for pattern in patterns)


def align_companion_periods(disclosures: list[Disclosure]) -> None:
    """Attach same-day companion documents to one unambiguous result period."""
    groups: dict[tuple[str, str], list[Disclosure]] = defaultdict(list)
    for disclosure in disclosures:
        if disclosure.ticker:
            groups[(disclosure.ticker, disclosure.published_at[:10])].append(disclosure)
    for items in groups.values():
        anchors = [item for item in items if any(marker in item.title.casefold() for marker in _RESULT_ANCHORS)]
        periods = set()
        for item in anchors:
            fiscal_year, quarter = infer_period(item)
            if fiscal_year and quarter:
                periods.add((fiscal_year, quarter, item.period_end))
        if len(periods) != 1:
            continue
        fiscal_year, quarter, period_end = next(iter(periods))
        for item in items:
            if item not in anchors:
                item.fiscal_year = fiscal_year
                item.quarter = quarter
                item.period_end = period_end


def attach(event: EarningsEvent | None, disclosure: Disclosure, now: datetime) -> EarningsEvent | None:
    eid = event_id(disclosure)
    if not eid:
        return None
    if event is None:
        fiscal_year, quarter = infer_period(disclosure)
        event = EarningsEvent(eid, disclosure.ticker or "", fiscal_year, quarter, now_iso(now),
                              period_end=disclosure.period_end)
    if disclosure.key not in event.documents:
        event.documents.append(disclosure.key)
    event.updated_at = now_iso(now)
    return event


def ready_for_analysis(event: EarningsEvent | None, now: datetime, fallback_wait_hours: int = 4) -> bool:
    """Analyze when evidence is ready; allow SEC-first v1 during IR degradation."""
    if event is None:
        return False
    status = event.collection_status
    transcript_status = status.get("transcript_status")
    if transcript_status == "FOUND":
        return True
    # IR retrieval was attempted but could not complete. The publication gate
    # will still require primary SEC results before allowing SEC-only v1.
    if status.get("official_ir_last_attempt_incomplete"):
        return True
    if not status.get("official_ir_checked_at"):
        return False
    if transcript_status in {"NOT_FOUND_AFTER_RETRY", "CONFIRMED_NOT_PUBLISHED"}:
        return True
    try:
        first_seen = datetime.fromisoformat(event.first_seen_at)
        if first_seen.tzinfo is None and now.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=now.tzinfo)
        if now.tzinfo is not None:
            first_seen = first_seen.astimezone(now.tzinfo)
        return now - first_seen >= timedelta(hours=fallback_wait_hours)
    except (TypeError, ValueError):
        return False
