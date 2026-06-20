from __future__ import annotations

from datetime import date
import json
import unittest
from urllib.parse import quote

from lead_generator.planning.adapters.arcus import ArcusCouncilConfig, ArcusPlanningScraper
from lead_generator.planning.http import FetchResponse


class FakeArcusHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        boot = {
            "mode": "PROD",
            "fwuid": "fw-test",
            "app": "siteforce:communityApp",
            "loaded": {"APPLICATION@markup://siteforce:communityApp": "app-test"},
            "pathPrefix": "/pr",
        }
        return FetchResponse(
            url=url,
            status_code=200,
            text=f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>',
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posted.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            {
              "actions": [
                {
                  "state": "SUCCESS",
                  "returnValue": {
                    "returnValue": {
                      "records": [
                        {
                          "arcusbuiltenv__Status__c": "Valid",
                          "arcusbuiltenv__Received_Date__c": "2026-06-18",
                          "arcusbuiltenv__Type__c": "Householder planning permission",
                          "arcusbuiltenv__Site_Address__c": "7 ORLESTONE GARDENS, ORPINGTON, BR6 6HB",
                          "Id": "a0lTv000003UuB7IAK",
                          "arcusbuiltenv__Proposal__c": "Part garage conversion",
                          "Name": "26/02469/HPA"
                        }
                      ]
                    }
                  }
                }
              ]
            }
            """,
        )


class ArcusPlanningScraperTest(unittest.TestCase):
    def test_discover_ids_uses_arcus_salesforce_search(self) -> None:
        http = FakeArcusHttpClient()
        scraper = ArcusPlanningScraper(
            ArcusCouncilConfig("Bromley", "https://planningaccess.bromley.gov.uk/pr"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planningaccess.bromley.gov.uk/pr/s/register-view?c__r=Arcus_BE_Public_Register",
            start_date=date(2026, 5, 21),
            end_date=date(2026, 6, 20),
        )

        payload = json.loads(http.posted[0][1]["message"])
        request = payload["actions"][0]["params"]["params"]["request"]
        filters = {item["fieldDeveloperName"]: item["fieldValue"] for item in request["searchFilters"]}
        self.assertEqual(request["searchName"], "Planning_Applications")
        self.assertEqual(filters["PA_ADV_DateValidFrom"], "2026-05-21")
        self.assertEqual(filters["PA_ADV_DateValidTo"], "2026-06-20")
        application = discovery.applications[0]
        self.assertEqual(application.reference, "26/02469/HPA")
        self.assertEqual(application.date_received, "2026-06-18")
        self.assertEqual(application.postcode, "BR6 6HB")
        self.assertEqual(
            application.url,
            "https://planningaccess.bromley.gov.uk/pr/s/detail/a0lTv000003UuB7IAK",
        )
        self.assertTrue(application.raw["detail_complete"])


if __name__ == "__main__":
    unittest.main()
