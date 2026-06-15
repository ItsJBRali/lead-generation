from __future__ import annotations

from dataclasses import dataclass

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)


@dataclass(frozen=True, slots=True)
class AgileCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "agile"
    uid_query_params: tuple[str, ...] = (
        "theApnID",
        "theApnId",
        "apnID",
        "appID",
        "appId",
        "id",
        "reference",
        "appNo",
    )
    detail_markers: tuple[str, ...] = (
        "wphappdetail.displayurl",
        "wphappcriteria.display",
        "/apas/run/",
        "appdetail",
        "planning",
    )


class AgilePlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for Agile Applications / APAS planning pages."""

