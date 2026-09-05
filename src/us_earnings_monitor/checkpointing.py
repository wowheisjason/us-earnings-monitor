from __future__ import annotations

import hashlib
import json
from typing import Any

from .models import Evidence

# V5 replaces lossy evidence sampling with full-coverage unit extraction and a
# compact research packet. Pre-V5 checkpoints must never cross this boundary.
CHECKPOINT_PIPELINE_VERSION = 5


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def evidence_fingerprint(evidence: list[Evidence]) -> str:
    """Hash source text, structured facts and extraction metadata."""
    items = []
    for item in evidence:
        items.append({
            "document_key": item.document_key,
            "title": item.title,
            "url": item.url,
            "text_sha256": hashlib.sha256(item.text.encode("utf-8")).hexdigest(),
            "structured_facts_sha256": hashlib.sha256(
                _stable_json(item.structured_facts).encode("utf-8")
            ).hexdigest(),
            "metadata_sha256": hashlib.sha256(_stable_json(item.metadata).encode("utf-8")).hexdigest(),
        })
    payload = _stable_json(sorted(items, key=lambda value: value["document_key"]))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prepare_checkpoint(existing: dict, fingerprint: str) -> tuple[dict, bool]:
    compatible = bool(
        existing
        and existing.get("pipeline_version") == CHECKPOINT_PIPELINE_VERSION
        and existing.get("evidence_fingerprint") == fingerprint
    )
    if compatible:
        checkpoint = dict(existing)
        checkpoint.setdefault("stages", {})
        return checkpoint, False
    return {
        "pipeline_version": CHECKPOINT_PIPELINE_VERSION,
        "evidence_fingerprint": fingerprint,
        "stages": {},
    }, bool(existing)


def get_stage(checkpoint: dict, stage: str) -> Any | None:
    value = (checkpoint.get("stages") or {}).get(stage)
    if not isinstance(value, dict) or "payload" not in value:
        return None
    return value["payload"]


def put_stage(checkpoint: dict, stage: str, payload: Any) -> None:
    checkpoint.setdefault("stages", {})[stage] = {"payload": payload}


def completed_stages(checkpoint: dict) -> list[str]:
    return list((checkpoint.get("stages") or {}).keys())
