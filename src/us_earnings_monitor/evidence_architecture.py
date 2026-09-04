from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Iterable

from .models import Evidence

TRANSCRIPT_MARKERS = (
    "transcript", "q&a", "question-and-answer", "question and answer",
    "prepared remarks", "earnings call", "conference call",
)
QA_START_MARKERS = (
    "question-and-answer session", "question and answer session", "questions and answers", "q&a session",
    "we will now begin the question-and-answer", "we'll now begin the question-and-answer",
    "we will now take questions", "we'll now take questions",
)
SECTION_CHARS = 7_500
GROUP_CHARS = 22_000
MAX_GROUPS = 6
BALANCED_TOTAL_CHARS = 72_000
QA_TOTAL_CHARS = 30_000


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().casefold()


def _is_transcript(item: Evidence) -> bool:
    value = f"{item.title}\n{item.text[:4000]}".casefold()
    return any(marker in value for marker in TRANSCRIPT_MARKERS)


def _paragraph_chunks(text: str, max_chars: int = SECTION_CHARS) -> list[str]:
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    if not paragraphs:
        paragraphs = [text.strip()] if text.strip() else []
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for paragraph in paragraphs:
        pieces = [paragraph[i:i + max_chars] for i in range(0, len(paragraph), max_chars)] or [paragraph]
        for piece in pieces:
            extra = len(piece) + (2 if current else 0)
            if current and size + extra > max_chars:
                chunks.append("\n\n".join(current))
                current = []
                size = 0
            current.append(piece)
            size += len(piece) + (2 if len(current) > 1 else 0)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def sectionize(evidence: Iterable[Evidence]) -> list[dict]:
    """Create position-aware sections and only mark Q&A after a real Q&A boundary."""
    sections: list[dict] = []
    for item in evidence:
        text = re.sub(r"\n{3,}", "\n\n", item.text or "").strip()
        if not text and not item.structured_facts:
            continue
        transcript = _is_transcript(item)
        title_is_qa = any(marker in item.title.casefold() for marker in ("q&a", "question-and-answer", "questions and answers"))
        qa_started = title_is_qa
        chunks = _paragraph_chunks(text)
        for index, chunk in enumerate(chunks):
            lower = chunk.casefold()
            if any(marker in lower for marker in QA_START_MARKERS):
                qa_started = True
            kind = "qa" if transcript and qa_started else ("transcript" if transcript else "document")
            sections.append({
                "section_id": f"{item.document_key}#s{index + 1}",
                "document_key": item.document_key,
                "title": item.title,
                "url": item.url,
                "kind": kind,
                "position": index + 1,
                "text": chunk,
                "structured_facts": item.structured_facts if index == 0 else [],
            })
        if not chunks and item.structured_facts:
            sections.append({
                "section_id": f"{item.document_key}#facts",
                "document_key": item.document_key,
                "title": item.title,
                "url": item.url,
                "kind": "structured_facts",
                "position": 0,
                "text": "",
                "structured_facts": item.structured_facts,
            })
    return sections


def _pack(sections: list[dict]) -> list[list[dict]]:
    groups: list[list[dict]] = []
    current: list[dict] = []
    size = 0
    for section in sections:
        section_size = len(section["text"])
        if current and size + section_size > GROUP_CHARS:
            groups.append(current)
            current = []
            size = 0
        current.append(section)
        size += section_size
    if current:
        groups.append(current)
    return groups


def _sample_groups(groups: list[list[dict]], slots: int) -> list[list[dict]]:
    if slots <= 0 or not groups:
        return []
    if len(groups) <= slots:
        return groups
    if slots == 1:
        return [groups[-1]]
    indexes = sorted({round(i * (len(groups) - 1) / (slots - 1)) for i in range(slots)})
    return [groups[index] for index in indexes]


def extraction_groups(evidence: list[Evidence]) -> list[list[dict]]:
    """Bound map calls while guaranteeing that real Q&A is never sampled away."""
    sections = sectionize(evidence)
    total = sum(len(section["text"]) for section in sections)
    if total <= GROUP_CHARS:
        return [sections] if sections else [[]]

    qa_sections = [section for section in sections if section["kind"] == "qa"]
    other_sections = [section for section in sections if section["kind"] != "qa"]
    qa_groups = _pack(qa_sections)
    other_groups = _pack(other_sections)

    if not qa_groups:
        return _sample_groups(other_groups, MAX_GROUPS)

    # Reserve up to half the map budget for Q&A, which is the least redundant
    # and highest-alpha material. Remaining slots cover the rest of the corpus.
    qa_slots = min(len(qa_groups), max(2, MAX_GROUPS // 2))
    selected_qa = _sample_groups(qa_groups, qa_slots)
    selected_other = _sample_groups(other_groups, MAX_GROUPS - len(selected_qa))
    return selected_other + selected_qa


def sections_json(sections: list[dict]) -> str:
    return json.dumps({"sections": sections}, ensure_ascii=False)


def qa_evidence_payload(evidence: list[Evidence], total_chars: int = QA_TOTAL_CHARS) -> dict:
    qa = [section for section in sectionize(evidence) if section["kind"] == "qa"]
    output: list[dict] = []
    used = 0
    for section in qa:
        remaining = total_chars - used
        if remaining <= 0:
            break
        text = section["text"][:remaining]
        output.append({**section, "text": text})
        used += len(text)
    return {"sections": output}


def balanced_evidence_payload(evidence: list[Evidence], total_chars: int = BALANCED_TOTAL_CHARS) -> dict:
    sections = sectionize(evidence)
    if not sections:
        return {"documents": []}
    qa = [section for section in sections if section["kind"] == "qa"]
    other = [section for section in sections if section["kind"] != "qa"]
    ordered: list[dict] = []
    ordered.extend(qa)
    if other:
        if len(other) <= 6:
            ordered.extend(other)
        else:
            indexes = sorted({0, 1, len(other) // 3, len(other) // 2, (2 * len(other)) // 3, len(other) - 2, len(other) - 1})
            ordered.extend(other[index] for index in indexes if 0 <= index < len(other))
    output: list[dict] = []
    used = 0
    seen: set[str] = set()
    for section in ordered:
        if section["section_id"] in seen:
            continue
        seen.add(section["section_id"])
        remaining = total_chars - used
        if remaining <= 0:
            break
        text = section["text"][:remaining]
        output.append({**section, "text": text})
        used += len(text)
    return {"sections": output}


def merge_partial_extractions(parts: list[dict]) -> dict:
    merged: dict = {}
    list_values: dict[str, list] = defaultdict(list)
    for part in parts:
        for key, value in part.items():
            if isinstance(value, list):
                list_values[key].extend(value)
            elif value not in (None, "", [], {}):
                merged.setdefault(key, value)
    for key, values in list_values.items():
        unique: list = []
        fingerprints: set[str] = set()
        for value in values:
            fingerprint = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
            if fingerprint in fingerprints:
                continue
            fingerprints.add(fingerprint)
            unique.append(value)
        merged[key] = unique
    return merged


def quote_validation_issues(facts: dict, evidence: list[Evidence]) -> list[str]:
    by_key = {item.document_key: _norm(item.text) for item in evidence}
    issues: list[str] = []

    def walk(value, path: str = "facts") -> None:
        if isinstance(value, dict):
            evidence_node = value.get("evidence")
            if isinstance(evidence_node, dict):
                key = str(evidence_node.get("document_key") or "")
                quote = str(evidence_node.get("quote") or "").strip()
                if key and quote and key in by_key:
                    normalized_quote = _norm(quote)
                    if len(normalized_quote) >= 24 and normalized_quote not in by_key[key]:
                        issues.append(f"{path}:quote_not_found_in_{key}")
            for key, item in value.items():
                walk(item, f"{path}.{key}")
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(facts)
    return issues[:30]
