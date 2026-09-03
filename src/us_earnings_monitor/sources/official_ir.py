from __future__ import annotations

import hashlib
import logging
import os
import re
from datetime import date, datetime, timedelta
from urllib.parse import urljoin, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from ..grouping import classify_document, infer_period
from ..models import Company, Disclosure, EarningsEvent

LOG = logging.getLogger(__name__)

_IR_TERMS = (
    "決算", "業績", "説明", "質疑", "qa", "q&a", "transcript", "financial results",
    "earnings", "presentation", "supplement", "briefing", "fact book", "quarterly",
    "quarter", "investor relations", "shareholder", "financial tables", "performance review",
    "prepared remarks", "webcast", "conference call",
)
_DOCUMENT_TERMS = (
    "qa", "q&a", "transcript", "presentation", "supplement", "financial tables",
    "performance review", "prepared remarks", "earnings release", "press release",
    "webcast", "audio replay", "replay",
)
_COMPANION_KINDS = {"qa", "transcript", "presentation", "supplement", "financial_tables", "performance_review", "prepared_remarks"}
_DOCUMENT_SUFFIXES = (".pdf", ".htm", ".html", ".xlsx", ".xls", ".txt")
_HTML_SUFFIXES = ("", ".htm", ".html", ".php", ".aspx")
_DATE_PATTERNS = (
    re.compile(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})日?"),
    re.compile(r"(20\d{2})(\d{2})(\d{2})"),
)


def _canonical_url(value: str) -> str:
    parsed = urlparse(value)
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, "", parsed.query, ""))


def _host_allowed(candidate: str, configured_urls: list[str]) -> bool:
    host = (urlparse(candidate).hostname or "").lower()
    allowed = {(urlparse(item).hostname or "").lower() for item in configured_urls}
    return bool(host) and any(host == item or host.endswith("." + item) for item in allowed if item)


def _nearby_date(value: str) -> date | None:
    for index, pattern in enumerate(_DATE_PATTERNS):
        match = pattern.search(value)
        if not match:
            continue
        try:
            parts = tuple(int(part) for part in match.groups())
            # IR index pages commonly render release dates as 09/02/26.
            # Treat these as publication dates, never as a fiscal period.
            if index == 2:
                month, day_value, short_year = parts
                return date(2000 + short_year, month, day_value)
            return date(*parts)
        except ValueError:
            pass
    return None


def _link_context(anchor) -> str:
    pieces = [anchor.get_text(" ", strip=True)]
    heading = anchor.find_previous(["h1", "h2", "h3", "h4", "dt"])
    if heading is not None:
        pieces.append(heading.get_text(" ", strip=True))
    parent = anchor.find_parent(["li", "tr", "article", "section", "div", "p"])
    if parent is not None:
        pieces.append(parent.get_text(" ", strip=True))
    return " ".join(dict.fromkeys(piece for piece in pieces if piece))


def _looks_like_document(target: str, context: str) -> bool:
    path = urlparse(target).path.casefold()
    if path.endswith(_DOCUMENT_SUFFIXES):
        return True
    return any(term in context.casefold() for term in _DOCUMENT_TERMS)


def _looks_like_event_page(target: str, context: str) -> bool:
    path = urlparse(target).path.casefold()
    suffix = "." + path.rsplit(".", 1)[-1] if "." in path.rsplit("/", 1)[-1] else ""
    if suffix not in _HTML_SUFFIXES:
        return False
    lower = context.casefold()
    return any(term in lower for term in ("earnings", "financial results", "quarterly results", "results", "fiscal year", "quarter"))


class OfficialIrAdapter:
    """Event-triggered allowlisted IR crawler with one-level event-page discovery."""

    source_name = "official_ir"
    timeout = 20

    def __init__(self, events: list[EarningsEvent], session: requests.Session | None = None):
        self.events_by_ticker: dict[str, list[EarningsEvent]] = {}
        for event in events:
            self.events_by_ticker.setdefault(event.ticker, []).append(event)
        self.session = session or requests.Session()
        self.headers = {"User-Agent": os.getenv("USER_AGENT", "us-earnings-monitor/0.2")}
        self.checked_tickers: set[str] = set()
        self.partial_failure_tickers: set[str] = set()
        self.failed_tickers: set[str] = set()

    @staticmethod
    def _matching_event(context: str, events: list[EarningsEvent], day: date) -> EarningsEvent | None:
        probe = Disclosure("official_ir", "probe", None, context, day.isoformat(), "")
        fiscal_year, quarter = infer_period(probe)
        if fiscal_year and quarter:
            matches = [event for event in events if (event.fiscal_year, event.quarter) == (fiscal_year, quarter)]
            return matches[0] if len(matches) == 1 else None

        kind = classify_document(context)
        linked_date = _nearby_date(context)
        if kind in _COMPANION_KINDS and len(events) == 1 and linked_date is not None:
            return events[0] if abs((day - linked_date).days) <= 14 else None

        # Some SEC 8-K exhibits do not expose FY/Q in the filing title.  When
        # there is exactly one fresh event for the issuer, a dated official IR
        # release/event page is still safe to attach.  This deliberately does
        # not inherit an undated link from a broad historical index.
        if len(events) == 1 and linked_date is not None:
            fresh_terms = ("earnings", "financial results", "quarterly results", "conference call")
            if any(term in context.casefold() for term in fresh_terms) and abs((day - linked_date).days) <= 7:
                return events[0]
        return None

    def _parse_page(
        self,
        company: Company,
        page_url: str,
        day: date,
        configured_urls: list[str],
        allow_event_links: bool,
    ) -> tuple[list[Disclosure], list[str]]:
        response = self.session.get(page_url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        events = self.events_by_ticker.get(company.ticker, [])
        found: list[Disclosure] = []
        event_pages: list[str] = []
        seen_urls: set[str] = set()

        # Only an explicit H1 may define the whole page's fiscal event. Broad
        # IR indexes often contain many H2 quarter headings and must not inherit
        # the first quarter across unrelated historical assets.
        h1 = soup.find("h1")
        page_heading = h1.get_text(" ", strip=True) if h1 is not None else ""
        page_event = self._matching_event(page_heading, events, day) if page_heading else None

        for anchor in soup.select("a[href]"):
            context = _link_context(anchor)
            if not any(term in context.casefold() for term in _IR_TERMS):
                continue
            target = _canonical_url(urljoin(page_url, anchor["href"]))
            if target in seen_urls or not _host_allowed(target, configured_urls):
                continue
            seen_urls.add(target)

            event = self._matching_event(context, events, day) or page_event
            if event is None:
                continue

            # An HTML earnings page may itself be useful evidence AND contain
            # additional assets. Follow it before classifying it as a document.
            if allow_event_links and _looks_like_event_page(target, context):
                event_pages.append(target)

            if _looks_like_document(target, context):
                anchor_text = anchor.get_text(" ", strip=True)
                title = anchor_text if any(term in anchor_text.casefold() for term in _IR_TERMS) else context[:200]
                source_id = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
                path = urlparse(target).path
                suffix = path.rsplit(".", 1)[-1].lower() if "." in path.rsplit("/", 1)[-1] else "unknown"
                found.append(Disclosure(
                    source=self.source_name,
                    source_id=source_id,
                    ticker=company.ticker,
                    title=title,
                    published_at=f"{day.isoformat()}T00:00:00-04:00",
                    url=target,
                    document_url=target,
                    fiscal_year=event.fiscal_year,
                    quarter=event.quarter,
                    period_end=event.period_end,
                    metadata={"service": self.source_name, "format": suffix, "index_url": page_url},
                ))

        return found, event_pages

    def _page(self, company: Company, index_url: str, day: date) -> tuple[list[Disclosure], bool]:
        configured_urls = [item for item in [company.ir_index_url, *company.ir_additional_urls] if item]
        found, event_pages = self._parse_page(company, index_url, day, configured_urls, allow_event_links=True)
        complete = True

        for event_page in list(dict.fromkeys(event_pages))[:8]:
            try:
                child_docs, _ = self._parse_page(company, event_page, day, configured_urls, allow_event_links=False)
                found.extend(child_docs)
            except Exception as exc:  # noqa: BLE001
                complete = False
                LOG.warning("IR event page unavailable for %s (%s): %s", company.ticker, event_page, exc)

        deduped: list[Disclosure] = []
        seen: set[str] = set()
        for item in found:
            if item.document_url in seen:
                continue
            seen.add(item.document_url or "")
            deduped.append(item)
        if not deduped:
            LOG.warning("No eligible static IR documents for %s at %s; the page may require JavaScript or a company adapter.",
                        company.ticker, index_url)
        return deduped, complete

    def discover(self, companies: list[Company], day: date) -> list[Disclosure]:
        found: list[Disclosure] = []
        for company in companies:
            if company.ticker not in self.events_by_ticker or not company.ir_index_url:
                continue
            urls = [company.ir_index_url, *company.ir_additional_urls]
            successes = 0
            complete = True
            for index_url in urls:
                try:
                    page_docs, page_complete = self._page(company, index_url, day)
                    found.extend(page_docs)
                    successes += 1
                    complete = complete and page_complete
                except Exception as exc:  # noqa: BLE001
                    complete = False
                    LOG.warning("Official IR unavailable for %s (%s): %s", company.ticker, index_url, exc)
            if successes == 0:
                self.failed_tickers.add(company.ticker)
            elif successes == len(urls) and complete:
                self.checked_tickers.add(company.ticker)
            else:
                self.partial_failure_tickers.add(company.ticker)
        return found


def active_events_for_ir(events: list[EarningsEvent], now: datetime, days: int = 14) -> list[EarningsEvent]:
    """Return events within a bounded retry window measured from first detection."""
    cutoff = now - timedelta(days=days)
    selected: list[EarningsEvent] = []
    for event in events:
        if event.status not in {"collecting", "published", "published_sec_pending", "needs_human_review"}:
            continue
        try:
            if datetime.fromisoformat(event.first_seen_at) >= cutoff:
                selected.append(event)
        except (TypeError, ValueError):
            continue
    return selected
