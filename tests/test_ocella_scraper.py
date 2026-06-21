from __future__ import annotations

from datetime import date
import unittest
from pathlib import Path

from lead_generator.planning.http import FetchResponse
from lead_generator.planning.adapters.ocella import OcellaCouncilConfig, OcellaPlanningScraper


FIXTURES = Path(__file__).parent / "fixtures"


class FakeOcellaHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <form method="post" action="planningSearch">
                <input name="reference" value="">
                <input name="receivedFrom" value="">
                <input name="receivedTo" value="">
                <input type="submit" name="action" value="Search">
              </form>
            </body></html>
            """,
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posted.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <table>
                <tr><th>Reference</th><th>Location</th><th>Proposal</th><th>Received</th><th>Status</th></tr>
                <tr>
                  <td><a href="planningDetails?reference=77047/APP/2026/1428&amp;from=planningSearch">77047/APP/2026/1428</a></td>
                  <td>67 RAEBURN ROAD HAYES UB4 8PN</td>
                  <td>Erection of a single storey rear extension</td>
                  <td>15-06-26</td>
                  <td>Undecided</td>
                </tr>
              </table>
            </body></html>
            """,
        )


class FakeCappedOcellaHttpClient(FakeOcellaHttpClient):
    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posted.append((url, data))
        received_from = data["receivedFrom"]
        received_to = data["receivedTo"]
        if received_from == "01-06-26" and received_to == "30-06-26":
            body = """
            <tr>
              <td><a href="planningDetails?reference=77047/APP/2026/0000&amp;from=planningSearch">77047/APP/2026/0000</a></td>
              <td>1 Parent Road UB1 1AA</td>
              <td>Parent capped row</td>
              <td>30-06-26</td>
              <td>Undecided</td>
            </tr>
            <p>First 1 results shown, there are 2 in total</p>
            """
        elif received_to == "15-06-26":
            body = """
            <tr>
              <td><a href="planningDetails?reference=77047/APP/2026/0001&amp;from=planningSearch">77047/APP/2026/0001</a></td>
              <td>1 Left Road UB1 1AA</td>
              <td>Left split row</td>
              <td>12-06-26</td>
              <td>Undecided</td>
            </tr>
            """
        else:
            body = """
            <tr>
              <td><a href="planningDetails?reference=77047/APP/2026/0002&amp;from=planningSearch">77047/APP/2026/0002</a></td>
              <td>2 Right Road UB2 2BB</td>
              <td>Right split row</td>
              <td>18-06-26</td>
              <td>Undecided</td>
            </tr>
            """
        return FetchResponse(
            url=url,
            status_code=200,
            text=f"""
            <html><body>
              <table>
                <tr><th>Reference</th><th>Location</th><th>Proposal</th><th>Received</th><th>Status</th></tr>
                {body}
              </table>
            </body></html>
            """,
        )


class FakeFarehamHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        if "ApplicationDetails.aspx" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                <html><body>
                  <div class="docGridRow">
                    <div class="detailsCells detailsFieldNames">Reference</div>
                    <div class="detailsCells detailsValues">P/26/0689/FP</div>
                  </div>
                  <div class="docGridRow">
                    <div class="detailsCells detailsFieldNames">Proposal</div>
                    <div class="detailsCells detailsValues">Two storey extension</div>
                  </div>
                  <div class="docGridRow">
                    <div class="detailsCells detailsFieldNames">Location</div>
                    <div class="detailsCells detailsValues">18 Gifford Close Fareham PO15 6PJ</div>
                  </div>
                  <div class="docGridRow">
                    <div class="detailsCells detailsFieldNames">Received</div>
                    <div class="detailsCells detailsValues">19/06/2026</div>
                  </div>
                  <div class="docGridRow">
                    <div class="detailsCells detailsFieldNames">Accepted</div>
                    <div class="detailsCells detailsValues">19/06/2026</div>
                  </div>
                </body></html>
                """,
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <form method="post" action="./ApplicationSearch.aspx?search=true">
                <input name="__VIEWSTATE" type="hidden" value="state">
                <input name="ctl00$BodyPlaceHolder$uxTextSearchKeywords" type="text">
                <input name="ctl00$BodyPlaceHolder$uprnFromSearchKeywords" type="hidden">
                <input name="ctl00$BodyPlaceHolder$appRefFromSearchKeywords" type="hidden">
                <input name="ctl00$BodyPlaceHolder$uxButtonSearch" type="submit" value="Search">
              </form>
            </body></html>
            """,
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posted.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <a href="ApplicationDetails.aspx?reference=P/26/0689/FP&amp;uprn=100060341761">P/26/0689/FP</a>
            </body></html>
            """,
        )


class OcellaPlanningScraperTest(unittest.TestCase):
    def setUp(self) -> None:
        self.scraper = OcellaPlanningScraper(
            OcellaCouncilConfig(
                authority="Example Ocella Council",
                base_url="https://planning.example.gov.uk",
            )
        )

    def test_parse_listing_extracts_application_ids(self) -> None:
        listing_html = (FIXTURES / "ocella_listing.html").read_text(encoding="utf-8")

        applications = self.scraper.parse_listing(
            listing_html,
            "https://planning.example.gov.uk/planning/search-results",
        )

        self.assertEqual(len(applications), 2)
        self.assertEqual(applications[0].uid, "OCELLA001")
        self.assertEqual(applications[0].reference, "24/09999/FUL")
        self.assertEqual(
            applications[0].url,
            "https://planning.example.gov.uk/planning/application-details?id=OCELLA001",
        )

    def test_parse_detail_extracts_fields_and_documents(self) -> None:
        detail_html = (FIXTURES / "ocella_detail.html").read_text(encoding="utf-8")

        application = self.scraper.parse_detail(
            detail_html,
            "https://planning.example.gov.uk/planning/application-details?id=OCELLA001",
        )

        self.assertEqual(application.uid, "OCELLA001")
        self.assertEqual(application.reference, "24/09999/FUL")
        self.assertEqual(application.address, "14 Station Road, Cardiff, CF10 1AA")
        self.assertEqual(application.postcode, "CF10 1AA")
        self.assertEqual(application.description, "Change of use to office")
        self.assertEqual(application.status, "Pending consideration")
        self.assertEqual(application.date_received, "2026-06-01")
        self.assertEqual(application.date_validated, "2026-06-03")
        self.assertEqual(application.applicant_name, "Example Developments Ltd")
        self.assertEqual(application.agent_name, "Planning Agent LLP")
        self.assertEqual(len(application.documents), 1)
        self.assertEqual(application.documents[0].title, "Planning statement.pdf")
        self.assertEqual(application.documents[0].date_published, "2026-06-15")

    def test_parse_detail_accepts_short_received_and_validated_labels(self) -> None:
        detail_html = """
        <html><body>
          <table>
            <tr><td>Reference</td><td>BR/111/24/PL</td></tr>
            <tr><td>Location</td><td>8 Argyle Road, Bognor Regis, PO21 1DY</td></tr>
            <tr><td>Received</td><td>21-06-24</td></tr>
            <tr><td>Validated</td><td>10-07-24</td></tr>
          </table>
        </body></html>
        """

        application = self.scraper.parse_detail(
            detail_html,
            "https://planning.example.gov.uk/planningDetails?reference=BR/111/24/PL",
        )

        self.assertEqual(application.date_received, "2024-06-21")
        self.assertEqual(application.date_validated, "2024-07-10")

    def test_discover_ids_submits_received_date_search(self) -> None:
        http = FakeOcellaHttpClient()
        scraper = OcellaPlanningScraper(
            OcellaCouncilConfig("Example Ocella Council", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/OcellaWeb/planningSearch",
            start_date=date(2026, 5, 21),
            end_date=date(2026, 6, 20),
        )

        self.assertEqual(http.posted[0][1]["receivedFrom"], "21-05-26")
        self.assertEqual(http.posted[0][1]["receivedTo"], "20-06-26")
        application = discovery.applications[0]
        self.assertEqual(application.reference, "77047/APP/2026/1428")
        self.assertEqual(application.date_received, "2026-06-15")
        self.assertEqual(application.status, "Undecided")
        self.assertTrue(application.raw["detail_complete"])

    def test_capped_result_search_is_split_by_date_range(self) -> None:
        http = FakeCappedOcellaHttpClient()
        scraper = OcellaPlanningScraper(
            OcellaCouncilConfig("Example Ocella Council", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/OcellaWeb/planningSearch",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        self.assertEqual(len(http.posted), 3)
        self.assertEqual(
            [application.reference for application in discovery.applications],
            ["77047/APP/2026/0001", "77047/APP/2026/0002"],
        )

    def test_fareham_style_search_uses_application_year_and_detail_grid(self) -> None:
        http = FakeFarehamHttpClient()
        scraper = OcellaPlanningScraper(
            OcellaCouncilConfig("Fareham", "https://www.fareham.gov.uk/casetrackerplanning/"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://www.fareham.gov.uk/casetrackerplanning/ApplicationSearch.aspx?search=true",
            start_date=date(2026, 5, 21),
            end_date=date(2026, 6, 20),
        )

        self.assertEqual(http.posted[0][1]["ctl00$BodyPlaceHolder$uxTextSearchKeywords"], "P/26")
        stub = discovery.applications[0]
        self.assertEqual(stub.reference, "P/26/0689/FP")
        application = scraper.fetch_application(stub.uid, stub.url)
        self.assertEqual(application.date_received, "2026-06-19")
        self.assertEqual(application.date_validated, "2026-06-19")
        self.assertEqual(application.postcode, "PO15 6PJ")


if __name__ == "__main__":
    unittest.main()
