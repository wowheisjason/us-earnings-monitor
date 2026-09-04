from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .checkpointing import get_stage
from .evidence_architecture import extraction_groups, merge_partial_extractions, quote_validation_issues, sections_json
from .investor_analysis_v3 import InvestorFrameworkV3Client, _bounded_facts
from .report_contract import harden_audit_with_report_quality, stage_contract
from .report_output import chinese_language_error, clean_user_report

_PENDING_TRANSCRIPT_STATES = {
    "UNKNOWN", "EXPECTED_NOT_YET_AVAILABLE", "NOT_FOUND_AFTER_RETRY", "USER_SUPPLIED_NOT_PRESENT",
}
_AUDIT_ERROR_KEYS = (
    "unsupported_claims", "numerical_errors", "missing_material_points", "misleading_inferences",
    "evidence_grade_errors", "materiality_score_errors", "cross_context_errors", "causal_chain_errors",
    "value_chain_errors", "critical_issues",
)


def _normalize_report_header(value: str) -> str:
    value = clean_user_report(value)
    value = value.replace("🏢 業務部門 / 客戶ROI:", "🏢 業務部門:\n└ 客戶/ROI:")
    value = value.replace("🏢 業務部門 / 客戶ROI：", "🏢 業務部門:\n└ 客戶/ROI:")
    return value


def _remove_qa_section(value: str) -> str:
    """Remove a legacy standalone Q&A section when no transcript evidence exists yet."""
    return re.sub(
        r"\n{2,}🎙️\s*法說\s*Q&A\s*[:：].*?(?=\n{2,}⚖️|\Z)",
        "",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    ).strip()


def _is_pending_qa_complaint(value: Any) -> bool:
    text = str(value).casefold()
    topic = any(token in text for token in ("q&a", "法說", "逐字稿", "transcript"))
    absence = any(token in text for token in (
        "缺少", "未提供", "未取得", "尚未提供", "沒有", "佔位", "占位", "placeholder",
        "missing", "not provided", "not available",
    ))
    return topic and absence


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


def _normalize_pending_transcript_audit(value: dict[str, Any], facts: dict) -> dict[str, Any]:
    """Apply deterministic SEC-only V1 semantics after the model audit.

    Missing Q&A is publish-blocking only when transcript/Q&A evidence is actually
    present. When the deterministic collection state says the transcript is
    still pending and facts.qa is empty, absence-related Q&A complaints are not
    valid audit errors. All unrelated evidence, numerical, inference, language,
    materiality and V4 report-quality errors remain untouched.
    """
    status = str((facts.get("collection_status") or {}).get("transcript_status") or "UNKNOWN").upper()
    if facts.get("qa") or status == "FOUND":
        return value
    if status not in _PENDING_TRANSCRIPT_STATES:
        return value

    normalized = dict(value)
    for key in ("missing_material_points", "misleading_inferences", "critical_issues"):
        normalized[key] = [item for item in (normalized.get(key) or []) if not _is_pending_qa_complaint(item)]
    draft = str(normalized.get("corrected_telegram_draft") or "")
    if draft:
        normalized["corrected_telegram_draft"] = _remove_qa_section(draft)

    blocking = []
    for key in _AUDIT_ERROR_KEYS:
        blocking.extend(normalized.get(key) or [])
    if not blocking:
        normalized["pass"] = True
        normalized["overall_score"] = max(90, int(normalized.get("overall_score", 0) or 0))
    return normalized


class ProductionInvestorV3Client(InvestorFrameworkV3Client):
    """Production US client with resumable extraction, SEC-V1 semantics and V4 report guards."""

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

    def audit(self, event, facts: dict, analysis: dict, evidence) -> dict:
        value = super().audit(event, facts, analysis, evidence)
        value = _normalize_pending_transcript_audit(value, facts)
        return harden_audit_with_report_quality(value)

    def _json(self, prompt: str, stage: str, tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        original_stage = stage
        contract = stage_contract(original_stage)
        if contract:
            prompt = prompt + "\n\n" + contract
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
