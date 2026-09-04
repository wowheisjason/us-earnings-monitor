from __future__ import annotations

from typing import Any

from .investor_analysis_v3 import InvestorFrameworkV3Client


def _normalize_report_header(value: str) -> str:
    return value.replace("🏢 業務部門 / 客戶ROI:", "🏢 業務部門:\n└ 客戶/ROI:")


def _harden_audit_result(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize V3 semantic audit errors into the legacy critical gate.

    The main production loop already treats critical_issues and pass=false as
    non-publishable.  Promote any new V3 error arrays here so a model cannot
    accidentally return pass=true while also reporting a materiality/cross-
    context/value-chain error, and so the existing revision path is triggered.
    """
    guarded = (
        "evidence_grade_errors",
        "materiality_score_errors",
        "cross_context_errors",
        "causal_chain_errors",
        "value_chain_errors",
    )
    critical = list(value.get("critical_issues") or [])
    for key in guarded:
        errors = value.get(key) or []
        if errors:
            marker = f"deterministic_v3_gate:{key}"
            if marker not in critical:
                critical.append(marker)
    if critical:
        value["critical_issues"] = critical
        value["pass"] = False
    return value


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """V3 analysis with legacy production stage and Telegram guards preserved."""

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        original_stage = stage
        aliases = {
            "analyst_v3": "analyst",
            "auditor_v3": "auditor",
            "revision_v3": "revision",
        }
        value = super()._json(prompt, aliases.get(stage, stage), tools)
        if original_stage == "auditor_v3":
            value = _harden_audit_result(value)
        for key in ("telegram_draft", "corrected_telegram_draft"):
            if isinstance(value.get(key), str):
                value[key] = _normalize_report_header(value[key])
        return value
