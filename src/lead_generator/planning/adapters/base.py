from __future__ import annotations

from abc import ABC, abstractmethod

from lead_generator.planning.models import DiscoveryResult, PlanningApplication


class PlanningScraper(ABC):
    def __init__(self, authority: str) -> None:
        self.authority = authority

    @abstractmethod
    def discover_ids(self, **kwargs: object) -> DiscoveryResult:
        """Return application identifiers from a council listing/search page."""

    @abstractmethod
    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        """Fetch and normalize one application detail page."""
