from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    cik: str
    ir_index_url: str | None = None
    ir_additional_urls: list[str] = field(default_factory=list)


@dataclass
class Disclosure:
    source: str
    source_id: str
    ticker: str | None
    title: str
    published_at: str
    url: str
    document_url: str | None = None
    fiscal_year: int | None = None
    quarter: str | None = None
    period_end: str | None = None
    document_kind: str = "other"
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> str:
        return f"{self.source}:{self.source_id}"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "Disclosure":
        return cls(**value)


@dataclass
class Evidence:
    document_key: str
    title: str
    url: str
    text: str
    structured_facts: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class EarningsEvent:
    event_id: str
    ticker: str
    fiscal_year: int | None
    quarter: str | None
    first_seen_at: str
    period_end: str | None = None
    documents: list[str] = field(default_factory=list)
    status: str = "collecting"  # collecting | published | needs_human_review
    report_version: int = 0
    last_analyzed_document_count: int = 0
    updated_at: str | None = None
    collection_status: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EarningsEvent":
        return cls(**value)


def now_iso(now: datetime) -> str:
    return now.isoformat(timespec="seconds")
