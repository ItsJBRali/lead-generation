from __future__ import annotations

from datetime import date
import unittest
from pathlib import Path

from lead_generator.planning.http import CouncilFetchError, FetchResponse
from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper


FIXTURES = Path(__file__).parent / "fixtures"


class FakeHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[str] = []

    def get(self, url: str) -> FetchResponse:
        self.gets.append(url)
        if "activeTab=documents" in url:
            return FetchResponse(url=url, status_code=200, text=(FIXTURES / "idox_documents.html").read_text(encoding="utf-8"))
        return FetchResponse(url=url, status_code=200, text=(FIXTURES / "idox_weekly_form.html").read_text(encoding="utf-8"))

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(url=url, status_code=200, text=(FIXTURES / "idox_listing.html").read_text(encoding="utf-8"))


class FakeAdvancedSearchHttpClient(FakeHttpClient):
    def get(self, url: str) -> FetchResponse:
        self.gets.append(url)
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <form action="/online-applications/advancedSearchResults.do?action=firstPage" method="post">
                <input type="hidden" name="_csrf" value="advanced-token">
                <input name="date(applicationReceivedStart)" value="">
                <input name="date(applicationReceivedEnd)" value="">
                <input name="date(applicationValidatedStart)" value="">
                <input name="date(applicationValidatedEnd)" value="">
              </form>
            </body></html>
            """,
        )


class FakePagedAdvancedSearchHttpClient(FakeAdvancedSearchHttpClient):
    def get(self, url: str) -> FetchResponse:
        self.gets.append(url)
        if "pagedSearchResults.do" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                <html><body>
                  <ul>
                    <li>
                      <a href="applicationDetails.do?activeTab=summary&amp;keyVal=PAGE2A">26/00003/FUL</a>
                      3 Third Street
                    </li>
                  </ul>
                </body></html>
                """,
            )
        return super().get(url)

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <ul>
                <li>
                  <a href="applicationDetails.do?activeTab=summary&amp;keyVal=PAGE1A">26/00001/FUL</a>
                  1 First Street
                </li>
                <li>
                  <a href="applicationDetails.do?activeTab=summary&amp;keyVal=PAGE1B">26/00002/FUL</a>
                  2 Second Street
                </li>
              </ul>
              <a href="pagedSearchResults.do?action=page&amp;searchCriteria.page=2">2</a>
            </body></html>
            """,
        )


class FakeAdvancedSearchFailureHttpClient(FakeHttpClient):
    def get(self, url: str) -> FetchResponse:
        if "action=advanced" in url:
            self.gets.append(url)
            raise CouncilFetchError("HTTP 500 while fetching advanced search")
        return super().get(url)


class IdoxPublicAccessScraperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = IdoxPublicAccessScraper(IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"))

    def test_parse_listing_deduplicates_detail_tabs_and_normalizes_urls(self) -> None:
        listing_html = (FIXTURES / "idox_listing.html").read_text(encoding="utf-8")
        result = self.scraper.parse_listing(listing_html, "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList")
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0].uid, "ABC123XYZ")
        self.assertEqual(result[0].reference, "24/01234/FUL")
        self.assertEqual(result[0].url, "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ")
        self.assertIn("12 High Street", result[0].address or "")
        self.assertEqual(result[1].uid, "DEF456XYZ")

    def test_parse_detail_maps_fields_dates_and_postcode(self) -> None:
        summary_html = (FIXTURES / "idox_summary.html").read_text(encoding="utf-8")
        application = self.scraper.parse_detail(summary_html, "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ")
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
        self.assertEqual(self.scraper.build_weekly_list_url(), "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList")
        self.assertEqual(self.scraper.build_detail_url("ABC123XYZ"), "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ")

    def test_discover_ids_submits_weekly_list_form(self) -> None:
        http = FakeHttpClient()
        scraper = IdoxPublicAccessScraper(IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"), http_client=http)
        discovery = scraper.discover_ids(limit=1)
        self.assertEqual(len(discovery.applications), 1)
        self.assertEqual(discovery.applications[0].uid, "ABC123XYZ")
        self.assertEqual(http.posts[0][0], "https://planning.example.gov.uk/online-applications/weeklyListResults.do?action=firstPage")
        self.assertEqual(http.posts[0][1]["_csrf"], "fixture-token")
        self.assertEqual(http.posts[0][1]["searchCriteria.ward"], "")
        self.assertEqual(http.posts[0][1]["week"], "0")
        self.assertEqual(http.posts[0][1]["dateType"], "DC_Validated")

    def test_discover_ids_submits_real_idox_advanced_received_date_fields(self) -> None:
        http = FakeAdvancedSearchHttpClient()
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"),
            http_client=http,
        )

        scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            start_date=date(2026, 5, 21),
            end_date=date(2026, 6, 20),
        )

        self.assertEqual(http.posts[0][0], "https://planning.example.gov.uk/online-applications/advancedSearchResults.do?action=firstPage")
        self.assertEqual(http.posts[0][1]["date(applicationReceivedStart)"], "21/05/2026")
        self.assertEqual(http.posts[0][1]["date(applicationReceivedEnd)"], "20/06/2026")
        self.assertNotIn("searchCriteria.dateReceivedStart", http.posts[0][1])

    def test_advanced_search_failure_falls_back_to_weekly_list(self) -> None:
        http = FakeAdvancedSearchFailureHttpClient()
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
            limit=1,
        )

        self.assertEqual(len(discovery.applications), 1)
        self.assertIn("search.do?action=weeklyList", http.gets[1])
        self.assertEqual(http.posts[0][0], "https://planning.example.gov.uk/online-applications/weeklyListResults.do?action=firstPage")

    def test_discover_ids_follows_idox_result_pagination(self) -> None:
        http = FakePagedAdvancedSearchHttpClient()
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual([application.uid for application in discovery.applications], ["PAGE1A", "PAGE1B", "PAGE2A"])
        self.assertTrue(any("pagedSearchResults.do?action=page" in url for url in http.gets))

    def test_discover_ids_respects_limit_before_fetching_idox_pages(self) -> None:
        http = FakePagedAdvancedSearchHttpClient()
        scraper = IdoxPublicAccessScraper(
            IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
            limit=2,
        )

        self.assertEqual([application.uid for application in discovery.applications], ["PAGE1A", "PAGE1B"])
        self.assertFalse(any("pagedSearchResults.do?action=page" in url for url in http.gets))

    def test_parse_weekly_detail_response_as_single_discovery(self) -> None:
        page_html = (FIXTURES / "idox_weekly_detail.html").read_text(encoding="utf-8")
        result = self.scraper.parse_listing(page_html, "https://planning.example.gov.uk/online-applications/weeklyListResults.do?action=firstPage")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].uid, "TF8BYXDNG8500")
        self.assertEqual(result[0].reference, "26/12108/VC")
        self.assertEqual(result[0].address, "52A Arley Hill Bristol BS6 5PP")
        self.assertEqual(result[0].description, "Sycamore - Reduce by 2.5m.")
        self.assertEqual(result[0].status, "Pending decision")
        self.assertEqual(result[0].date_validated, "2026-06-15")
        self.assertNotIn("_csrf", result[0].raw)
        self.assertNotIn("org.apache.struts.taglib.html.TOKEN", result[0].raw)
        self.assertEqual(result[0].url, "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=TF8BYXDNG8500")

    def test_parse_documents_extracts_attachment_metadata(self) -> None:
        documents_html = (FIXTURES / "idox_documents.html").read_text(encoding="utf-8")
        documents = self.scraper.parse_documents(documents_html, "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=documents&keyVal=ABC123XYZ")
        self.assertEqual(len(documents), 2)
        self.assertEqual(documents[0].title, "Proposed floor plan.pdf")
        self.assertEqual(documents[0].url, "https://planning.example.gov.uk/online-applications/documentdownload.do?module=planning&keyVal=DOC001")
        self.assertEqual(documents[0].document_type, "Drawing")
        self.assertEqual(documents[0].date_published, "2026-06-15")
        self.assertEqual(documents[0].description, "Proposed floor plan")
        self.assertEqual(documents[0].file_size, "512 KB")
        self.assertEqual(documents[1].date_published, "2026-06-16")
        self.assertNotIn("description", documents[1].to_dict())

    def test_fetch_application_can_include_documents(self) -> None:
        http = FakeHttpClient()
        scraper = IdoxPublicAccessScraper(IdoxCouncilConfig(authority="Example Council", base_url="https://planning.example.gov.uk"), http_client=http)
        application = scraper.fetch_application("ABC123XYZ", "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123XYZ", include_documents=True)
        self.assertEqual(len(application.documents), 2)
        self.assertIn("activeTab=documents", http.gets[-1])


if __name__ == "__main__":
    unittest.main()
