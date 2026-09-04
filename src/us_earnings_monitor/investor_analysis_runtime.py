from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .checkpointing import get_stage
from .evidence_architecture import extraction_groups, merge_partial_extractions, quote_validation_issues, sections_json
from .investor_analysis_v3 import InvestorFrameworkV3Client, _bounded_facts
from .report_output import chinese_language_error, clean_user_report


def _normalize_report_header(value: str) -> str:
    value = clean_user_report(value)
    value = value.replace("🏢 業務部門 / 客戶ROI:", "🏢 業務部門:\n└ 客戶/ROI:")
    value = value.replace("🏢 業務部門 / 客戶ROI：", "🏢 業務部門:\n└ 客戶/ROI:")
    return value


def _harden_audit_result(value: dict[str, Any]) -> dict[str, Any]:
    """Promote semantic/language audit failures into the production critical gate."""
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
    draft = str(value.get("corrected_telegram_draft") or "")
    if draft:
        language_error = chinese_language_error(draft)
        if language_error and language_error not in critical:
            critical.append(language_error)
    if critical:
        value["critical_issues"] = critical
        value["pass"] = False
        value["overall_score"] = min(int(value.get("overall_score", 0) or 0), 80)
    return value


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """V3 production wrapper with resumable extraction and user-output guards."""

    _STAGE_ALIASES = {
        "analyst_v3": "analyst",
        "auditor_v3": "auditor",
        "revision_v3": "revision",
    }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._checkpoint: dict | None = None
        self._persist_checkpoint_stage: Callable[[str, dict], None] | None = None

    def configure_analysis_checkpoint(self, checkpoint: dict, persist_stage: Callable[[str, dict], None]) -> None:
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
        """Resume map chunks, but merge them deterministically rather than via LLM."""
        groups = extraction_groups(evidence)
        if len(groups) <= 1:
            stage = "facts_internal_single"
            facts = self._checkpoint_payload(stage)
            if facts is None:
                facts = self._extract_group(event, sections_json(groups[0] if groups else []), partial=False)
                self._persist_internal_stage(stage, facts)
        else:
            partials: list[dict] = []
            for index, group in enumerate(groups):
                stage = f"facts_chunk_{index + 1}"
                partial = self._checkpoint_payload(stage)
                if partial is None:
                    partial = self._extract_group(event, sections_json(group), partial=True)
                    self._persist_internal_stage(stage, partial)
                partials.append(partial)
            merge_stage = "facts_deterministic_merge"
            facts = self._checkpoint_payload(merge_stage)
            if facts is None:
                facts = _bounded_facts(merge_partial_extractions(partials))
                self._persist_internal_stage(merge_stage, facts)

        facts = _bounded_facts(dict(facts))
        if not facts.get("qa"):
            qa_stage = "qa_focused"
            qa_payload = self._checkpoint_payload(qa_stage)
            if qa_payload is None:
                qa_payload = {"qa": self._extract_qa(event, evidence)}
                self._persist_internal_stage(qa_stage, qa_payload)
            if qa_payload.get("qa"):
                facts["qa"] = qa_payload["qa"]

        facts["quote_validation_issues"] = quote_validation_issues(facts, evidence)
        cluster_stage = "cross_context_internal"
        cluster_payload = self._checkpoint_payload(cluster_stage)
        if cluster_payload is None:
            cluster_payload = {"clusters": self._cross_context_clusters(facts)}
            self._persist_internal_stage(cluster_stage, cluster_payload)
        facts["cross_context_clusters"] = cluster_payload.get("clusters") or []
        facts["extraction_mode"] = "deterministic_map_merge" if len(groups) > 1 else "single_bounded_corpus"
        facts["extraction_group_count"] = len(groups)
        return facts

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        original_stage = stage
        value = super()._json(prompt, self._STAGE_ALIASES.get(stage, stage), tools)
        if original_stage == "auditor_v3":
            value = _harden_audit_result(value)
        for key in ("telegram_draft", "corrected_telegram_draft"):
            if isinstance(value.get(key), str):
                value[key] = _normalize_report_header(value[key])
        return value

    def revise(self, facts: dict, analysis: dict, audit: dict) -> dict:
        language_issue = any("predominantly English" in str(item) for item in (audit.get("critical_issues") or []))
        if language_issue:
            analysis = dict(analysis)
            analysis["_revision_language_instruction"] = (
                "Rewrite every explanatory sentence in Taiwan Traditional Chinese; keep only proper nouns, product names and metric labels in English."
            )
        return super().revise(facts, analysis, audit)
