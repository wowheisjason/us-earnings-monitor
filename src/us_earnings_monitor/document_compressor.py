from __future__ import annotations

import re
from collections.abc import Iterable

# This module intentionally does NOT summarize. It only selects source text so
# quotes/numbers remain verbatim and downstream evidence validation still works.
_US_TERMS = (
    "revenue", "guidance", "outlook", "margin", "gross margin", "operating margin",
    "free cash flow", "cash flow", "capex", "capital expenditure", "customer", "customers",
    "demand", "orders", "backlog", "supply", "capacity", "utilization", "shipment", "shipments",
    "pricing", "price", "competition", "competitive", "market share", "ai", "xpu", "accelerator",
    "networking", "inference", "model", "usage", "consumption", "adoption", "roi", "productivity",
)
_QA_START = (
    "question-and-answer session", "question and answer session", "questions and answers",
    "we will now begin the question-and-answer", "we'll now begin the question-and-answer",
    "we will now take questions", "we'll now take questions", "q&a session",
)
_TRANSCRIPT_HINTS = ("transcript", "earnings call", "conference call", "prepared remarks", "q&a")
_PRESENTATION_HINTS = ("presentation", "supplement", "slides", "performance review", "investor")


def _clean(value: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", value or "").strip()


def _score(text: str) -> float:
    lower = text.casefold()
    keyword_hits = sum(lower.count(term) for term in _US_TERMS)
    numbers = len(re.findall(r"(?:\$|\b)\d[\d,]*(?:\.\d+)?%?", text))
    year_or_quarter = len(re.findall(r"\b(?:q[1-4]|fy\s?20\d{2}|20\d{2})\b", lower))
    return keyword_hits * 4 + min(numbers, 40) * 0.6 + year_or_quarter * 0.5


def _with_page(number: int, text: str) -> str:
    return f"[[PAGE {number}]]\n{_clean(text)}"


def _bounded_join(parts: Iterable[str], max_chars: int) -> str:
    output: list[str] = []
    used = 0
    for part in parts:
        part = _clean(part)
        if not part:
            continue
        remaining = max_chars - used
        if remaining <= 0:
            break
        value = part[:remaining]
        output.append(value)
        used += len(value) + 2
    return "\n\n".join(output)[:max_chars]


def _qa_start_index(pages: list[str]) -> int | None:
    for index, page in enumerate(pages):
        lower = page.casefold()
        if any(marker in lower for marker in _QA_START):
            return index
    return None


def compress_transcript_pages(pages: list[str], max_chars: int = 46_000) -> str:
    """Keep Q&A first, then only high-signal prepared remarks.

    Q&A gets the majority of the token budget because it is the least redundant
    and usually contains the highest-alpha management answers. No model call is
    used here.
    """
    pages = [_clean(page) for page in pages if _clean(page)]
    if not pages:
        return ""
    qa_index = _qa_start_index(pages)
    if qa_index is None:
        # Some vendor transcripts omit an explicit Q&A heading. Preserve the
        # tail plus a few high-signal earlier pages instead of the entire call.
        tail_start = max(1, len(pages) - max(4, len(pages) // 3))
        qa_pages = list(range(tail_start, len(pages)))
        prepared_pool = list(range(0, tail_start))
    else:
        qa_pages = list(range(qa_index, len(pages)))
        prepared_pool = list(range(0, qa_index))

    qa_budget = int(max_chars * 0.68)
    prepared_budget = max_chars - qa_budget
    qa_text = _bounded_join((_with_page(i + 1, pages[i]) for i in qa_pages), qa_budget)

    ranked = sorted(prepared_pool, key=lambda i: (_score(pages[i]), -i), reverse=True)
    chosen = sorted(set(([0] if prepared_pool else []) + ranked[:5]))
    prepared = _bounded_join((_with_page(i + 1, pages[i]) for i in chosen), prepared_budget)
    return _bounded_join((qa_text, prepared), max_chars)


def compress_presentation_pages(pages: list[str], max_chars: int = 30_000) -> str:
    """Select page-level operating/guidance evidence from decks without AI."""
    pages = [_clean(page) for page in pages if _clean(page)]
    if not pages:
        return ""
    ranked = sorted(range(len(pages)), key=lambda i: (_score(pages[i]), -i), reverse=True)
    # Keep cover/context + top operating pages. 12 pages is enough for most
    # earnings decks while avoiding 30-80 page full-deck prompts.
    chosen = sorted(set([0, 1 if len(pages) > 1 else 0] + ranked[:10]))
    return _bounded_join((_with_page(i + 1, pages[i]) for i in chosen), max_chars)


def compress_document_pages(pages: list[str], max_chars: int = 24_000) -> str:
    pages = [_clean(page) for page in pages if _clean(page)]
    if not pages:
        return ""
    ranked = sorted(range(len(pages)), key=lambda i: (_score(pages[i]), -i), reverse=True)
    chosen = sorted(set([0] + ranked[:8]))
    return _bounded_join((_with_page(i + 1, pages[i]) for i in chosen), max_chars)


def compress_pdf_pages(pages: list[str], document_kind: str = "", title: str = "") -> str:
    probe = f"{document_kind} {title}".casefold()
    if document_kind in {"transcript", "qa", "prepared_remarks"} or any(hint in probe for hint in _TRANSCRIPT_HINTS):
        return compress_transcript_pages(pages)
    if document_kind in {"presentation", "supplement", "performance_review"} or any(hint in probe for hint in _PRESENTATION_HINTS):
        return compress_presentation_pages(pages)
    return compress_document_pages(pages)


def compress_text(text: str, document_kind: str = "", title: str = "") -> str:
    cleaned = _clean(text)
    probe = f"{document_kind} {title}".casefold()
    if document_kind in {"transcript", "qa", "prepared_remarks"} or any(hint in probe for hint in _TRANSCRIPT_HINTS):
        # Paragraph pseudo-pages preserve position while applying the same Q&A-first policy.
        chunks = [part for part in re.split(r"\n\s*\n", cleaned) if part.strip()]
        pseudo_pages = ["\n\n".join(chunks[i:i + 12]) for i in range(0, len(chunks), 12)]
        return compress_transcript_pages(pseudo_pages)
    chunks = re.split(r"(?:\n\s*\n|(?<=[.!?])\s+)", cleaned)
    selected = [chunk.strip() for chunk in chunks if any(term in chunk.casefold() for term in _US_TERMS)]
    return _bounded_join(selected or [cleaned], 24_000)
