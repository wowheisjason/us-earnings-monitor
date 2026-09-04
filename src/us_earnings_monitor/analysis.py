from __future__ import annotations

import os
import time
from typing import Protocol

from .investor_analysis_v3 import InvestorFrameworkV3Client
from .openai_ir import OpenAIWebIrClient


class AnalysisClient(Protocol):
    def extract_facts(self, event, evidence) -> dict: ...
    def analyze(self, event, facts: dict, evidence) -> dict: ...
    def audit(self, event, facts: dict, analysis: dict, evidence) -> dict: ...
    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict: ...
    def material_update(self, facts: dict, previous_count: int, current_count: int) -> bool: ...


class IrResearchClient(Protocol):
    def research_official_ir(self, company, event, now): ...


class FallbackIrResearchClient:
    """Try independent web-retrieval providers without coupling them to analysis."""

    def __init__(self, providers: list[tuple[str, IrResearchClient]]):
        self.providers = providers

    def research_official_ir(self, company, event, now):
        attempts = []
        last_status = {"research_complete": False, "provider": "none"}
        for name, client in self.providers:
            started = time.monotonic()
            try:
                documents, status = client.research_official_ir(company, event, now)
                attempts.append({
                    "provider": name,
                    "ok": bool(documents and status.get("research_complete")),
                    "seconds": round(time.monotonic() - started, 3),
                    "documents": len(documents),
                    "model": status.get("model"),
                })
                last_status = status
                if documents and status.get("research_complete"):
                    return documents, {**status, "provider": name, "provider_attempts": attempts}
            except Exception as exc:  # noqa: BLE001
                attempts.append({
                    "provider": name,
                    "ok": False,
                    "seconds": round(time.monotonic() - started, 3),
                    "error": f"{type(exc).__name__}: {exc}"[:500],
                    "category": getattr(exc, "category", None),
                })
        return [], {**last_status, "research_complete": False, "provider_attempts": attempts}


def _provider(value: str | None, env_name: str) -> str:
    return (value or os.getenv(env_name, "gemini")).strip().casefold()


def build_analysis_client(provider: str | None = None) -> AnalysisClient:
    """Build the LLM analysis adapter independently from retrieval."""
    selected = _provider(provider, "ANALYSIS_PROVIDER")
    if selected == "gemini":
        return InvestorFrameworkV3Client()
    raise RuntimeError(f"Unsupported ANALYSIS_PROVIDER={selected!r}. Currently supported: 'gemini'.")


def build_ir_research_client(provider: str | None = None, *, disabled_providers: set[str] | None = None) -> IrResearchClient:
    """Build a resilient IR discovery chain.

    Retrieval remains independent from the V3 analysis layer. Gemini Search is
    primary when healthy; OpenAI web search stays an optional no-hidden-spend fallback.
    """
    selected = _provider(provider, "IR_RESEARCH_PROVIDER")
    if selected not in {"gemini", "auto"}:
        raise RuntimeError(f"Unsupported IR_RESEARCH_PROVIDER={selected!r}. Currently supported: 'gemini' or 'auto'.")
    disabled = disabled_providers or set()
    providers: list[tuple[str, IrResearchClient]] = []
    if "gemini_search" not in disabled:
        providers.append(("gemini_search", InvestorFrameworkV3Client()))
    if "openai_web_search" not in disabled and os.getenv("OPENAI_API_KEY") and os.getenv("OPENAI_IR_ENABLED", "1") != "0":
        providers.append(("openai_web_search", OpenAIWebIrClient()))
    return FallbackIrResearchClient(providers)
