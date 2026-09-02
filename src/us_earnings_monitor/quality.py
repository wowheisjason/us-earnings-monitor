from __future__ import annotations

from datetime import datetime, timedelta

from .models import Disclosure, EarningsEvent

PRIMARY_KINDS = {"financial_results", "financial_tables", "performance_review"}
TRANSCRIPT_KINDS = {"transcript", "qa", "prepared_remarks"}


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
        "has_official_ir": any(doc.source == "official_ir" for doc in documents),
        "has_transcript_or_qa": any(doc.document_kind in TRANSCRIPT_KINDS for doc in documents),
    }


def publication_gate(
    event: EarningsEvent,
    documents: list[Disclosure],
    now: datetime,
    transcript_wait_hours: int = 24,
) -> tuple[bool, list[str], dict]:
    """Deterministic pre-LLM gate.

    We do not assume every issuer publishes an official transcript. Instead,
    during the first 24 hours after a newly detected earnings event we keep
    collecting official IR material before allowing publication. This gives
    delayed transcripts/Q&A time to appear without permanently blocking
    issuers that never publish one.
    """
    manifest = source_manifest(documents)
    reasons: list[str] = []
    if not manifest["has_primary_results"]:
        reasons.append("missing_primary_results")

    try:
        first_seen = datetime.fromisoformat(event.first_seen_at)
        if first_seen.tzinfo is None and now.tzinfo is not None:
            first_seen = first_seen.replace(tzinfo=now.tzinfo)
        age = now - first_seen.astimezone(now.tzinfo) if now.tzinfo else now - first_seen
    except (TypeError, ValueError):
        age = timedelta.max

    if age < timedelta(hours=transcript_wait_hours) and not manifest["has_transcript_or_qa"]:
        reasons.append("transcript_collection_window_open")

    return not reasons, reasons, manifest
