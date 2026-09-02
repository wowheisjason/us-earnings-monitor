from __future__ import annotations

from numbers import Real


def _number(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, Real):
        return float(value)
    return None


def validate_extracted_facts(facts: dict) -> list[str]:
    """Validate only invariants that are safe to check without interpretation.

    This intentionally avoids guessing units, comparison periods, or accounting
    definitions. Anything requiring semantic judgment remains with the auditor.
    """
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
