from __future__ import annotations

import unittest
from pathlib import Path

from lead_generator.planning.http import FetchResponse
from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper


FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str) -> FetchResponse:
        return FetchResponse(
            url=url,
            status_code=200,
            text=(FIXTURES / "idox_weekly_form.html").read_text(encoding="utf-8"),
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text=(FIXTURES / "idox_listing.html").read_text(encoding="utf-8"),
        )


class IdoxPublicAccessScraperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(
                authority="Example Council",
                base_url="https://planning.example.gov.uk",
            )
        )

    def test_parse_listing_deduplicates_detail_tabs_and_normalizes_urls(self) -> None:
        listing_html = (FIXTURES / "idox_listing.html").read_text(encoding="utf-8")

        result = self.scraper.parse_listing(
            listing_html,
            "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList",
        )

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].uid, "ABC123XYZ")
        self.assertEqual(result[0].reference, "24/01234/FUL")
        self.assertEqual(
            result[0].url,
            "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ",
        )
        self.assertIn("12 High Street", result[0].address or "")
        self.assertEqual(result[1].uid, "DEF456XYZ")

    def test_parse_detail_maps_fields_dates_and_postcode(self) -> None:
        summary_html = (FIXTURES / "idox_summary.html").read_text(encoding="utf-8")

        application = self.scraper.parse_detail(
            summary_html,
            "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ",
        )

        self.assertEqual(application.uid, "ABC123XYZ")
        self.assertEqual(application.reference, "24/01234/FUL")
        self.assertEqual(application.address, "12 High Street, Bristol, BS1 4ST")
        self.assertEqual(application.description, "Single storey rear extension and associated works")
        self.assertEqual(application.status, "Awaiting decision")
        self.assertEqual(application.decision, "Not yet decided")
        self.assertEqual(application.date_received, "2026-06-03")
        self.assertEqual(application.date_validated, "2026-06-05")
        self.assertEqual(application.applicant_name, "Jane Builder")
        self.assertEqual(application.agent_name, "Acme Planning Ltd")
        self.assertEqual(application.case_officer, "A. Planner")
        self.assertEqual(application.ward, "Central")
        self.assertEqual(application.parish, "Unparished")
        self.assertEqual(application.postcode, "BS1 4ST")

    def test_builds_default_idox_urls(self) -> None:
        weekly_url = self.scraper.build_weekly_list_url()
        detail_url = self.scraper.build_detail_url("ABC123XYZ")

        self.assertEqual(
            weekly_url,
            "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList",
        )
        self.assertEqual(
            detail_url,
            "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ",
        )

    def test_discover_ids_submits_weekly_list_form(self) -> None:
        http = FakeHttpClient()
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(
                authority="Example Council",
                base_url="https://planning.example.gov.uk",
            ),
            http_client=http,
        )

        discovery = scraper.discover_ids(limit=1)

        self.assertEqual(len(discovery.applications), 1)
        self.assertEqual(discovery.applications[0].uid, "ABC123XYZ")
        self.assertEqual(
            http.posts[0][0],
            "https://planning.example.gov.uk/online-applications/weeklyListResults.do?action=firstPage",
        )
        self.assertEqual(http.posts[0][1]["_csrf"], "fixture-token")
        self.assertEqual(http.posts[0][1]["searchCriteria.ward"], "")
        self.assertEqual(http.posts[0][1]["week"], "0")
        self.assertEqual(http.posts[0][1]["dateType"], "DC_Validated")

    def test_parse_weekly_detail_response_as_single_discovery(self) -> None:
        page_html = (FIXTURES / "idox_weekly_detail.html").read_text(encoding="utf-8")

        result = self.scraper.parse_listing(
            page_html,
            "https://planning.example.gov.uk/online-applications/weeklyListResults.do?action=firstPage",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].uid, "TF8BYXDNG8500")
        self.assertEqual(result[0].reference, "26/12108/VC")
        self.assertEqual(result[0].address, "52A Arley Hill Bristol BS6 5PP")
        self.assertEqual(result[0].description, "Sycamore - Reduce by 2.5m.")
        self.assertEqual(result[0].status, "Pending decision")
        self.assertEqual(result[0].date_validated, "2026-06-15")
        self.assertNotIn("_csrf", result[0].raw)
        self.assertNotIn("org.apache.struts.taglib.html.TOKEN", result[0].raw)
        self.assertEqual(
            result[0].url,
            "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=TF8BYXDNG8500",
        )


if __name__ == "__main__":
    unittest.main()
