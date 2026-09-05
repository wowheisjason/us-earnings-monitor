from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from typing import Any

TOPIC_ORDER = (
    "demand_revenue",
    "guidance",
    "margin_unit_economics",
    "cash_capex",
    "customer_usage",
    "product_competition",
    "supply_capacity",
    "qa_management",
    "risk",
    "other",
)
_SOURCE_PRIORITY = {
    "sec_edgar": 100,
    "official_ir": 95,
    "gemini_grounded_ir": 94,
    "openai_web_ir": 94,
    "company_ir": 94,
    "alpha_vantage_transcript": 65,
    "third_party_transcript": 55,
}


def _norm(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"\s+", " ", text).strip()


def _materiality(card: dict[str, Any]) -> int:
    try:
        value = int(card.get("materiality_candidate", 1) or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(5, value))


def _source_score(card: dict[str, Any]) -> int:
    source = str(card.get("source") or "").casefold()
    provenance = str(card.get("provenance") or "").casefold()
    if provenance == "third_party_transcript":
        return _SOURCE_PRIORITY["third_party_transcript"]
    return _SOURCE_PRIORITY.get(source, 70)


def _fingerprint(card: dict[str, Any]) -> str:
    values = (
        card.get("card_type"), card.get("topic"), card.get("metric"), card.get("value"), card.get("unit"),
        card.get("period"), card.get("reported_change"), card.get("statement"), card.get("question_summary"),
        card.get("answer_summary"),
    )
    return "|".join(_norm(value) for value in values if value not in (None, ""))


def dedupe_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deduplicate only after the whole corpus has been extracted.

    When equivalent cards collide, retain the strongest provenance and then the
    higher materiality candidate. No source section is skipped before this point.
    """
    chosen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for card in cards:
        fp = _fingerprint(card)
        if not fp:
            fp = json.dumps(card, ensure_ascii=False, sort_keys=True, default=str)
        if fp not in chosen:
            chosen[fp] = card
            order.append(fp)
            continue
        current = chosen[fp]
        old_rank = (_source_score(current), _materiality(current))
        new_rank = (_source_score(card), _materiality(card))
        if new_rank > old_rank:
            chosen[fp] = card
    return [chosen[fp] for fp in order]


def _compact_card(card: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "card_id", "card_type", "topic", "statement", "metric", "value", "unit", "period",
        "comparison_value", "comparison_period", "reported_change", "low", "midpoint", "high",
        "previous_low", "previous_midpoint", "previous_high", "materiality_candidate", "fact_class",
        "speaker", "analyst", "management_speaker", "question_summary", "answer_summary", "answer_quality",
        "customer", "product", "use_case", "outcome", "quantified_result", "source", "provenance",
        "unit_id", "document_key", "quote",
    )
    output = {key: card.get(key) for key in keep if card.get(key) not in (None, "", [], {})}
    if isinstance(output.get("statement"), str):
        output["statement"] = output["statement"][:360]
    if isinstance(output.get("question_summary"), str):
        output["question_summary"] = output["question_summary"][:280]
    if isinstance(output.get("answer_summary"), str):
        output["answer_summary"] = output["answer_summary"][:420]
    if isinstance(output.get("quote"), str):
        output["quote"] = output["quote"][:240]
    return output


def select_cards(cards: list[dict[str, Any]], max_cards: int = 90) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    deduped = dedupe_cards(cards)
    mandatory: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    for card in deduped:
        card_type = str(card.get("card_type") or "")
        materiality = _materiality(card)
        if materiality >= 4 or card_type == "guidance" or (card_type == "qa" and materiality >= 3):
            mandatory.append(card)
        else:
            optional.append(card)

    selected = list(mandatory)
    selected_fps = {_fingerprint(card) for card in selected}
    topic_counts: dict[str, int] = defaultdict(int)
    for card in selected:
        topic_counts[str(card.get("topic") or "other")] += 1

    # Preserve breadth before filling by raw rank. This prevents one verbose
    # topic (for example product announcements) from crowding out cash, risk or Q&A.
    ranked_optional = sorted(
        optional,
        key=lambda card: (_materiality(card), _source_score(card), -int(card.get("position", 0) or 0)),
        reverse=True,
    )
    for topic in TOPIC_ORDER:
        if len(selected) >= max_cards:
            break
        candidates = [card for card in ranked_optional if str(card.get("topic") or "other") == topic]
        target = 4 if topic in {"demand_revenue", "guidance", "margin_unit_economics", "qa_management", "risk"} else 2
        for card in candidates:
            if topic_counts[topic] >= target or len(selected) >= max_cards:
                break
            fp = _fingerprint(card)
            if fp in selected_fps:
                continue
            selected.append(card)
            selected_fps.add(fp)
            topic_counts[topic] += 1

    for card in ranked_optional:
        if len(selected) >= max_cards:
            break
        fp = _fingerprint(card)
        if fp in selected_fps:
            continue
        selected.append(card)
        selected_fps.add(fp)

    selected = sorted(
        selected,
        key=lambda card: (
            TOPIC_ORDER.index(str(card.get("topic") or "other")) if str(card.get("topic") or "other") in TOPIC_ORDER else len(TOPIC_ORDER),
            -_materiality(card),
            str(card.get("unit_id") or ""),
        ),
    )
    diagnostics = {
        "raw_card_count": len(cards),
        "deduped_card_count": len(deduped),
        "selected_card_count": len(selected),
        "omitted_low_materiality_card_count": max(0, len(deduped) - len(selected)),
        "selected_topic_counts": dict(sorted(topic_counts.items())),
    }
    return [_compact_card(card) for card in selected], diagnostics


def build_research_packet(cards: list[dict[str, Any]], coverage: dict[str, Any]) -> dict[str, Any]:
    selected, diagnostics = select_cards(cards)
    topic_inventory: dict[str, int] = defaultdict(int)
    for card in dedupe_cards(cards):
        topic_inventory[str(card.get("topic") or "other")] += 1
    return {
        "packet_version": 5,
        "coverage": {
            "expected_unit_count": coverage.get("expected_unit_count", 0),
            "processed_unit_count": coverage.get("processed_unit_count", 0),
            "coverage_ratio": coverage.get("coverage_ratio", 0),
            "complete": coverage.get("complete", False),
            "transcript_unit_count": coverage.get("transcript_unit_count", 0),
            "qa_unit_count": coverage.get("qa_unit_count", 0),
            "missing_unit_ids": coverage.get("missing_unit_ids", []),
            "source_truncated_documents": coverage.get("source_truncated_documents", []),
        },
        "topic_inventory": dict(sorted(topic_inventory.items())),
        "selection": diagnostics,
        "cards": selected,
    }
