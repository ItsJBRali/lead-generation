from __future__ import annotations

from dataclasses import dataclass

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)


@dataclass(frozen=True, slots=True)
class NorthgateCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "northgate"
    uid_query_params: tuple[str, ...] = (
        "PARAM0",
        "param0",
        "KEYVAL",
        "keyVal",
        "id",
        "AppNo",
        "appNo",
        "reference",
    )
    detail_markers: tuple[str, ...] = (
        "stddetails.aspx",
        "planningpk.xml",
        "planningexplorer",
        "applicationdetails",
        "detail",
    )


class NorthgatePlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for Northgate Planning Explorer pages."""

