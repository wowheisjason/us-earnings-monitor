from __future__ import annotations

import os
from typing import Protocol

from .gemini import GeminiClient


class AnalysisClient(Protocol):
    def extract_facts(self, event, evidence) -> dict: ...
    def analyze(self, event, facts: dict, evidence) -> dict: ...
    def audit(self, event, facts: dict, analysis: dict, evidence) -> dict: ...
    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict: ...
    def material_update(self, facts: dict, previous_count: int, current_count: int) -> bool: ...
    def repair_cash_flow_reconciliation(self, event, facts: dict, evidence, issues: list[str]) -> dict: ...


def build_analysis_client(provider: str | None = None) -> AnalysisClient:
    """Return the configured analysis adapter.

    Automated production defaults to Gemini because the project already has a
    Gemini API key and can stay within its free tier. The rest of the pipeline
    depends only on this protocol, so another free provider can be added without
    changing discovery, extraction, validation, state, or Telegram delivery.

    ChatGPT in an interactive conversation is intentionally not treated as an
    unattended API provider: using it here requires no OpenAI API spend, but a
    GitHub Actions job cannot invoke the conversation itself.
    """
    selected = (provider or os.getenv("ANALYSIS_PROVIDER", "gemini")).strip().casefold()
    if selected == "gemini":
        return GeminiClient()
    raise RuntimeError(
        f"Unsupported ANALYSIS_PROVIDER={selected!r}. Automated free-tier production currently supports 'gemini'."
    )
