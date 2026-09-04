from __future__ import annotations

from typing import Any

from .investor_analysis_v3 import InvestorFrameworkV3Client


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """V3 analysis with legacy production stage guards preserved."""

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        aliases = {
            "analyst_v3": "analyst",
            "auditor_v3": "auditor",
            "revision_v3": "revision",
        }
        return super()._json(prompt, aliases.get(stage, stage), tools)
