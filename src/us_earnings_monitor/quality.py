from __future__ import annotations

from datetime import datetime, timedelta

from .models import Disclosure, EarningsEvent, now_iso

PRIMARY_KINDS = {"financial_results", "financial_tables", "performance_review"}
TRANSCRIPT_KINDS = {"transcript", "qa", "prepared_remarks"}
OFFICIAL_IR_SOURCES = {"official_ir", "gemini_grounded_ir", "openai_web_ir"}

TRANSCRIPT_FOUND = "FOUND"
TRANSCRIPT_EXPECTED = "EXPECTED_NOT_YET_AVAILABLE"
TRANSCRIPT_NOT_FOUND = "NOT_FOUND_AFTER_RETRY"
TRANSCRIPT_CONFIRMED_NONE = "CONFIRMED_NOT_PUBLISHED"
TRANSCRIPT_UNKNOWN = "UNKNOWN"


def _event_age(event: EarningsEvent, now: datetime) -> timedelta:
    try:
        first_seen = datetime.fromisoformat(event.first_seen_at)
        if first_seen.tzinfo is None and now.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=now.tzinfo)
        if now.tzinfo is not None:
            first_seen = first_seen.astimezone(now.tzinfo)
        return now - first_seen
    except (TypeError, ValueError):
        return timedelta.max


def source_manifest(documents: list[Disclosure]) -> dict:
    kinds: dict[str, int] = {}
    sources: dict[str, int] = {}
    for doc in documents:
        kinds[doc.document_kind] = kinds.get(doc.document_kind, 0) + 1
        sources[doc.source] = sources.get(doc.source, 0) + 1
    return {
        "document_count": len(documents),
        "kinds": kinds,
        "sources": sources,
        "has_primary_results": any(doc.document_kind in PRIMARY_KINDS for doc in documents),
        "has_official_ir": any(doc.source in OFFICIAL_IR_SOURCES for doc in documents),
        "has_transcript_or_qa": any(doc.document_kind in TRANSCRIPT_KINDS for doc in documents),
        "has_financial_tables": kinds.get("financial_tables", 0) > 0,
        "has_performance_review": kinds.get("performance_review", 0) > 0,
        "has_presentation": kinds.get("presentation", 0) > 0,
    }


def update_collection_status(
    event: EarningsEvent,
    documents: list[Disclosure],
    now: datetime,
    *,
    official_ir_checked: bool,
    transcript_wait_hours: int = 4,
) -> dict:
    """Persist source-discovery state separately from report-generation state."""
    manifest = source_manifest(documents)
    status = event.collection_status
    status["source_manifest"] = manifest
    status["last_collection_check_at"] = now_iso(now)
    if official_ir_checked:
        status["official_ir_checked_at"] = now_iso(now)
        status["official_ir_check_count"] = int(status.get("official_ir_check_count", 0)) + 1

    previous = status.get("transcript_status")
    if manifest["has_transcript_or_qa"]:
        transcript_status = TRANSCRIPT_FOUND
    elif previous == TRANSCRIPT_CONFIRMED_NONE:
        transcript_status = TRANSCRIPT_CONFIRMED_NONE
    elif _event_age(event, now) < timedelta(hours=transcript_wait_hours):
        transcript_status = TRANSCRIPT_EXPECTED
    elif official_ir_checked or status.get("official_ir_checked_at"):
        transcript_status = TRANSCRIPT_NOT_FOUND
    else:
        transcript_status = TRANSCRIPT_UNKNOWN

    status["transcript_status"] = transcript_status
    return status


def publication_gate(
    event: EarningsEvent,
    documents: list[Disclosure],
    now: datetime,
    transcript_wait_hours: int = 4,
) -> tuple[bool, list[str], dict]:
    """Deterministic pre-LLM completeness gate.

    Publish as soon as a transcript/Q&A is found. If no transcript is found,
    keep a short event-specific collection window open, then allow a v1 report
    after at least one completed official-IR search. A later transcript creates
    new evidence and can trigger a material v2 update.
    """
    manifest = source_manifest(documents)
    reasons: list[str] = []
    if not manifest["has_primary_results"]:
        reasons.append("missing_primary_results")

    transcript_status = event.collection_status.get("transcript_status", TRANSCRIPT_UNKNOWN)
    age = _event_age(event, now)
    if transcript_status == TRANSCRIPT_UNKNOWN and age < timedelta(hours=transcript_wait_hours):
        transcript_status = TRANSCRIPT_EXPECTED
    if transcript_status == TRANSCRIPT_EXPECTED:
        if age < timedelta(hours=transcript_wait_hours):
            reasons.append("transcript_collection_window_open")
        elif event.collection_status.get("official_ir_checked_at"):
            transcript_status = TRANSCRIPT_NOT_FOUND

    if not event.collection_status.get("official_ir_checked_at"):
        reasons.append("official_ir_not_checked")

    manifest = {**manifest, "transcript_status": transcript_status,
                "official_ir_checked_at": event.collection_status.get("official_ir_checked_at")}
    return not reasons, reasons, manifest
