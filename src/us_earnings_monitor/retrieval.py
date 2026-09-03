from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from .models import Company, Disclosure, EarningsEvent, now_iso
from .sources import OfficialIrAdapter

LOG = logging.getLogger("us_earnings_monitor")


class FastOfficialIrAdapter(OfficialIrAdapter):
    # Investor Relations sites commonly have slower anti-bot/CDN handshakes than
    # SEC.  Eight seconds turns a reachable issuer site into a false failure.
    timeout = int(os.getenv("IR_DIRECT_TIMEOUT_SECONDS", "30"))


class IrResearchClient(Protocol):
    def research_official_ir(self, company: Company, event: EarningsEvent, now: datetime) -> tuple[list[Disclosure], dict[str, Any]]: ...


@dataclass
class RetrievalResult:
    documents: list[Disclosure] = field(default_factory=list)
    complete: bool = False
    status: dict[str, Any] = field(default_factory=dict)
    attempts: list[dict[str, Any]] = field(default_factory=list)


def should_attempt_ir(event: EarningsEvent, now: datetime) -> bool:
    """Bound expensive IR retries while allowing late transcripts to repair reports."""
    status = event.collection_status
    if status.get("transcript_status") == "FOUND" and status.get("official_ir_checked_at"):
        return False
    raw = status.get("next_ir_retry_at")
    if not raw:
        return True
    try:
        retry_at = datetime.fromisoformat(str(raw))
        if retry_at.tzinfo is None and now.tzinfo is not None:
            retry_at = retry_at.replace(tzinfo=now.tzinfo)
        return now >= retry_at.astimezone(now.tzinfo) if now.tzinfo else now >= retry_at
    except (TypeError, ValueError):
        return True


def schedule_next_ir_retry(event: EarningsEvent, now: datetime) -> None:
    """Event-local retry clock; GitHub cron merely supplies wake-up opportunities."""
    status = event.collection_status
    transcript_status = status.get("transcript_status", "UNKNOWN")
    if transcript_status == "FOUND":
        status.pop("next_ir_retry_at", None)
        return

    try:
        first_seen = datetime.fromisoformat(event.first_seen_at)
        if first_seen.tzinfo is None and now.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=now.tzinfo)
        if now.tzinfo is not None:
            first_seen = first_seen.astimezone(now.tzinfo)
        age = now - first_seen
    except (TypeError, ValueError):
        age = timedelta.max

    if age < timedelta(hours=1):
        delay = timedelta(minutes=15)
    elif age < timedelta(hours=4):
        delay = timedelta(minutes=30)
    elif age < timedelta(hours=24):
        delay = timedelta(hours=2)
    else:
        delay = timedelta(hours=12)
    status["next_ir_retry_at"] = now_iso(now + delay)


class IrRetrievalRouter:
    """Multi-provider research first, bounded deterministic direct-IR fallback last."""

    def __init__(self, research_client: IrResearchClient | None = None):
        self.research_client = research_client

    def collect(self, company: Company, event: EarningsEvent, now: datetime, *, dry_run: bool = False) -> RetrievalResult:
        attempts: list[dict[str, Any]] = []

        if not dry_run and self.research_client is not None:
            started = time.monotonic()
            try:
                documents, status = self.research_client.research_official_ir(company, event, now)
                provider_attempts = status.get("provider_attempts", []) or []
                if provider_attempts:
                    attempts.extend(provider_attempts)
                else:
                    attempts.append({
                        "provider": status.get("provider", "research_provider"),
                        "ok": bool(documents and status.get("research_complete")),
                        "seconds": round(time.monotonic() - started, 3),
                        "documents": len(documents),
                        "model": status.get("model"),
                    })
                if documents and status.get("research_complete"):
                    final_status = {**status, "attempts": attempts}
                    return RetrievalResult(documents, True, final_status, attempts)
                if attempts:
                    attempts[-1].setdefault("reason", "no_eligible_official_documents")
            except Exception as exc:  # noqa: BLE001
                attempts.append({
                    "provider": "research_chain",
                    "ok": False,
                    "seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                })
                LOG.warning("IR research chain failed for %s: %s", event.event_id, exc)

        started = time.monotonic()
        adapter = FastOfficialIrAdapter([event])
        try:
            documents = adapter.discover([company], now.date())
            complete = company.ticker in adapter.checked_tickers
            elapsed = round(time.monotonic() - started, 3)
            attempts.append({"provider": "direct_ir", "ok": complete, "seconds": elapsed,
                             "documents": len(documents),
                             "partial": company.ticker in adapter.partial_failure_tickers})
            status = {
                "research_complete": complete,
                "document_count": len(documents),
                "transcript_status": "FOUND" if any(d.document_kind in {"transcript", "qa"} for d in documents) else "UNKNOWN",
                "provider": "direct_ir",
                "attempts": attempts,
            }
            return RetrievalResult(documents, complete, status, attempts)
        except Exception as exc:  # noqa: BLE001
            elapsed = round(time.monotonic() - started, 3)
            attempts.append({"provider": "direct_ir", "ok": False, "seconds": elapsed,
                             "error": f"{type(exc).__name__}: {exc}"[:500]})
            return RetrievalResult([], False, {"research_complete": False, "provider": "none", "attempts": attempts}, attempts)
