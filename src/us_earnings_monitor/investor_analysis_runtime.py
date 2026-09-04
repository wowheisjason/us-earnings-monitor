from __future__ import annotations

import os
import re
from typing import Any

from .investor_analysis_v3 import InvestorFrameworkV3Client
from .report_output import chinese_language_error, clean_user_report


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """V3 production wrapper preserving legacy stage/model guard behavior.

    The underlying V3 class uses descriptive stage names such as analyst_v3.
    Existing provider safeguards (notably the USD unit-conversion rule) are
    attached to legacy analyst/auditor/revision stages, so this wrapper maps
    them before the provider call and normalizes user-facing output afterward.
    """

    _STAGE_ALIASES = {
        "analyst_v3": "analyst",
        "auditor_v3": "auditor",
        "revision_v3": "revision",
    }

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        original_stage = stage
        value = super()._json(prompt, self._STAGE_ALIASES.get(stage, stage), tools)
        if original_stage == "analyst_v3":
            self._normalize_analysis(value)
        elif original_stage == "auditor_v3":
            self._normalize_audit(value)
        elif original_stage == "revision_v3":
            self._normalize_analysis(value)
        return value

    @staticmethod
    def _normalize_report_header(text: str) -> str:
        text = clean_user_report(text)
        # Preserve the existing downstream formatter's expected header phrase.
        text = re.sub(r"(?m)^\s*核心投資結論\s*[:：]", "💡 核心投資結論:", text)
        if "💡 核心投資結論:" not in text and text:
            text = "💡 核心投資結論:\n" + text
        return text

    def _normalize_analysis(self, value: dict[str, Any]) -> None:
        draft = str(value.get("telegram_draft") or "")
        if draft:
            value["telegram_draft"] = self._normalize_report_header(draft)

    def _normalize_audit(self, value: dict[str, Any]) -> None:
        draft = str(value.get("corrected_telegram_draft") or "")
        if draft:
            draft = self._normalize_report_header(draft)
            value["corrected_telegram_draft"] = draft
            language_error = chinese_language_error(draft)
            if language_error:
                critical = list(value.get("critical_issues") or [])
                if language_error not in critical:
                    critical.append(language_error)
                value["critical_issues"] = critical
                value["pass"] = False
                value["overall_score"] = min(int(value.get("overall_score", 0) or 0), 80)

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        # The base revision prompt is strict on evidence but not strong enough on
        # output language after an English-heavy analyst response. Prefix a hard
        # presentation constraint while preserving the evidence-only semantics.
        language_issue = any("predominantly English" in str(item) for item in (audit.get("critical_issues") or []))
        if language_issue:
            analysis = dict(analysis)
            analysis["_revision_language_instruction"] = (
                "Rewrite every explanatory sentence in Taiwan Traditional Chinese. "
                "Keep only proper nouns, product names and metric labels in English."
            )
        return super().revise(facts, analysis, audit)
