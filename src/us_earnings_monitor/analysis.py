from __future__ import annotations

import os
from typing import Protocol

from .gemini_v2 import GeminiV2Client


class AnalysisClient(Protocol):
    def extract_facts(self, event, evidence) -> dict: ...
    def analyze(self, event, facts: dict, evidence) -> dict: ...
    def audit(self, event, facts: dict, analysis: dict, evidence) -> dict: ...
    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict: ...
    def material_update(self, facts: dict, previous_count: int, current_count: int) -> bool: ...


class IrResearchClient(Protocol):
    def research_official_ir(self, company, event, now): ...


def _provider(value: str | None, env_name: str) -> str:
    return (value or os.getenv(env_name, "gemini")).strip().casefold()


def build_analysis_client(provider: str | None = None) -> AnalysisClient:
    """Build the LLM analysis adapter independently from retrieval."""
    selected = _provider(provider, "ANALYSIS_PROVIDER")
    if selected == "gemini":
        return GeminiV2Client()
    raise RuntimeError(f"Unsupported ANALYSIS_PROVIDER={selected!r}. Currently supported: 'gemini'.")


def build_ir_research_client(provider: str | None = None) -> IrResearchClient:
    """Build the official-IR discovery provider independently from analysis."""
    selected = _provider(provider, "IR_RESEARCH_PROVIDER")
    if selected == "gemini":
        return GeminiV2Client()
    raise RuntimeError(f"Unsupported IR_RESEARCH_PROVIDER={selected!r}. Currently supported: 'gemini'.")
