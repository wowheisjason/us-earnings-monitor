from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ..models import Company, Disclosure, EarningsEvent


class PublicTranscriptAdapter:
    """Last-resort textual earnings-call enrichment from a public transcript page.

    This source is explicitly third-party and qualitative-only. It must never
    replace SEC/issuer evidence for reported financial numbers. The purpose is
    to recover management wording and analyst Q&A when the issuer publishes only
    an audio webcast and paid/API transcript sources are unavailable.
    """

    source_name = "third_party_transcript"
    provider = "earningscall.ai"
    base_url = "https://www.earningscall.ai/stock/transcript/{ticker}-{fy}-Q{q}"

    def __init__(self, session: requests.Session | None = None):
        self.session = session or requests.Session()

    @staticmethod
    def _period(event: EarningsEvent) -> tuple[int, int] | None:
        if not event.fiscal_year:
            return None
        match = re.search(r"[1-4]", str(event.quarter or ""))
        if not match:
            return None
        return int(event.fiscal_year), int(match.group(0))

    @staticmethod
    def _clean_html(blob: bytes) -> str:
        soup = BeautifulSoup(blob, "lxml")
        for tag in soup(["script", "style", "noscript", "svg", "nav", "header", "footer"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    @staticmethod
    def _looks_like_transcript(text: str, company: Company, event: EarningsEvent) -> bool:
        lower = text.casefold()
        company_tokens = [company.ticker.casefold(), company.name.casefold().split()[0]]
        has_company = any(token and token in lower for token in company_tokens)
        has_call = "earnings call transcript" in lower or "conference call" in lower
        has_dialogue = any(marker in lower for marker in (
            "question-and-answer", "question and answer", "operator", "your line is open", "open up the call for questions",
        ))
        return len(text) >= 5_000 and has_company and has_call and has_dialogue

    def fetch(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict]:
        if os.getenv("PUBLIC_TRANSCRIPT_FALLBACK_ENABLED", "1") == "0":
            return [], {"configured": False, "provider": self.provider, "reason": "disabled"}
        period = self._period(event)
        if not period:
            return [], {"configured": True, "provider": self.provider, "reason": "period_unavailable"}
        fy, q = period
        url = self.base_url.format(ticker=company.ticker.upper(), fy=fy, q=q)
        response = self.session.get(
            url,
            headers={
                "User-Agent": os.getenv("USER_AGENT", "Mozilla/5.0 earnings-monitor/0.6"),
                "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
            },
            timeout=30,
        )
        response.raise_for_status()
        text = self._clean_html(response.content)
        if not self._looks_like_transcript(text, company, event):
            return [], {"configured": True, "provider": self.provider, "reason": "page_not_verified_as_transcript", "url": url}

        source_id = hashlib.sha256(f"{company.ticker}:{fy}:Q{q}:{self.provider}".encode()).hexdigest()[:24]
        disclosure = Disclosure(
            source=self.source_name,
            source_id=source_id,
            ticker=company.ticker,
            title=f"{company.name} FY{fy} Q{q} Earnings Call Transcript (third-party: {self.provider})",
            published_at=now.isoformat(),
            url=url,
            document_url=None,
            fiscal_year=fy,
            quarter=f"Q{q}",
            period_end=event.period_end,
            document_kind="transcript",
            metadata={
                "service": self.source_name,
                "provider": self.provider,
                "format": "public_html_transcript",
                "transcript_text": text,
                "provenance": "third_party_transcript",
                "qualitative_only": True,
                "retrieved_at": now.isoformat(),
            },
        )
        return [disclosure], {
            "configured": True,
            "provider": self.provider,
            "document_count": 1,
            "url": url,
            "provenance": "third_party_transcript",
        }
