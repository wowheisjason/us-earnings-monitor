from __future__ import annotations

from numbers import Real


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        return float(value)
    return None


def validate_extracted_facts(facts: dict) -> list[str]:
    """Validate only invariants that are safe to check without interpretation."""
    issues: list[str] = []

    consensus = facts.get("market_consensus") or []
    if consensus:
        issues.append("market_consensus_present_without_external_consensus_provider")

    for index, item in enumerate(facts.get("guidance") or []):
        low = item.get("low")
        midpoint = item.get("midpoint")
        high = item.get("high")
        if (low is None) != (high is None):
            issues.append(f"guidance[{index}]_incomplete_range")
        low_n, mid_n, high_n = _number(low), _number(midpoint), _number(high)
        if low_n is not None and mid_n is not None and high_n is not None:
            expected = (low_n + high_n) / 2
            tolerance = max(1e-9, abs(expected) * 0.002)
            if abs(mid_n - expected) > tolerance:
                issues.append(f"guidance[{index}]_midpoint_not_range_average")

    for index, item in enumerate(facts.get("cash_flow_and_capex") or []):
        metric = str(item.get("metric") or "").casefold()
        metric_type = str(item.get("metric_type") or "").casefold()
        reconciliation = item.get("reconciliation") or []
        is_adjusted = "adjusted" in metric or metric_type == "adjusted_fcf"
        if "adjusted" in metric and metric_type not in {"adjusted_fcf", "other_adjusted", "adjusted"}:
            issues.append(f"cash_flow_and_capex[{index}]_adjusted_metric_taxonomy_mismatch")
        if metric_type == "standard_fcf" and "adjusted" in metric:
            issues.append(f"cash_flow_and_capex[{index}]_adjusted_fcf_mislabeled_standard")
        if is_adjusted and not reconciliation:
            issues.append(f"cash_flow_and_capex[{index}]_adjusted_metric_missing_reconciliation")

    return issues


def validate_report_text(text: str) -> list[str]:
    """Reject deterministic presentation errors that an LLM auditor can miss.

    Reports preserve source USD million/billion units. This avoids the exact
    10x conversion error observed when an audited Dell preview turned $10.531B
    into 10.531 億美元 while still receiving an auditor score of 100.
    """
    issues: list[str] = []
    forbidden_units = ("億美元", "兆美元", "億美金", "兆美金")
    if any(token in text for token in forbidden_units):
        issues.append("report_contains_model_generated_currency_unit_conversion")
    if "應為" in text or "更正為" in text:
        issues.append("report_contains_self_correction_language")
    return issues
