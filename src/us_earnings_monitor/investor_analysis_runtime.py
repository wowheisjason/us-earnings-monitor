from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .checkpointing import get_stage
from .evidence_architecture import (
    extraction_groups,
    merge_partial_extractions,
    quote_validation_issues,
    sections_json,
)
from .investor_analysis_v3 import InvestorFrameworkV3Client


def _normalize_report_header(value: str) -> str:
    return value.replace("🏢 業務部門 / 客戶ROI:", "🏢 業務部門:\n└ 客戶/ROI:")


def _harden_audit_result(value: dict[str, Any]) -> dict[str, Any]:
    """Normalize V3 semantic audit errors into the legacy critical gate.

    The main production loop already treats critical_issues and pass=false as
    non-publishable. Promote any new V3 error arrays here so a model cannot
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
    """V3 analysis with production guards and resumable map/reduce extraction."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._checkpoint: dict | None = None
        self._persist_checkpoint_stage: Callable[[str, dict], None] | None = None

    def configure_analysis_checkpoint(
        self,
        checkpoint: dict,
        persist_stage: Callable[[str, dict], None],
    ) -> None:
        """Attach the current event checkpoint for resumable internal V3 stages.

        The event-level runner owns checkpoint validity/fingerprinting. This
        runtime only reads/writes bounded sub-stages such as individual facts
        chunks, consolidation, and cross-context clustering.
        """
        self._checkpoint = checkpoint
        self._persist_checkpoint_stage = persist_stage

    def _checkpoint_payload(self, stage: str) -> dict | None:
        if self._checkpoint is None:
            return None
        value = get_stage(self._checkpoint, stage)
        return value if isinstance(value, dict) else None

    def _persist_internal_stage(self, stage: str, payload: dict) -> None:
        if self._persist_checkpoint_stage is not None:
            self._persist_checkpoint_stage(stage, payload)

    def extract_facts(self, event, evidence) -> dict:
        """Resume long-corpus extraction at chunk granularity.

        Without this override, an outage on chunk N forces chunks 1..N-1 to be
        regenerated because the base V3 `extract_facts` is one Python call.
        Persist every successful map output plus consolidation/cross-context so
        retries spend tokens only on the missing sub-stage.
        """
        groups = extraction_groups(evidence)
        partials: list[dict] = []

        if len(groups) <= 1:
            stage = "facts_internal_single"
            facts = self._checkpoint_payload(stage)
            if facts is None:
                facts = self._extract_group(event, sections_json(groups[0] if groups else []), partial=False)
                self._persist_internal_stage(stage, facts)
        else:
            for index, group in enumerate(groups):
                stage = f"facts_chunk_{index + 1}"
                partial = self._checkpoint_payload(stage)
                if partial is None:
                    partial = self._extract_group(event, sections_json(group), partial=True)
                    self._persist_internal_stage(stage, partial)
                partials.append(partial)

            facts = self._checkpoint_payload("facts_consolidation")
            if facts is None:
                facts = self._consolidate_extractions(event, merge_partial_extractions(partials))
                self._persist_internal_stage("facts_consolidation", facts)

        facts = dict(facts)
        facts["quote_validation_issues"] = quote_validation_issues(facts, evidence)

        cluster_payload = self._checkpoint_payload("cross_context_internal")
        if cluster_payload is None:
            clusters = self._cross_context_clusters(facts)
            cluster_payload = {"clusters": clusters}
            self._persist_internal_stage("cross_context_internal", cluster_payload)
        facts["cross_context_clusters"] = cluster_payload.get("clusters") or []
        facts["extraction_mode"] = "section_map_reduce" if len(groups) > 1 else "single_bounded_corpus"
        facts["extraction_group_count"] = len(groups)
        return facts

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