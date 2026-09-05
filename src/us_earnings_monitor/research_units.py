from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Any, Iterable

from .models import Evidence

TRANSCRIPT_MARKERS = (
    "transcript", "earnings call", "conference call", "prepared remarks", "question-and-answer", "q&a",
)
QA_START_MARKERS = (
    "question-and-answer session", "question and answer session", "questions and answers", "q&a session",
    "we will now begin the question-and-answer", "we'll now begin the question-and-answer",
    "we will now take questions", "we'll now take questions",
)
RELEVANT_FILING_TERMS = (
    "revenue", "net sales", "gross margin", "operating margin", "operating income", "guidance", "outlook",
    "free cash flow", "cash flow", "capital expenditure", "capex", "orders", "backlog", "demand", "pricing",
    "customer", "usage", "consumption", "adoption", "capacity", "utilization", "shipment", "inventory",
    "competition", "market share", "segment", "risk factor", "liquidity", "working capital",
)
# Extraction units remain large enough to preserve local semantic context, while
# batches are deliberately small so sparse evidence-card JSON cannot hit output
# limits before acknowledging every unit. This preserves 100% coverage without
# paying for retries on oversized mapper calls.
DEFAULT_UNIT_CHARS = 4_000
DEFAULT_BATCH_CHARS = 8_000
DEFAULT_BATCH_UNITS = 2


def _kind(item: Evidence) -> str:
    explicit = str((item.metadata or {}).get("document_kind") or "").casefold()
    if explicit:
        return explicit
    probe = item.title.casefold()
    if any(marker in probe for marker in TRANSCRIPT_MARKERS):
        return "transcript"
    if "presentation" in probe or "slides" in probe:
        return "presentation"
    if "10-q" in probe or "10-k" in probe or "20-f" in probe or "40-f" in probe:
        return "filing"
    if "earnings release" in probe or "financial results" in probe or "press release" in probe:
        return "financial_results"
    return "document"


def _paragraphs(text: str) -> list[str]:
    cleaned = re.sub(r"\n{3,}", "\n\n", text or "").strip()
    if not cleaned:
        return []
    parts = [part.strip() for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
    return parts or [cleaned]


def _bounded_blocks(parts: Iterable[str], max_chars: int = DEFAULT_UNIT_CHARS) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    size = 0
    for raw in parts:
        part = raw.strip()
        if not part:
            continue
        pieces = [part[i:i + max_chars] for i in range(0, len(part), max_chars)] or [part]
        for piece in pieces:
            extra = len(piece) + (2 if current else 0)
            if current and size + extra > max_chars:
                blocks.append("\n\n".join(current))
                current = []
                size = 0
            current.append(piece)
            size += len(piece) + (2 if len(current) > 1 else 0)
    if current:
        blocks.append("\n\n".join(current))
    return blocks


def _is_transcript(item: Evidence) -> bool:
    return _kind(item) in {"transcript", "qa", "prepared_remarks"} or any(
        marker in item.title.casefold() for marker in TRANSCRIPT_MARKERS
    )


def _filing_relevant(part: str) -> bool:
    lower = part.casefold()
    has_signal = any(term in lower for term in RELEVANT_FILING_TERMS)
    has_number = bool(re.search(r"(?:\$|\b)\d[\d,.]*(?:\.\d+)?%?", part))
    return has_signal or (has_number and any(term in lower for term in ("revenue", "income", "margin", "cash", "segment")))


def unitize_evidence(evidence: list[Evidence], max_chars: int = DEFAULT_UNIT_CHARS) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Turn the complete extracted corpus into bounded units without sampling.

    Transcript/prepared-remarks/Q&A text is always covered end-to-end. Long regulatory
    filings may deterministically exclude boilerplate paragraphs, but every paragraph
    classified as earnings-relevant is covered. The returned manifest makes that
    distinction explicit instead of silently claiming full raw-document coverage.
    """
    units: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    for item in evidence:
        kind = _kind(item)
        transcript = _is_transcript(item)
        raw_parts = _paragraphs(item.text)
        relevant_parts = raw_parts
        selection_mode = "full"
        if kind == "filing" and not transcript:
            selected = [part for part in raw_parts if _filing_relevant(part)]
            relevant_parts = selected or raw_parts[:1]
            selection_mode = "deterministic_relevant_filing_sections"

        phase = "prepared_remarks" if transcript else kind
        blocks: list[tuple[str, str]] = []
        if transcript:
            phase_parts: list[str] = []
            current_phase = phase
            for part in relevant_parts:
                lower = part.casefold()
                if any(marker in lower for marker in QA_START_MARKERS):
                    if phase_parts:
                        for block in _bounded_blocks(phase_parts, max_chars):
                            blocks.append((current_phase, block))
                        phase_parts = []
                    current_phase = "qa"
                phase_parts.append(part)
            if phase_parts:
                for block in _bounded_blocks(phase_parts, max_chars):
                    blocks.append((current_phase, block))
        else:
            blocks = [(kind, block) for block in _bounded_blocks(relevant_parts, max_chars)]

        source = str((item.metadata or {}).get("source") or "")
        for index, (unit_phase, text) in enumerate(blocks, start=1):
            units.append({
                "unit_id": f"{item.document_key}#u{index}",
                "document_key": item.document_key,
                "title": item.title,
                "url": item.url,
                "source": source,
                "document_kind": kind,
                "phase": unit_phase,
                "position": index,
                "text": text,
            })

        if item.structured_facts:
            units.append({
                "unit_id": f"{item.document_key}#structured",
                "document_key": item.document_key,
                "title": item.title,
                "url": item.url,
                "source": source,
                "document_kind": "structured_facts",
                "phase": "structured_facts",
                "position": 0,
                "text": json.dumps(item.structured_facts, ensure_ascii=False, default=str),
            })

        documents.append({
            "document_key": item.document_key,
            "title": item.title,
            "document_kind": kind,
            "selection_mode": selection_mode,
            "raw_chars": int((item.metadata or {}).get("raw_chars") or len(item.text or "")),
            "extracted_chars": len(item.text or ""),
            "source_truncated": bool((item.metadata or {}).get("truncated", False)),
            "raw_paragraphs": len(raw_parts),
            "relevant_paragraphs": len(relevant_parts),
            "unit_count": len(blocks) + (1 if item.structured_facts else 0),
            "transcript": transcript,
        })

    expected_ids = [unit["unit_id"] for unit in units]
    manifest = {
        "documents": documents,
        "expected_unit_ids": expected_ids,
        "expected_unit_count": len(expected_ids),
        "transcript_unit_count": sum(1 for unit in units if unit["phase"] in {"prepared_remarks", "qa"}),
        "qa_unit_count": sum(1 for unit in units if unit["phase"] == "qa"),
        "source_truncated_documents": [doc["document_key"] for doc in documents if doc["source_truncated"]],
    }
    return units, manifest


def batch_units(
    units: list[dict[str, Any]],
    max_chars: int = DEFAULT_BATCH_CHARS,
    max_units: int = DEFAULT_BATCH_UNITS,
) -> list[list[dict[str, Any]]]:
    """Pack every unit exactly once; never sample or rank away a unit."""
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    size = 0
    for unit in units:
        unit_size = len(str(unit.get("text") or ""))
        if current and (len(current) >= max_units or size + unit_size > max_chars):
            batches.append(current)
            current = []
            size = 0
        current.append(unit)
        size += unit_size
    if current:
        batches.append(current)
    return batches


def coverage_result(manifest: dict[str, Any], processed_unit_ids: Iterable[str]) -> dict[str, Any]:
    expected = list(manifest.get("expected_unit_ids") or [])
    processed = list(dict.fromkeys(str(value) for value in processed_unit_ids))
    expected_set = set(expected)
    processed_set = set(processed)
    missing = [unit_id for unit_id in expected if unit_id not in processed_set]
    unexpected = [unit_id for unit_id in processed if unit_id not in expected_set]
    ratio = 1.0 if not expected else (len(expected_set - set(missing)) / len(expected_set))
    return {
        **manifest,
        "processed_unit_ids": processed,
        "processed_unit_count": len(processed_set & expected_set),
        "missing_unit_ids": missing,
        "unexpected_unit_ids": unexpected,
        "coverage_ratio": round(ratio, 6),
        "complete": not missing and not unexpected and not manifest.get("source_truncated_documents"),
    }


def topic_inventory(cards: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for card in cards:
        counts[str(card.get("topic") or "other")] += 1
    return dict(sorted(counts.items()))
