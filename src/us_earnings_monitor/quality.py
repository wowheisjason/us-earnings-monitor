from __future__ import annotations

from datetime import datetime, timedelta

from .models import Disclosure, EarningsEvent, now_iso

PRIMARY_KINDS = {"financial_results", "financial_tables", "performance_review"}
TRANSCRIPT_KINDS = {"transcript", "qa", "prepared_remarks"}
OFFICIAL_IR_SOURCES = {"official_ir", "gemini_grounded_ir", "openai_web_ir"}
TRANSCRIPT_ENRICHMENT_SOURCES = {"alpha_vantage_transcript", "third_party_transcript"}

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
        "has_transcript_enrichment": any(doc.source in TRANSCRIPT_ENRICHMENT_SOURCES for doc in documents),
        "has_financial_tables": kinds.get("financial_tables", 0) > 0,
        "has_performance_review": kinds.get("performance_review", 0) > 0,
        "has_presentation": kinds.get("presentation", 0) > 0,
    }


def sec_first_fallback_active(event: EarningsEvent, documents: list[Disclosure]) -> bool:
    """Allow a prompt SEC-only v1 only when no transcript evidence exists."""
    manifest = source_manifest(documents)
    return bool(
        manifest["has_primary_results"]
        and not manifest["has_official_ir"]
        and not manifest["has_transcript_or_qa"]
        and event.collection_status.get("official_ir_last_attempt_incomplete")
    )


def update_collection_status(
    event: EarningsEvent,
    documents: list[Disclosure],
    now: datetime,
    *,
    official_ir_checked: bool,
    transcript_wait_hours: int = 4,
) -> dict:
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
    manifest = source_manifest(documents)
    reasons: list[str] = []
    if not manifest["has_primary_results"]:
        reasons.append("missing_primary_results")

    sec_only_v1 = sec_first_fallback_active(event, documents)
    if sec_only_v1:
        manifest = {
            **manifest,
            "transcript_status": event.collection_status.get("transcript_status", TRANSCRIPT_UNKNOWN),
            "official_ir_checked_at": event.collection_status.get("official_ir_checked_at"),
            "publication_mode": "sec_only_v1_ir_pending",
        }
        return not reasons, reasons, manifest

    transcript_status = event.collection_status.get("transcript_status", TRANSCRIPT_UNKNOWN)
    if manifest["has_transcript_or_qa"]:
        transcript_status = TRANSCRIPT_FOUND
    age = _event_age(event, now)
    if transcript_status == TRANSCRIPT_UNKNOWN and age < timedelta(hours=transcript_wait_hours):
        transcript_status = TRANSCRIPT_EXPECTED
    if transcript_status == TRANSCRIPT_EXPECTED:
        if age < timedelta(hours=transcript_wait_hours) and not manifest["has_transcript_or_qa"]:
            reasons.append("transcript_collection_window_open")
        elif event.collection_status.get("official_ir_checked_at"):
            transcript_status = TRANSCRIPT_NOT_FOUND

    # If a verified transcript fallback exists, the research report can proceed
    # even when the issuer IR page itself is unreachable. The report must still
    # retain third-party provenance and primary-source financial facts.
    if not event.collection_status.get("official_ir_checked_at") and not manifest["has_transcript_enrichment"]:
        reasons.append("official_ir_not_checked")

    if manifest["has_official_ir"]:
        publication_mode = "integrated_ir"
    elif manifest["has_transcript_enrichment"]:
        publication_mode = "integrated_transcript_ir_pending"
    else:
        publication_mode = "post_ir_check_v1"

    manifest = {
        **manifest,
        "transcript_status": transcript_status,
        "official_ir_checked_at": event.collection_status.get("official_ir_checked_at"),
        "publication_mode": publication_mode,
    }
    return not reasons, reasons, manifest


def requires_deterministic_enrichment_followup(event: EarningsEvent, documents: list[Disclosure]) -> bool:
    """Any newly collected official IR or transcript source must trigger V2."""
    enrichment_sources = OFFICIAL_IR_SOURCES | TRANSCRIPT_ENRICHMENT_SOURCES
    return bool(
        event.status in {"published", "published_sec_pending"}
        and len(documents) > event.last_analyzed_document_count
        and any(document.source in enrichment_sources for document in documents)
    )
