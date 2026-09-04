from __future__ import annotations

from typing import Any

from .investor_analysis_v3 import InvestorFrameworkV3Client


def _normalize_report_header(value: str) -> str:
    return value.replace("🏢 業務部門 / 客戶ROI:", "🏢 業務部門:\n└ 客戶/ROI:")


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """V3 analysis with legacy production stage and Telegram guards preserved."""

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        aliases = {
            "analyst_v3": "analyst",
            "auditor_v3": "auditor",
            "revision_v3": "revision",
        }
        value = super()._json(prompt, aliases.get(stage, stage), tools)
        for key in ("telegram_draft", "corrected_telegram_draft"):
            if isinstance(value.get(key), str):
                value[key] = _normalize_report_header(value[key])
        return value
