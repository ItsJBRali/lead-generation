from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class PlanningApplication:
    authority: str
    uid: str
    url: str
    reference: str | None = None
    address: str | None = None
    description: str | None = None
    status: str | None = None
    decision: str | None = None
    date_received: str | None = None
    date_validated: str | None = None
    applicant_name: str | None = None
    agent_name: str | None = None
    case_officer: str | None = None
    ward: str | None = None
    parish: str | None = None
    postcode: str | None = None
    source_url: str | None = None
    date_scraped: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "", {})}


@dataclass(slots=True)
class DiscoveryResult:
    authority: str
    source_url: str
    applications: list[PlanningApplication]
    date_scraped: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return {
            "authority": self.authority,
            "source_url": self.source_url,
            "date_scraped": self.date_scraped,
            "result": [application.to_dict() for application in self.applications],
        }
