from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass(slots=True)
class PlanningDocument:
    title: str
    url: str
    document_type: str | None = None
    date_published: str | None = None
    file_size: str | None = None
    description: str | None = None
    source_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value not in (None, "", {})}


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
    documents: list[PlanningDocument] = field(default_factory=list)
    date_scraped: str = field(default_factory=utc_now_iso)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        data = {
            "authority": self.authority,
            "uid": self.uid,
            "url": self.url,
            "reference": self.reference,
            "address": self.address,
            "description": self.description,
            "status": self.status,
            "decision": self.decision,
            "date_received": self.date_received,
            "date_validated": self.date_validated,
            "applicant_name": self.applicant_name,
            "agent_name": self.agent_name,
            "case_officer": self.case_officer,
            "ward": self.ward,
            "parish": self.parish,
            "postcode": self.postcode,
            "source_url": self.source_url,
            "documents": [document.to_dict() for document in self.documents],
            "date_scraped": self.date_scraped,
            "raw": self.raw,
        }
        return {key: value for key, value in data.items() if value not in (None, "", {}, [])}


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
