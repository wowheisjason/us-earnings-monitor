from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime

import requests

from ..models import Company, Disclosure, EarningsEvent


class AlphaVantageTranscriptAdapter:
    """Optional transcript enrichment; never a primary earnings-results source.

    The endpoint is only used when ALPHA_VANTAGE_API_KEY is configured. The API
    key is sent as a request parameter but is never persisted in URLs/state.
    Transcript text is stored in disclosure metadata for the current run/state.
    """

    source_name = "alpha_vantage_transcript"

    def __init__(self, api_key: str | None = None, session: requests.Session | None = None):
        self.api_key = api_key or os.getenv("ALPHA_VANTAGE_API_KEY")
        self.session = session or requests.Session()

    @staticmethod
    def _quarter(event: EarningsEvent) -> str | None:
        fy = re.search(r"20\d{2}", str(event.fiscal_year or ""))
        q = re.search(r"[1-4]", str(event.quarter or ""))
        if not fy or not q:
            return None
        return f"{fy.group(0)}Q{q.group(0)}"

    @staticmethod
    def _text(payload: dict) -> str:
        transcript = payload.get("transcript") or payload.get("Transcript") or []
        lines: list[str] = []
        if isinstance(transcript, list):
            for turn in transcript:
                if not isinstance(turn, dict):
                    continue
                speaker = str(turn.get("speaker") or turn.get("name") or "").strip()
                title = str(turn.get("title") or "").strip()
                content = str(turn.get("content") or turn.get("text") or "").strip()
                if not content:
                    continue
                label = speaker
                if title and title.casefold() not in speaker.casefold():
                    label = f"{speaker} ({title})" if speaker else title
                lines.append(f"{label}: {content}" if label else content)
        elif isinstance(transcript, str):
            lines.append(transcript)
        return "\n\n".join(lines).strip()

    def fetch(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict]:
        if not self.api_key:
            return [], {"configured": False, "provider": self.source_name}
        quarter = self._quarter(event)
        if not quarter:
            return [], {"configured": True, "provider": self.source_name, "reason": "period_unavailable"}
        response = self.session.get(
            "https://www.alphavantage.co/query",
            params={
                "function": "EARNINGS_CALL_TRANSCRIPT",
                "symbol": company.ticker,
                "quarter": quarter,
                "apikey": self.api_key,
            },
            headers={"User-Agent": "us-earnings-monitor/0.5"},
            timeout=30,
        )
        response.raise_for_status()
        payload = response.json()
        text = self._text(payload)
        if not text:
            note = payload.get("Information") or payload.get("Note") or payload.get("Error Message")
            return [], {"configured": True, "provider": self.source_name, "reason": str(note or "no_transcript")[:300]}
        source_id = hashlib.sha256(f"{company.ticker}:{quarter}:alpha-vantage".encode()).hexdigest()[:24]
        disclosure = Disclosure(
            source=self.source_name,
            source_id=source_id,
            ticker=company.ticker,
            title=f"{company.name} {quarter} Earnings Call Transcript (third-party transcript via Alpha Vantage)",
            published_at=now.isoformat(),
            url="https://www.alphavantage.co/",
            document_url=None,
            fiscal_year=event.fiscal_year,
            quarter=event.quarter,
            period_end=event.period_end,
            document_kind="transcript",
            metadata={
                "service": self.source_name,
                "format": "transcript_json",
                "transcript_text": text,
                "provenance": "third_party_transcript",
                "quarter": quarter,
            },
        )
        return [disclosure], {"configured": True, "provider": self.source_name, "document_count": 1, "quarter": quarter}
