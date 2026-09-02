from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from ..models import Company, Disclosure


class SourceAdapter(ABC):
    @abstractmethod
    def discover(self, companies: list[Company], day: date) -> list[Disclosure]:
        """Return only metadata. Content is fetched later only for an eligible event."""


