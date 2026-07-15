from __future__ import annotations

from datetime import date
import json
import unittest
from urllib.parse import parse_qs, quote, urlsplit

from lead_generator.planning.adapters.wiltshire import (
    WiltshireCouncilConfig,
    WiltshirePlanningScraper,
)
from lead_generator.planning.http import CouncilFetchError, FetchResponse
from lead_generator.planning.leads import CouncilTarget, planning_scraper_for_target


class FakeWiltshireHttpClient:
    def __init__(self, response_text: str | None = None) -> None:
        self.response_text = response_text
        self.posted: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        boot = {
            "mode": "PROD",
            "fwuid": "fw-test",
            "app": "siteforce:napiliApp",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "app-test"},
            "pathPrefix": "/pr",
        }
        encoded_boot = quote(json.dumps(boot, separators=(",", ":")), safe="")
        return FetchResponse(
            url=url,
            status_code=200,
            text=f'<script src="/pr/s/sfsites/l/{encoded_boot}/bootstrap.js"></script>',
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posted.append((url, data))
        if self.response_text is not None:
            response_text = self.response_text
        else:
            records = {
                "records": [
                    {
                        "Name": "PL/2026/04348",
                        "Id": "a0iQ300000MuM2nIAF",
                        "arcusbuiltenv__Site_Address__c": "1 High Street, Salisbury, SP1 2AA",
                        "Hidden_Proposal__c": "Installation of entrance gates",
                        "arcusbuiltenv__Valid_Date__c": "2026-07-10",
                        "arcusbuiltenv__Status__c": "Under Consultation",
                    }
                ]
            }
            response_text = json.dumps(
                {
                    "actions": [
                        {
                            "state": "SUCCESS",
                            "returnValue": json.dumps(records, separators=(",", ":")),
                        }
                    ]
                }
            )
        return FetchResponse(url=url, status_code=200, text=response_text)


class WiltshirePlanningScraperTest(unittest.TestCase):
    def test_discover_ids_uses_wiltshire_search_and_maps_records(self) -> None:
        http = FakeWiltshireHttpClient()
        scraper = WiltshirePlanningScraper(
            WiltshireCouncilConfig("Wiltshire", "https://development.wiltshire.gov.uk/pr"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://development.wiltshire.gov.uk/pr/s/?tabset-167f1=3",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        endpoint, form = http.posted[0]
        self.assertEqual(parse_qs(urlsplit(endpoint).query)["other.PR_SearchCont.query"], ["1"])
        action = json.loads(form["message"])["actions"][0]
        self.assertEqual(action["descriptor"], "apex://PR_SearchCont/ACTION$query")
        self.assertEqual(action["params"]["category_name"], "PApplication")
        self.assertEqual(
            action["params"]["search_criteria"],
            {
                "arcusbuiltenv__Valid_Date__c:from": "2026-07-06",
                "arcusbuiltenv__Valid_Date__c:to": "2026-07-12",
            },
        )
        application = discovery.applications[0]
        self.assertEqual(application.reference, "PL/2026/04348")
        self.assertEqual(application.description, "Installation of entrance gates")
        self.assertEqual(application.date_received, None)
        self.assertEqual(application.date_validated, "2026-07-10")
        self.assertEqual(application.postcode, "SP1 2AA")
        self.assertEqual(
            application.url,
            "https://development.wiltshire.gov.uk/pr/s/planning-application/"
            "a0iQ300000MuM2nIAF/pl202604348",
        )
        self.assertTrue(application.raw["detail_complete"])

    def test_error_action_is_not_treated_as_an_empty_result(self) -> None:
        response = json.dumps(
            {
                "actions": [
                    {
                        "state": "ERROR",
                        "error": [{"message": "Search unavailable"}],
                    }
                ]
            }
        )
        scraper = WiltshirePlanningScraper(
            WiltshireCouncilConfig("Wiltshire", "https://development.wiltshire.gov.uk/pr"),
            http_client=FakeWiltshireHttpClient(response),
        )

        with self.assertRaisesRegex(CouncilFetchError, "Search unavailable"):
            scraper.discover_ids(
                listing_url="https://development.wiltshire.gov.uk/pr/s/",
                start_date=date(2026, 7, 6),
                end_date=date(2026, 7, 12),
            )

    def test_wiltshire_target_uses_dedicated_adapter(self) -> None:
        scraper = planning_scraper_for_target(
            CouncilTarget(
                authority="Wiltshire",
                portal_family="unknown",
                scraper_type="Custom",
                base_url="https://development.wiltshire.gov.uk/pr/s/",
                listing_url="https://development.wiltshire.gov.uk/pr/s/?tabset-167f1=3",
                geometry={},
            )
        )

        self.assertIsInstance(scraper, WiltshirePlanningScraper)


if __name__ == "__main__":
    unittest.main()
