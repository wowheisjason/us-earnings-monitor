from __future__ import annotations

import hashlib
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ..models import Company, Disclosure, EarningsEvent

_MONTHS = "January|February|March|April|May|June|July|August|September|October|November|December|Jan\.?|Feb\.?|Mar\.?|Apr\.?|Jun\.?|Jul\.?|Aug\.?|Sep\.?|Sept\.?|Oct\.?|Nov\.?|Dec\.?”
_DATE_RE = re.compile(rf"\b({_MONTHS})\s+(\d{{1,2}}),\s+(20\d{{2}})\b", re.IGNORECASE)


class EarningsCallAiTranscriptAdapter:
    """Last-resort textual transcript enrichment with explicit third-party provenance.

    Official IR and Alpha Vantage remain preferred. This adapter is only for the
    case where the issuer confirms that an earnings call occurred but no text
    transcript is exposed to the runner. It never becomes a primary financial
    results source; reported numbers must still be anchored to SEC/official
    results by the analysis/auditor pipeline.
    """

    source_name = "earningscall_ai_transcript"
    base_url = "https://www.earningscall.ai/stock/transcript/{ticker}-{fy}-Q{quarter}"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    @staticmethod
    def _event_date(event: EarningsEvent) -> datetime | None:
        for raw in (event.period_end, event.first_seen_at):
            if not raw:
                continue
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except ValueError:
                try:
                    return datetime.strptime(str(raw)[:10], "%Y-%m-%d")
                except ValueError:
                    pass
        return None

    @staticmethod
    def _page_date(text: str) -> datetime | None:
        match = _DATE_RE.search(text)
        if not match:
            return None
        month = match.group(1).rstrip(".")
        aliases = {"Sept": "Sep"}
        month = aliases.get(month.title(), month.title())
        for fmt in ("%B %d %Y", "%b %d %Y"):
            try:
                return datetime.strptime(f"{month} {match.group(2)} {match.group(3)}", fmt)
            except ValueError:
                continue
        return None

    @staticmethod
    def _candidate_quarters(event: EarningsEvent) -> list[int]:
        if event.quarter:
            match = re.search(r"([1-4])", str(event.quarter))
            if match:
                return [int(match.group(1))]
        event_date = EarningsCallAiTranscriptAdapter._event_date(event)
        calendar_guess = ((event_date.month - 1) // 3 + 1) if event_date else 1
        return [calendar_guess, *[q for q in (1, 2, 3, 4) if q != calendar_guess]]

    def fetch(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict]:
        fiscal_year = int(event.fiscal_year) if event.fiscal_year else None
        if not fiscal_year:
            return [], {"provider": self.source_name, "reason": "fiscal_year_unavailable"}
        event_date = self._event_date(event)
        for quarter in self._candidate_quarters(event):
            url = self.base_url.format(ticker=company.ticker.upper(), fy=fiscal_year, quarter=quarter)
            try:
                response = self.session.get(
                    url,
                    headers={"User-Agent": "Mozilla/5.0 us-earnings-monitor/0.6"},
                    timeout=15,
                )
                if response.status_code == 404:
                    continue
                response.raise_for_status()
            except requests.RequestException:
                continue
            soup = BeautifulSoup(response.content, "lxml")
            title = soup.find("h1")
            title_text = title.get_text(" ", strip=True) if title else ""
            page_text = soup.get_text("\n", strip=True)
            lower = f"{title_text}\n{page_text[:2000]}".casefold()
            if company.ticker.casefold() not in lower or "earnings call transcript" not in lower:
                continue
            page_date = self._page_date(page_text)
            if event_date and page_date and abs((page_date.date() - event_date.date()).days) > 10:
                continue
            marker = title_text or f"{company.name} {fiscal_year} Q{quarter} Earnings Call Transcript"
            start = page_text.find(marker)
            transcript_text = page_text[start:] if start >= 0 else page_text
            if len(transcript_text) < 2_000:
                continue
            source_id = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            disclosure = Disclosure(
                source=self.source_name,
                source_id=source_id,
                ticker=company.ticker,
                title=f"{company.name} FY{fiscal_year} Q{quarter} Earnings Call Transcript (third-party transcription; issuer call existence independently verified)",
                published_at=now.isoformat(),
                url=url,
                document_url=url,
                fiscal_year=event.fiscal_year,
                # Preserve the parent event's grouping when its SEC 8-K did not
                # yet resolve a fiscal quarter; provider_quarter remains metadata.
                quarter=event.quarter,
                period_end=event.period_end,
                document_kind="transcript",
                metadata={
                    "service": self.source_name,
                    "format": "html",
                    "provenance": "third_party_transcript",
                    "provider_quarter": f"Q{quarter}",
                    "provider_fiscal_year": fiscal_year,
                    "verification_policy": "qualitative_call_evidence; financial_numbers_cross_check_against_SEC",
                },
            )
            return [disclosure], {
                "provider": self.source_name,
                "document_count": 1,
                "provider_quarter": f"Q{quarter}",
                "url": url,
            }
        return [], {"provider": self.source_name, "reason": "no_date_matched_transcript"}
