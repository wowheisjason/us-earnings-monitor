from __future__ import annotations

import re
import unicodedata

_REQUIRED_HEADINGS = (
    "💡 投資結論與邏輯",
    "📊 關鍵數據與財測",
    "🧭 營運動能與法說攻防",
    "⚠️ 風險、反證與待驗證",
)
_LEGACY_HEADINGS = (
    "核心投資結論", "本期變化", "本季變化", "跨段資訊整合", "關鍵論述與證據",
    "財測與共識", "關鍵指標", "業務部門", "因果鏈與單位經濟", "未來展望", "法說 Q&A", "反證與未知",
)
_METRIC_TERMS = (
    "product revenue", "產品營收", "revenue", "總營收", "營收", "sales", "売上",
    "operating margin", "營業利益率", "營業利潤率", "営業利益率", "gross margin", "毛利率",
    "rpo", "backlog", "受注残", "orders", "受注", "eps", "fcf", "free cash flow",
    "capex", "設備投資", "guidance", "指引", "業績予想", "nrr", "net revenue retention",
)
_NUMBER_RE = re.compile(r"(?<![A-Za-z])[-+]?\$?\d[\d,.]*(?:\s?(?:%|bps?|bp|million|billion|trillion|m|b|bn|億|兆|萬|万))?", re.I)
_HEADING_EMOJI_RE = re.compile(r"^[💡🔄🧩📌📢📈🏢🔗🔮🎙️⚖️⚠️📊🧭\s]+")


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKC", value or "").casefold()
    value = value.replace("：", ":")
    return re.sub(r"\s+", " ", value).strip()


def _section_heading(line: str) -> str | None:
    normalized = _norm(line).rstrip(":")
    for heading in _REQUIRED_HEADINGS:
        if normalized == _norm(heading):
            return heading
    return None


def _legacy_heading_label(line: str) -> str:
    return _HEADING_EMOJI_RE.sub("", _norm(line)).rstrip(":").strip()


def structure_errors(report: str) -> list[str]:
    lines = [line.strip() for line in (report or "").splitlines() if line.strip()]
    found: list[tuple[int, str]] = []
    legacy: list[str] = []
    for index, line in enumerate(lines):
        heading = _section_heading(line)
        if heading:
            found.append((index, heading))
        legacy_probe = _legacy_heading_label(line)
        for old in _LEGACY_HEADINGS:
            if legacy_probe == _norm(old):
                legacy.append(old)
    errors: list[str] = []
    found_names = [name for _, name in found]
    missing = [name for name in _REQUIRED_HEADINGS if name not in found_names]
    if missing:
        errors.append("missing_v4_sections:" + ",".join(missing))
    if found_names and found_names != [name for name in _REQUIRED_HEADINGS if name in found_names]:
        errors.append("v4_section_order_invalid")
    if len(found_names) != len(set(found_names)):
        errors.append("duplicate_v4_section_heading")
    if legacy:
        errors.append("legacy_fragmented_sections_present:" + ",".join(sorted(set(legacy))))
    return errors


def redundancy_errors(report: str) -> list[str]:
    lines = [line.strip() for line in (report or "").splitlines() if line.strip()]
    exact_seen: dict[str, int] = {}
    metric_occurrences: dict[tuple[str, tuple[str, ...]], set[str]] = {}
    current_section = "header"
    errors: list[str] = []

    for line in lines:
        heading = _section_heading(line)
        if heading:
            current_section = heading
            continue
        stripped = re.sub(r"^[•❓💬└\-]+\s*", "", line).strip()
        normalized = _norm(stripped)
        if len(normalized) >= 28:
            exact_seen[normalized] = exact_seen.get(normalized, 0) + 1
        numbers = tuple(sorted({_norm(token) for token in _NUMBER_RE.findall(stripped)}))
        if not numbers:
            continue
        lower = _norm(stripped)
        metric = next((term for term in _METRIC_TERMS if _norm(term) in lower), None)
        if metric:
            key = (_norm(metric), numbers[:4])
            metric_occurrences.setdefault(key, set()).add(current_section)

    for text, count in exact_seen.items():
        if count >= 2:
            errors.append("duplicate_bullet:" + text[:80])
    for (metric, numbers), sections in metric_occurrences.items():
        if len(sections) >= 3:
            errors.append(f"metric_repeated_across_sections:{metric}:{'/'.join(numbers)}")
    return errors[:12]


def report_quality_errors(report: str) -> tuple[list[str], list[str]]:
    return structure_errors(report), redundancy_errors(report)


V4_OUTPUT_CONTRACT = r"""
BUY-SIDE TELEGRAM V4 OUTPUT CONTRACT — this overrides any older multi-section Telegram template above.
After the company/period header, output EXACTLY these four top-level sections, in this order:
1. 💡 投資結論與邏輯:
2. 📊 關鍵數據與財測:
3. 🧭 營運動能與法說攻防:
4. ⚠️ 風險、反證與待驗證:
Do not emit the old separate sections such as 本期變化、跨段資訊整合、關鍵論述、關鍵指標、因果鏈、未來展望、法說 Q&A.

Reading-efficiency rules:
- Section 1: maximum 3 bullets. State the thesis-changing signal and causal mechanism. Do not repeat a data table. A thesis-critical number may appear here once, but do not repeat several guidance/RPO/margin figures already shown in Section 2.
- Section 2 comes immediately after the executive summary. Consolidate results, explicit change vs prior period/prior guidance, guidance, and expectation context into ONE compact table/bullet set. Each metric/value pair should appear here once. If verified external consensus is absent, use exactly one concise line: 「外部市場共識未納入，本報告不判定 Beat/Miss。」 Never invent consensus, valuation, price targets, or multiples.
- Section 3: combine operating drivers, customer proof, cross-context synthesis and management Q&A. If facts.qa has 2+ material exchanges, select 2–3 DIFFERENT debate topics (for example demand durability, margin/cost, pricing/optimization, competition/capacity) and show question → management response → investment read-through. If only one material Q&A exists, use one. If no verified Q&A exists, state that once and do not fabricate it. Customer cases are included only when they add quantified or mechanism-level evidence.
- Section 4: maximum 4 bullets. Preserve contradictions, weak links, downside mechanisms and what evidence would confirm/falsify the thesis.
- Target <=2600 Traditional-Chinese characters before sources.
- Cross-section de-duplication is mandatory: the same metric/value pair may appear in at most two sections total and should normally appear only in Section 2. Do not paraphrase the same conclusion in multiple sections.
- Keep facts vs management claims vs analyst inference distinct. Keep accounting taxonomy and full guidance ranges exact.
"""

V4_AUDITOR_CONTRACT = r"""
BUY-SIDE V4 AUDITOR ADDENDUM:
Treat report architecture and redundancy as publication-quality requirements. corrected_telegram_draft must follow the exact four-section order above. Reject fragmented legacy 8–11 section output, data buried after qualitative discussion, repeated metric/value pairs across 3+ sections, or the same conclusion paraphrased repeatedly. If facts.qa contains at least two genuinely material exchanges, prefer 2–3 diverse Q&A attack/defense topics rather than a single marketing-style anecdote. Absence of external consensus or valuation is NOT an error when no verified source is supplied; inventing either is a critical error.
"""


def stage_contract(stage: str) -> str:
    lower = (stage or "").casefold()
    if "auditor" in lower:
        return V4_OUTPUT_CONTRACT + "\n" + V4_AUDITOR_CONTRACT
    if "analyst" in lower or "revision" in lower:
        return V4_OUTPUT_CONTRACT
    return ""


def harden_audit_with_report_quality(value: dict) -> dict:
    draft = str(value.get("corrected_telegram_draft") or "")
    if not draft:
        return value
    structure, redundancy = report_quality_errors(draft)
    value["structure_errors"] = structure
    value["redundancy_errors"] = redundancy
    if structure or redundancy:
        critical = list(value.get("critical_issues") or [])
        if structure and "deterministic_v4_gate:structure" not in critical:
            critical.append("deterministic_v4_gate:structure")
        if redundancy and "deterministic_v4_gate:redundancy" not in critical:
            critical.append("deterministic_v4_gate:redundancy")
        value["critical_issues"] = critical
        value["pass"] = False
        value["overall_score"] = min(int(value.get("overall_score", 0) or 0), 85)
    return value
