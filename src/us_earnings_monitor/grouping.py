from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

from .models import Disclosure, EarningsEvent, Evidence, now_iso

_FY = re.compile(r"(?:(?:FY\s*)|(?:Fiscal\s+Year\s+))?(20\d{2})(?:年?\d{1,2}月期)?", re.IGNORECASE)
_Q = re.compile(r"(?:第?([1-4])四半期|Q([1-4])|([1-4])Q|([1-4])(?:st|nd|rd|th)\s+quarter)", re.IGNORECASE)
_Q_WORD = re.compile(r"\b(first|second|third|fourth)\s+quarter\b", re.IGNORECASE)
_Q_WORD_MAP = {"first": "1", "second": "2", "third": "3", "fourth": "4"}
_RESULT_ANCHORS = (
    "form 10-q", "form 10-k", "financial results", "earnings results", "earnings release", "quarterly results",
)


def classify_document(title: str) -> str:
    lower = title.casefold()
    if "prepared remarks" in lower:
        return "prepared_remarks"
    if "q&a" in lower or "質疑" in title:
        return "qa"
    if "transcript" in lower or "逐語" in title:
        return "transcript"
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
            own_fy, own_quarter = infer_period(item)
            # Preserve a document that already carries its own complete period;
            # fill only companions whose title/metadata cannot identify one.
            if own_fy and own_quarter and item.period_end:
                continue
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
    if status.get("official_ir_last_attempt_incomplete"):
        return True
    if not status.get("official_ir_checked_at"):
        return False
    if transcript_status in {"NOT_FOUND_AFTER_RETRY", "CONFIRMED_NOT_PUBLISHED"}:
        return True
    # Source and audit gates run in _run_analysis; Q&A absence adds no clock delay.
    return True


def repair_incomplete_event_period(event: EarningsEvent, documents: list[Disclosure],
                                   official_evidence: list[Evidence] | None = None) -> bool:
    """Repair only unpublished, incomplete periods from explicit SEC exhibit names.

    A filing/report date is not a fiscal period. Never infer from forward guidance,
    rename event IDs, modify receipts, or overwrite a complete published period.
    """
    from urllib.parse import urlparse
    if (event.status not in {"collecting", "needs_human_review"} or event.report_version
            or event.delivery or (event.fiscal_year and event.quarter)):
        return False
    candidates = []
    for doc in documents:
        if (doc.source != "sec_edgar" or doc.ticker != event.ticker
                or doc.document_kind != "financial_results"
                or str(doc.metadata.get("document_type", "")).upper() != "EX-99.1"):
            continue
        parsed = urlparse(doc.document_url or doc.url)
        if parsed.hostname not in {"www.sec.gov", "sec.gov"}:
            continue
        filename = parsed.path.rsplit("/", 1)[-1]
        # Explicit reported-quarter exhibit, e.g. a2026q4ex991-pressrelease.htm.
        match = re.fullmatch(r"a?(20\d{2})q([1-4])ex99[-_]?1[-_](?:pressrelease|earningsrelease)\.html?", filename, re.I)
        if match:
            candidates.append((int(match[1]), "Q" + match[2], doc.key, filename))
    periods = {(fy, quarter) for fy, quarter, _, _ in candidates}
    if len(periods) != 1:
        return False
    fy, quarter = next(iter(periods))
    if event.quarter and event.quarter != quarter:
        return False
    corroboration = []
    if event.fiscal_year and event.fiscal_year != fy:
        by_key = {item.document_key: item for item in (official_evidence or [])}
        ordinal = {"Q1": "first", "Q2": "second", "Q3": "third", "Q4": "fourth"}[quarter]
        qnum = quarter[-1]
        period = rf"(?:fiscal\s+)?{ordinal}\s+quarter(?:\s+and\s+(?:the\s+)?full\s+(?:fiscal\s+)?year)?\s+(?:of\s+)?(?:fiscal(?:\s+year)?\s+|fy\s*)?{fy}"
        patterns = (
            rf"\b(?:reports|reported|announces|announced)\s+(?:financial\s+)?results\s+for\s+(?:the\s+)?{period}\b",
            rf"\b(?:fq{qnum}|q{qnum})\s+(?:fiscal\s+|fy\s*)?{fy}\s+(?:financial\s+|earnings\s+)?results\b",
            rf"\b{period}\s+(?:financial\s+|earnings\s+)?results\b",
        )
        for _, _, key, _ in candidates:
            item = by_key.get(key)
            if item is None:
                continue
            # Only the reported-results heading in the matching primary document,
            # not an arbitrary period mentioned later in outlook or comparison.
            header = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", item.text[:2500])).strip()
            for pattern in patterns:
                for match in re.finditer(pattern, header, re.I):
                    context = header[max(0, match.start() - 100):match.end()]
                    if re.search(r"\b(?:guidance|outlook|expects?|expected|forecast|will|anticipates?|projects?)\b", context, re.I):
                        continue
                    corroboration.append({"document_key": key, "reported_period_anchor": match.group(0)})
        if not corroboration:
            # Filename is insufficient to override existing fiscal metadata.
            return False
    previous = {"fiscal_year": event.fiscal_year, "quarter": event.quarter, "period_end": event.period_end}
    event.fiscal_year, event.quarter = fy, quarter
    event.collection_status["period_repair"] = {
        "method": "sec_exhibit_and_reported_header" if corroboration else "explicit_sec_earnings_exhibit_filename", "previous": previous,
        "official_header_corroboration": corroboration,
        "fiscal_year": fy, "quarter": quarter,
        "evidence": [{"document_key": key, "filename": filename} for _, _, key, filename in candidates],
        "period_end_verified": False,
    }
    return True
