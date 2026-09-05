from __future__ import annotations

import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

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
_QUANT_RATE_RE = re.compile(r"(?<![\w.])([+-]?\d[\d,]*(?:\.\d+)?)\s*(%|bps?|bp)(?![A-Za-z])", re.I)
_FY_RE = re.compile(r"\bFY\s*(20)?(\d{2})\b", re.I)
_HEADING_EMOJI_RE = re.compile(r"^[💡🔄🧩📌📢📈🏢🔗🔮🎙️⚖️⚠️📊🧭\s]+")
_READTHROUGH_MARKERS = (
    "→", "顯示", "意味", "代表", "反映", "說明", "因此", "但", "而", "暗示", "表明",
    "可見", "指向", "對投資", "read-through", "意味著", "換言之", "仍不能", "尚不能",
)
_CUSTOMER_CONCEPTS = {
    "customer_base": ("總客戶", "total customer", "customer count", "customers total", "客戶數"),
    "large_customer": (">$1m", "$1m", "100 萬美元", "100萬美元", ">$10m", "$10m", "1,000 萬美元", "1000萬美元", "million ttm"),
    "retention": ("nrr", "net revenue retention", "淨營收留存", "retention rate"),
    "backlog": ("rpo", "remaining performance obligation", "剩餘履約義務"),
    "global_enterprise": ("global 2000", "forbes global 2000"),
    "usage": ("consumption", "usage", "使用量", "消費量"),
}
_STRONG_CLAIMS = {
    "pricing_power": ("定價權", "定價能力", "pricing power", "価格決定力"),
    "bottleneck_control": ("瓶頸供應商", "瓶頸供應", "bottleneck supplier", "control point", "控制點", "支配性市占", "支配的シェア", "獨占", "独占"),
}
_DIRECT_EVIDENCE = {
    "pricing_power": ("定價權", "pricing power", "価格決定力"),
    "bottleneck_control": ("bottleneck", "control point", "瓶頸", "控制點", "sole source", "single source", "exclusive", "獨占", "独占", "支配性市占", "支配的シェア"),
}


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


def _section_lines(report: str) -> dict[str, list[str]]:
    sections = {heading: [] for heading in _REQUIRED_HEADINGS}
    current: str | None = None
    for raw in (report or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        heading = _section_heading(line)
        if heading:
            current = heading
            continue
        if current:
            sections[current].append(line)
    return sections


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


def _metric_number_pairs(lines: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for line in lines:
        lower = _norm(line)
        metrics = [_norm(term) for term in _METRIC_TERMS if _norm(term) in lower]
        numbers = {_norm(token) for token in _NUMBER_RE.findall(line) if token.strip()}
        for metric in metrics[:2]:
            for number in numbers:
                pairs.add((metric, number))
    return pairs


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
    sections = _section_lines(report)
    repeated = sorted(_metric_number_pairs(sections[_REQUIRED_HEADINGS[0]]) & _metric_number_pairs(sections[_REQUIRED_HEADINGS[1]]))
    if len(repeated) > 1:
        errors.append("summary_repeats_hard_data:" + ",".join(f"{m}={n}" for m, n in repeated[:6]))
    return errors[:12]


def period_label_errors(report: str) -> list[str]:
    lines = [line.strip() for line in (report or "").splitlines() if line.strip()]
    if len(lines) <= 1:
        return []
    counts: dict[str, int] = {}
    for _century, yy in _FY_RE.findall("\n".join(lines[1:])):
        year = f"20{yy}"
        counts[year] = counts.get(year, 0) + 1
    return [f"fiscal_period_label_repeated:FY{year}:{count}" for year, count in sorted(counts.items()) if count > 2]


def _has_readthrough(line: str) -> bool:
    lower = _norm(line)
    return any(_norm(marker) in lower for marker in _READTHROUGH_MARKERS)


def interpretation_errors(report: str) -> list[str]:
    data_lines = _section_lines(report)[_REQUIRED_HEADINGS[1]]
    errors: list[str] = []
    raw_table_rows = [line for line in data_lines if "|" in line and _NUMBER_RE.search(line)]
    if len(raw_table_rows) >= 2:
        errors.append(f"raw_kpi_table_forbidden:{len(raw_table_rows)}")
    fixed_consensus = _norm("外部市場共識未納入，本報告不判定 Beat/Miss。")
    for line in data_lines:
        stripped = re.sub(r"^[•\-]+\s*", "", line).strip()
        if not _NUMBER_RE.search(stripped) or _norm(stripped) == fixed_consensus or "|" in stripped:
            continue
        if not _has_readthrough(stripped):
            errors.append("kpi_without_readthrough:" + stripped[:90])
    return errors[:8]


def _concepts_in(value: object) -> set[str]:
    text = _norm(json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value)
    found: set[str] = set()
    for concept, terms in _CUSTOMER_CONCEPTS.items():
        if any(_norm(term) in text for term in terms):
            found.add(concept)
    return found


def customer_readthrough_errors(report: str, facts: dict | None) -> list[str]:
    if not facts:
        return []
    source_concepts = _concepts_in(facts)
    if len(source_concepts) < 3:
        return []
    sections = _section_lines(report)
    for line in sections[_REQUIRED_HEADINGS[1]] + sections[_REQUIRED_HEADINGS[2]]:
        if len(_concepts_in(line)) >= 2 and _has_readthrough(line):
            return []
    return ["customer_metrics_not_triangulated:" + ",".join(sorted(source_concepts)[:6])]


def density_errors(report: str) -> list[str]:
    sections = _section_lines(report)
    errors: list[str] = []
    if len(report or "") > 2600:
        errors.append(f"report_too_long:{len(report)}")
    summary_bullets = sum(1 for line in sections[_REQUIRED_HEADINGS[0]] if line.startswith("•"))
    if summary_bullets > 3:
        errors.append(f"summary_too_many_bullets:{summary_bullets}")
    data_lines = len(sections[_REQUIRED_HEADINGS[1]])
    if data_lines > 12:
        errors.append(f"hard_data_section_too_dense:{data_lines}")
    data_bullets = sum(1 for line in sections[_REQUIRED_HEADINGS[1]] if line.startswith("•"))
    if data_bullets > 7:
        errors.append(f"hard_data_too_many_clusters:{data_bullets}")
    risk_bullets = sum(1 for line in sections[_REQUIRED_HEADINGS[3]] if line.startswith("•"))
    if risk_bullets > 4:
        errors.append(f"risk_too_many_bullets:{risk_bullets}")
    return errors


def _decimal_key(value: str) -> str:
    try:
        number = Decimal(value.replace(",", ""))
    except InvalidOperation:
        return value
    normalized = format(number.normalize(), "f")
    return "0" if normalized in {"-0", ""} else normalized


def _rate_tokens(value: object) -> set[tuple[str, str]]:
    text = json.dumps(value, ensure_ascii=False, default=str) if not isinstance(value, str) else value
    tokens: set[tuple[str, str]] = set()
    for number, unit in _QUANT_RATE_RE.findall(text):
        tokens.add((_decimal_key(number), "%" if unit == "%" else "bps"))
    return tokens


def numeric_provenance_errors(report: str, facts: dict | None, market: str | None = None) -> list[str]:
    if not facts:
        return []
    source_rates = _rate_tokens(facts)
    errors = [f"unbacked_derived_rate:{token[0]}{token[1]}" for token in sorted(_rate_tokens(report)) if token not in source_rates]
    if (market or "").strip().upper() in {"US", "USA", "美股"} and re.search(r"(?:億|兆|萬|万)美元", report):
        errors.append("us_monetary_unit_conversion_forbidden")
    return errors[:12]


def strong_claim_errors(report: str, facts: dict | None) -> list[str]:
    if not facts:
        return []
    report_norm = _norm(report)
    facts_norm = _norm(json.dumps(facts, ensure_ascii=False, default=str))
    errors: list[str] = []
    for category, claims in _STRONG_CLAIMS.items():
        if any(_norm(term) in report_norm for term in claims) and not any(_norm(term) in facts_norm for term in _DIRECT_EVIDENCE[category]):
            errors.append(f"strong_claim_without_direct_evidence:{category}")
    return errors


def report_quality_errors(report: str, facts: dict | None = None, market: str | None = None) -> dict[str, list[str]]:
    return {
        "structure_errors": structure_errors(report),
        "redundancy_errors": redundancy_errors(report),
        "period_label_errors": period_label_errors(report),
        "interpretation_errors": interpretation_errors(report),
        "customer_readthrough_errors": customer_readthrough_errors(report, facts),
        "density_errors": density_errors(report),
        "numeric_provenance_errors": numeric_provenance_errors(report, facts, market),
        "strong_claim_errors": strong_claim_errors(report, facts),
    }


V4_OUTPUT_CONTRACT = r"""
BUY-SIDE TELEGRAM V4.2 OUTPUT CONTRACT — this overrides every older Telegram template above.
After the company/period header, output EXACTLY these four top-level sections, in this order:
1. 💡 投資結論與邏輯:
2. 📊 關鍵數據與財測:
3. 🧭 營運動能與法說攻防:
4. ⚠️ 風險、反證與待驗證:

Core rule: KPI INVENTORY IS NOT ANALYSIS. Every included operating number must earn its place by explaining what changed, what it means for earnings power, and what it does NOT yet prove.

Period-label discipline:
- Put the full fiscal period (for example FY2027 Q2) in the header ONCE.
- In the body prefer 本季 / 下季 / 全年 / Q3 / 前次指引. Do not repeat FY2027/FY27 on every row.
- Repeat the full FY label only when genuinely needed to disambiguate a different fiscal year.

Section rules:
- Section 1: maximum 3 bullets. State thesis-changing signals and causal mechanisms. Use at most ONE metric/value pair repeated in Section 2.
- Section 2: 4–7 COMPACT METRIC CLUSTERS, maximum 12 non-empty lines. NO spreadsheet-style `metric | value | comparison` table. Each numeric cluster must follow: evidence/change → investment read-through → limitation/alternative where material. Prioritize: (a) growth/demand, (b) customer/usage quality, (c) margin/unit economics, (d) cash/capex, (e) guidance change. Omit low-value figures such as share count or cash balance unless they materially change the thesis.
- Customer/usage metrics must be triangulated when multiple metrics exist. Combine total customer/base growth with large-customer cohorts and NRR/RPO/usage where available. Explain breadth vs depth, expansion quality and concentration ONLY when supported. If evidence cannot separate new-logo growth from expansion or cannot establish concentration, explicitly say it cannot be determined.
- Guidance should be compressed into 下季 and 全年 clusters, not one bullet per metric/period.
- If verified external consensus is absent, include exactly one concise line: 「外部市場共識未納入，本報告不判定 Beat/Miss。」 Never invent consensus, valuation, price targets or multiples.
- Section 3: combine operating drivers, customer proof and management Q&A. If facts.qa has 2+ material exchanges, select 2–3 DIFFERENT debate topics and show question → management response → investment read-through. If no verified Q&A exists, state that once and do not fabricate it.
- Section 4: maximum 4 bullets. Preserve contradictions, weak links, downside mechanisms and the evidence that would confirm/falsify the thesis.

Evidence / efficiency rules:
- Target 1900–2300 Traditional-Chinese characters; hard ceiling 2600 before sources.
- Never calculate or invent a percentage/bps figure absent from structured facts. No mental arithmetic in the report.
- For US-market reports keep USD monetary units in source-backed $M/$B style; never convert them into 中文 億/兆/萬美元.
- Claims of pricing power, bottleneck/control-point status, monopoly/dominant share require DIRECT structured evidence. Large orders, high margins, growth or cash flow alone are insufficient.
- Cross-section de-duplication is mandatory. Do not paraphrase the same conclusion in multiple sections.
- Keep objective facts, management claims and analyst inference distinct. Preserve accounting taxonomy and full guidance ranges.
"""

V4_AUDITOR_CONTRACT = r"""
BUY-SIDE V4.2 AUDITOR ADDENDUM:
Treat architecture, evidence provenance, KPI interpretation and reading density as publication requirements. Reject: fragmented legacy output; repeated fiscal-year labels; raw KPI tables; a material KPI listed without an investment read-through; multiple customer metrics dumped separately without triangulation; hard-data section >12 content lines or >7 clusters; repeated metric/value pairs; any percentage/bps absent from structured facts; unsupported pricing-power/bottleneck/control-point/dominant-share claims; US USD values converted into 中文億/兆/萬. Absence of external consensus or valuation is NOT an error when no verified source is supplied; inventing either is critical. Prefer deleting low-value metrics and prose over adding more content.
"""


def stage_contract(stage: str) -> str:
    lower = (stage or "").casefold()
    if "auditor" in lower:
        return V4_OUTPUT_CONTRACT + "\n" + V4_AUDITOR_CONTRACT
    if "analyst" in lower or "revision" in lower:
        return V4_OUTPUT_CONTRACT
    return ""


def harden_audit_with_report_quality(value: dict, facts: dict | None = None, market: str | None = None) -> dict:
    draft = str(value.get("corrected_telegram_draft") or "")
    if not draft:
        return value
    diagnostics = report_quality_errors(draft, facts, market)
    for key, errors in diagnostics.items():
        value[key] = errors
    critical = list(value.get("critical_issues") or [])
    for key, errors in diagnostics.items():
        if errors:
            marker = f"deterministic_v4_gate:{key.removesuffix('_errors')}"
            if marker not in critical:
                critical.append(marker)
    if critical:
        value["critical_issues"] = critical
        value["pass"] = False
        value["overall_score"] = min(int(value.get("overall_score", 0) or 0), 85)
    return value
