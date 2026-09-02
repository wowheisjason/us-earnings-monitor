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
    "quarter", "investor relations", "shareholder",
)
_COMPANION_KINDS = {"qa", "transcript", "presentation", "supplement"}
_DOCUMENT_SUFFIXES = (".pdf", ".htm", ".html", ".xlsx", ".xls")
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
    for pattern in _DATE_PATTERNS:
        match = pattern.search(value)
        if match:
            try:
                return date(*(int(part) for part in match.groups()))
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


class OfficialIrAdapter:
    """Conservative, event-triggered adapter for allowlisted company IR pages.

    This adapter only reads ordinary static HTML and directly linked documents.
    It does not execute JavaScript, crawl a whole site, bypass robots/access
    controls, or follow links onto an unconfigured host.
    """

    source_name = "official_ir"
    timeout = 20

    def __init__(self, events: list[EarningsEvent], session: requests.Session | None = None):
        self.events_by_ticker: dict[str, list[EarningsEvent]] = {}
        for event in events:
            self.events_by_ticker.setdefault(event.ticker, []).append(event)
        self.session = session or requests.Session()
        self.headers = {"User-Agent": os.getenv("USER_AGENT", "us-earnings-monitor/0.1")}

    @staticmethod
    def _matching_event(context: str, events: list[EarningsEvent], day: date) -> EarningsEvent | None:
        probe = Disclosure("official_ir", "probe", None, context, day.isoformat(), "")
        fiscal_year, quarter = infer_period(probe)
        if fiscal_year and quarter:
            matches = [event for event in events if (event.fiscal_year, event.quarter) == (fiscal_year, quarter)]
            return matches[0] if len(matches) == 1 else None

        kind = classify_document(context)
        linked_date = _nearby_date(context)
        if kind not in _COMPANION_KINDS or len(events) != 1 or linked_date is None:
            return None
        # A date is required for period-less companion documents so a historical
        # IR-library link cannot be accidentally attached to the current event.
        return events[0] if abs((day - linked_date).days) <= 14 else None

    def _page(self, company: Company, index_url: str, day: date) -> list[Disclosure]:
        configured_urls = [item for item in [company.ir_index_url, *company.ir_additional_urls] if item]
        response = self.session.get(index_url, headers=self.headers, timeout=self.timeout)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "lxml")
        events = self.events_by_ticker.get(company.ticker, [])
        found: list[Disclosure] = []
        seen_urls: set[str] = set()
        for anchor in soup.select("a[href]"):
            context = _link_context(anchor)
            if not any(term in context.casefold() for term in _IR_TERMS):
                continue
            target = _canonical_url(urljoin(index_url, anchor["href"]))
            if not urlparse(target).path.casefold().endswith(_DOCUMENT_SUFFIXES):
                continue
            if target in seen_urls or not _host_allowed(target, configured_urls):
                continue
            event = self._matching_event(context, events, day)
            if event is None:
                continue
            seen_urls.add(target)
            anchor_text = anchor.get_text(" ", strip=True)
            title = anchor_text if any(term in anchor_text.casefold() for term in _IR_TERMS) else context[:200]
            source_id = hashlib.sha256(target.encode("utf-8")).hexdigest()[:24]
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
                metadata={"service": self.source_name, "format": urlparse(target).path.rsplit(".", 1)[-1].lower(),
                          "index_url": index_url},
            ))
        if not found:
            LOG.warning("No eligible static IR documents for %s at %s; the page may require JavaScript or a company adapter.",
                        company.ticker, index_url)
        return found

    def discover(self, companies: list[Company], day: date) -> list[Disclosure]:
        found: list[Disclosure] = []
        for company in companies:
            if company.ticker not in self.events_by_ticker or not company.ir_index_url:
                continue
            for index_url in [company.ir_index_url, *company.ir_additional_urls]:
                try:
                    found.extend(self._page(company, index_url, day))
                except Exception as exc:  # noqa: BLE001 - one IR site must not stop the run
                    LOG.warning("Official IR unavailable for %s (%s): %s", company.ticker, index_url, exc)
        return found


def active_events_for_ir(events: list[EarningsEvent], now: datetime, days: int = 14) -> list[EarningsEvent]:
    """Return only recent events; company IR is never polled without a primary event."""
    cutoff = now - timedelta(days=days)
    selected: list[EarningsEvent] = []
    for event in events:
        if event.status not in {"collecting", "published"}:
            continue
        timestamp = event.updated_at or event.first_seen_at
        try:
            if datetime.fromisoformat(timestamp) >= cutoff:
                selected.append(event)
        except (TypeError, ValueError):
            continue
    return selected

