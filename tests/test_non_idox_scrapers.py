from __future__ import annotations

import unittest
from pathlib import Path

from lead_generator.planning.http import FetchResponse
from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.civica import CivicaCouncilConfig, CivicaPlanningScraper
from lead_generator.planning.adapters.northgate import (
    NorthgateCouncilConfig,
    NorthgatePlanningScraper,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FakeJsonHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, object]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        return FetchResponse(
            url=url,
            status_code=200,
            text='var APIroot = "https://api.example.gov.uk/webapi/api/";'
            'var PlanningAPI = "PlanningAPI/v2/";'
            'var PlanningData = "planningdata/";',
        )

    def post_json(self, url: str, data: object) -> FetchResponse:
        self.posted.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            {
              "planningData": {
                "refval": "26/01878/FUL",
                "addressline": "Woodbarn Farm, Denny Lane, BS40 8SZ",
                "proposal": "Conversion and extension of barn",
                "dcstat_text": "Pending Consideration",
                "dateaprecv_text": "15/05/2026",
                "dateapval_text": "21/05/2026",
                "appname": "Boyce Bros Ltd",
                "agtname": "Arena Global Management Ltd",
                "officer_name": "Danielle Milsom",
                "ward_text": "Chew Valley",
                "parish_text": "Chew Magna"
              }
            }
            """,
        )


class NonIdoxScraperTest(unittest.TestCase):
    def test_civica_listing_and_detail(self) -> None:
        scraper = CivicaPlanningScraper(
            CivicaCouncilConfig("Example Civica Council", "https://planning.example.gov.uk")
        )

        applications = scraper.parse_listing(
            (FIXTURES / "civica_listing.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/planningexplorer/search.aspx",
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].uid, "24/01234/FUL")

        application = scraper.parse_detail(
            (FIXTURES / "civica_detail.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/planningexplorer/applicationdetails.aspx?REFVAL=24/01234/FUL",
        )
        self.assertEqual(application.reference, "24/01234/FUL")
        self.assertEqual(application.postcode, "YO1 8AA")
        self.assertEqual(application.description, "Replacement shopfront")
        self.assertEqual(application.date_received, "2026-06-01")
        self.assertEqual(application.date_validated, "2026-06-03")
        self.assertEqual(len(application.documents), 1)

    def test_civica_webforms_detail_uses_json_api(self) -> None:
        http = FakeJsonHttpClient()
        scraper = CivicaPlanningScraper(
            CivicaCouncilConfig(
                "Example Civica Council",
                "https://app.example.gov.uk/webforms/planning/",
            ),
            http_client=http,
        )

        application = scraper.fetch_application(
            "26/01878/FUL",
            "https://app.example.gov.uk/webforms/planning/details.html?refval=26%2F01878%2FFUL",
        )

        self.assertEqual(http.posted[0][0], "https://api.example.gov.uk/webapi/api/PlanningAPI/v2/planningdata/")
        self.assertEqual(http.posted[0][1], "26/01878/FUL")
        self.assertEqual(application.reference, "26/01878/FUL")
        self.assertEqual(application.postcode, "BS40 8SZ")
        self.assertEqual(application.description, "Conversion and extension of barn")
        self.assertEqual(application.status, "Pending Consideration")
        self.assertEqual(application.date_received, "2026-05-15")
        self.assertEqual(application.date_validated, "2026-05-21")

    def test_agile_listing_and_detail(self) -> None:
        scraper = AgilePlanningScraper(
            AgileCouncilConfig("Example Agile Council", "https://planning.example.gov.uk")
        )

        applications = scraper.parse_listing(
            (FIXTURES / "agile_listing.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/apas/run/WPHAPPCRITERIA.Display",
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].uid, "25/00001/APAS")

        application = scraper.parse_detail(
            (FIXTURES / "agile_detail.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/apas/run/WPHAPPDETAIL.DisplayUrl?theApnID=25/00001/APAS",
        )
        self.assertEqual(application.reference, "25/00001/APAS")
        self.assertEqual(application.postcode, "NP20 1AA")
        self.assertEqual(application.description, "Two storey rear extension")
        self.assertEqual(application.status, "Registered")
        self.assertEqual(application.date_validated, "2026-06-05")

    def test_northgate_listing_and_detail(self) -> None:
        scraper = NorthgatePlanningScraper(
            NorthgateCouncilConfig("Example Northgate Council", "https://planning.example.gov.uk")
        )

        applications = scraper.parse_listing(
            (FIXTURES / "northgate_listing.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/PlanningExplorer/GeneralSearch.aspx",
        )
        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].uid, "26/00456/HSE")

        application = scraper.parse_detail(
            (FIXTURES / "northgate_detail.html").read_text(encoding="utf-8"),
            "https://planning.example.gov.uk/PlanningExplorer/Generic/StdDetails.aspx?PARAM0=26/00456/HSE",
        )
        self.assertEqual(application.reference, "26/00456/HSE")
        self.assertEqual(application.postcode, "DH1 1AA")
        self.assertEqual(application.description, "Garden studio")
        self.assertEqual(application.decision, "Approved")
        self.assertEqual(application.date_validated, "2026-06-08")


if __name__ == "__main__":
    unittest.main()
