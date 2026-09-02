from __future__ import annotations

from pathlib import Path

import yaml

from .models import Company


def load_watchlist(path: str | Path) -> tuple[list[Company], list[str]]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    companies = [Company(**item) for item in raw["companies"]]
    patterns = [str(p).casefold() for p in raw.get("earnings_title_patterns", [])]
    return companies, patterns


