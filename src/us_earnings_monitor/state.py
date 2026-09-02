from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

from .models import Disclosure, EarningsEvent

_IR_SOURCES = {"official_ir", "gemini_grounded_ir"}


class StateStore:
    """Small, reviewable state store. GitHub Actions commits this file after a run."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._read()

    def _read(self) -> dict:
        if not self.path.exists():
            return {"schema_version": 1, "documents": {}, "events": {}, "source_checks": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("source_checks", {})
        return data

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def seen_document(self, disclosure: Disclosure) -> bool:
        return disclosure.key in self.data["documents"]

    @staticmethod
    def _normalized_title(title: str) -> str:
        value = unicodedata.normalize("NFKC", title).casefold().replace("[xbrl]", "")
        return re.sub(r"[^0-9a-z一-龥ぁ-んァ-ヶ]+", "", value)

    def equivalent_primary_document(self, disclosure: Disclosure, event: EarningsEvent | None) -> bool:
        """Suppress an IR mirror of a document already collected from a primary source."""
        if disclosure.source not in _IR_SOURCES or event is None:
            return False
        candidate = self._normalized_title(disclosure.title)
        for key in event.documents:
            existing = self.get_document(key)
            if existing.source in _IR_SOURCES or existing.metadata.get("format") == "ixbrl":
                continue
            if self._normalized_title(existing.title) == candidate:
                return True
        return False

    def add_document(self, disclosure: Disclosure) -> None:
        self.data["documents"][disclosure.key] = disclosure.as_dict()

    def get_document(self, key: str) -> Disclosure:
        return Disclosure.from_dict(self.data["documents"][key])

    def get_event(self, event_id: str) -> EarningsEvent | None:
        raw = self.data["events"].get(event_id)
        return EarningsEvent.from_dict(raw) if raw else None

    def put_event(self, event: EarningsEvent) -> None:
        self.data["events"][event.event_id] = event.as_dict()

    def all_events(self) -> list[EarningsEvent]:
        return [EarningsEvent.from_dict(v) for v in self.data["events"].values()]

    def source_checked_on(self, source: str, day: str) -> bool:
        return self.data["source_checks"].get(source) == day

    def mark_source_checked(self, source: str, day: str) -> None:
        self.data["source_checks"][source] = day
