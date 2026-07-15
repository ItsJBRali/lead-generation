from __future__ import annotations

from datetime import date
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_generator.planning.http import FetchResponse
from lead_generator.planning.adapters.achieveforms import AchieveFormsCouncilConfig, AchieveFormsPlanningScraper
from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.atrium import AtriumCouncilConfig, AtriumPlanningScraper
from lead_generator.planning.adapters.civica import (
    CivicaCouncilConfig,
    CivicaPlanningScraper,
    fetch_civica_documents_from_raw,
)
from lead_generator.planning.adapters.legacy_forms import (
    AppSearchServPlanningScraper,
    AstunPlanningScraper,
    CcedPlanningScraper,
    EnterpriseStorePlanningScraper,
    FastwebPlanningScraper,
    HtmlListPlanningScraper,
    LegacyFormsCouncilConfig,
    QueryFormPlanningScraper,
    SocrataPlanningScraper,
    StatMapPlanningScraper,
    TascomiPlanningScraper,
)
from lead_generator.planning.adapters.northgate import (
    NorthgateCouncilConfig,
    NorthgatePlanningScraper,
)


FIXTURES = Path(__file__).parent / "fixtures"


class FakeJsonHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, object]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        if "getsearchcriteria" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                {
                  "RefType": "GFPlanning",
                  "SearchItems": [
                    {"DataType": "D", "Display": {"FieldName": "SDate5From", "Label": "Date Received (From)", "Value": ""}},
                    {"DataType": "D", "Display": {"FieldName": "SDate5To", "Label": "Date Received (To)", "Value": ""}},
                    {"DataType": "D", "Display": {"FieldName": "SDate1From", "Label": "Date Valid (From)", "Value": ""}},
                    {"DataType": "D", "Display": {"FieldName": "SDate1To", "Label": "Date Valid (To)", "Value": ""}}
                  ]
                }
                """,
            )
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


class FakeCivicaKeyObjectHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, object]] = []

    def get(self, url: str, params: dict[str, str] | None = None) -> FetchResponse:
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <script>
              Civica.APIUrl="/w2webparts/Resource/Civica/Handler.ashx/";
              Civica.KeyObjectViewerUrl="/my-requests/keyobject-viewer/";
              Civica.PortalSettings={"PlanningApplicationRefType":"GFPlanning"};
            </script>
            """,
        )

    def post_json(self, url: str, data: object) -> FetchResponse:
        self.posted.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            {
              "KeyObjects": [
                {
                  "KeyNumber": "549349",
                  "KeyObjectType": "GFPlanning",
                  "KeyText": "Subject",
                  "Items": [
                    {"FieldName": "SDescription", "Label": "Case No", "Value": "WA/2026/01093"},
                    {"FieldName": "SText1", "Label": "Applicant Name", "Value": "Jane Applicant"},
                    {"FieldName": "SText2", "Label": "Agent Name", "Value": "Agent Ltd"},
                    {"FieldName": "SText9", "Label": "Application Address", "Value": "Sadlers Petworth Road GU8 4UJ"},
                    {"FieldName": "SText10", "Label": "Proposal", "Value": "Replacement entrance gates"},
                    {"FieldName": "SDate1", "Label": "Date Valid", "Value": "18/06/2026"},
                    {"FieldName": "SDate5", "Label": "Date Received", "Value": "18/05/2026"},
                    {"FieldName": "SPicklist2", "Label": "Decision", "Value": "PENDING"},
                    {"FieldName": "SPicklist3", "Label": "Case Officer", "Value": "A Planner"},
                    {"FieldName": "APicklist2", "Label": "Ward", "Value": "Chiddingfold"},
                    {"FieldName": "APicklist3", "Label": "Parish", "Value": "Chiddingfold"},
                    {"FieldName": "AText6", "Label": "Postcode", "Value": "GU8 4UJ"}
                  ]
                }
              ],
              "TotalRows": 1
            }
            """,
        )


class FakeAgileHttpClient:
    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, str] | None, dict[str, str] | None]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.requests.append((url, params, headers))
        if "identity.agileapplications.co.uk" in url:
            return FetchResponse(
                url=f"{url}?url=dudley",
                status_code=200,
                text='{"name":"Dudley Metropolitan Borough Council","code":"DUDLEY","url":"dudley","id":359}',
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            {
              "total": 1,
              "results": [
                {
                  "id": 76428,
                  "applicationType": "Full Planning Permission",
                  "reference": "P26/0082",
                  "proposal": "Conversion of existing building from vacant public house",
                  "location": "OLDE QUEENS HEAD, BIRMINGHAM STREET, HALESOWEN, B63 3HN",
                  "applicantSurname": "McCauley",
                  "agentName": "Gill",
                  "decisionText": "",
                  "registrationDate": "2026-06-10T00:00:00",
                  "validDate": null,
                  "ward": "Halesowen South",
                  "status": ""
                }
              ]
            }
            """,
        )


class FakeLegacyAgileHttpClient:
    def __init__(self) -> None:
        self.posted: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        if "WPHAPPDETAIL" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                <html><body>
                  <table>
                    <tr><td>Application No</td><td>SDC/26CM004</td></tr>
                    <tr><td>Location</td><td>Former Quarry, Edgehill, OX15 6DH</td></tr>
                    <tr><td>Proposal</td><td>Variation of conditions</td></tr>
                    <tr><td>Registration Date</td><td>21-May-2026</td></tr>
                  </table>
                </body></html>
                """,
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <html><body>
              <form method="post" action="WPHAPPCRITERIA">
                <input name="APNID.MAINBODY.WPACIS.1" value="">
                <input name="REGFROMDATE.MAINBODY.WPACIS.1" value="">
                <input name="REGTODATE.MAINBODY.WPACIS.1" value="">
                <input type="submit" name="SEARCHBUTTON.MAINBODY.WPACIS.1" value="Search!">
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
                <tr>
                  <td><a href="WPHAPPDETAIL.DisplayUrl?theApnID=SDC/26CM004">SDC/26CM004</a></td>
                  <td>Variation of conditions</td>
                  <td>Former Quarry, Edgehill, OX15 6DH</td>
                  <td>21-May-2026</td>
                </tr>
              </table>
            </body></html>
            """,
        )


class FakeAchieveFormsHttpClient:
    def __init__(self) -> None:
        self.lookups: list[tuple[str, object]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        if "authapi/isauthenticated" in url:
            return FetchResponse(url=url, status_code=200, text='{"auth-session":"session-123"}')
        if params and params.get("api") == "getDocument":
            import base64
            import json

            definition = {
                "formName": "Search Planning Applications",
                "props": {"id": "AF-Form-example"},
                "sections": [
                    {
                        "fields": [
                            {"props": {"dataName": "weeklyList", "lookup": "weekly-lookup"}},
                            {"props": {"dataName": "planningResult", "lookup": "detail-lookup"}},
                            {"props": {"dataName": "docs", "lookup": "docs-lookup"}},
                        ]
                    }
                ],
            }
            content = base64.b64encode(json.dumps(definition).encode()).decode()
            return FetchResponse(url=url, status_code=200, text=json.dumps({"content": content}))
        return FetchResponse(
            url=url,
            status_code=200,
            text='<script>FS.FormDefinition={"x":{"metadata":{"publish-uri":"sandbox-publish://definition.json"}}};</script>',
        )

    def post_json(self, url: str, data: object) -> FetchResponse:
        self.lookups.append((url, data))
        if "id=weekly-lookup" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                {"status":"done","data":"<Responses><RequestResponse><DatabaseResponse><Fields><Field Name=\\"referenceNumber\\" /></Fields><Rows><Row><result column=\\"referenceNumber\\">041761</result><result column=\\"location\\">Bermuda Workingmens Club CV10 7PW</result></Row></Rows></DatabaseResponse></RequestResponse></Responses>"}
                """,
            )
        if "id=detail-lookup" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                {"status":"done","data":"<Responses><RequestResponse><DatabaseResponse><Rows><Row><result column=\\"referenceNumber\\">041761</result><result column=\\"location\\">Bermuda Workingmens Club CV10 7PW</result><result column=\\"dateReceived\\">04 Jun 2026</result><result column=\\"description\\">Application for approval of details reserved by condition.</result><result column=\\"applicationStatus\\">Received</result><result column=\\"dateAccepted\\">04 Jun 2026</result><result column=\\"officer\\">Kelly Pearson</result><result column=\\"agent\\">Miss Chloe Heales</result></Row></Rows></DatabaseResponse></RequestResponse></Responses>"}
                """,
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            {"status":"done","data":"<Responses><RequestResponse><DatabaseResponse><Rows><Row><result column=\\"display\\">Decision notice</result><result column=\\"URL\\">/documents/041761.pdf</result></Row></Rows></DatabaseResponse></RequestResponse></Responses>"}
            """,
        )


class FakeAtriumHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[str] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.gets.append(url)
        if "ResultsPage/2" in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text="""
                <table><tr>
                  <td><a href="/Planning/Display/20260844">20260844</a></td><td>VNT9575</td><td>Site Of 18 Spencefield Lane</td>
                  <td>Approval of details reserved by condition</td><td>13/06/2026</td><td></td><td>Pending decision</td>
                </tr></table>
                """,
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <form method="post" action="/Search/Results">
              <input name="DateReceivedFrom" value="">
              <input name="DateReceivedTo" value="">
              <input name="SearchPlanning" value="True">
            </form>
            """,
        )

    def post_form(self, url: str, data: dict[str, str]) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <table><tr>
              <td><a href="/Planning/Display/20260834">20260834</a></td><td>PIM5249</td><td>15 Yorkshire Road</td>
              <td>Installation of fascia sign</td><td>13/06/2026</td><td></td><td>Pending decision - Awaiting assessment</td>
            </tr></table>
            <a href="/Search/ResultsPage/2?module=PLA&tabOrder=0">2</a>
            """,
        )


class FakeModernAtriumHttpClient:
    def __init__(self) -> None:
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[tuple[str, dict[str, str] | None]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.gets.append((url, params))
        if "/Search/Advanced" not in url:
            return FetchResponse(
                url=url,
                status_code=200,
                text='<a href="/Search/Advanced">Advanced search</a>',
            )
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <form method="post" action="/Search/Results">
              <input name="DateValidFrom"><input name="DateValidTo">
              <input type="checkbox" name="SearchPlanning" value="true" checked>
              <input type="checkbox" name="SearchBuildingControl" value="true" checked>
              <input type="checkbox" name="SearchEnforcement" value="true" checked>
              <input type="submit" name="Search" value="Search">
            </form>
            """,
        )

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(
            url=url,
            status_code=200,
            text="""
            <form method="post" action="/Search/ChangeResultsPerPage">
              <select name="resultsPerPage"><option value="10">10</option><option value="100">100</option></select>
            </form>
            <div class="row results__item">
              <a href="/Planning/Display/P/26/02488/FUL">P/26/02488/FUL</a>
              <div class="results__address">1 High Street BH1 1AA</div>
            </div>
            <div class="row searchResultsCardRow">
              <h2>CR/2026/0409/192</h2>
              <a href="/Planning/Display/CR/2026/0409/192">2 Station Road RH10 1AA</a>
            </div>
            """,
        )

class FakeLegacyFormsHttpClient:
    def __init__(self, pages: dict[str, str]) -> None:
        self.pages = pages
        self.posts: list[tuple[str, dict[str, str]]] = []
        self.gets: list[tuple[str, dict[str, str] | None]] = []

    def get(
        self,
        url: str,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.gets.append((url, params))
        key = "get:" + url
        if params:
            key = "get:results"
        return FetchResponse(url=url, status_code=200, text=self.pages.get(key, self.pages.get("get", "")))

    def post_form(
        self,
        url: str,
        data: dict[str, str],
        headers: dict[str, str] | None = None,
    ) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(url=url, status_code=200, text=self.pages.get("post:" + url, self.pages.get("post", "")))

    def post_json(self, url: str, data: object) -> FetchResponse:
        self.posts.append((url, data))
        return FetchResponse(url=url, status_code=200, text=self.pages.get("post:" + url, self.pages.get("post", "")))


class NonIdoxScraperTest(unittest.TestCase):
    def test_achieveforms_weekly_lookup_fetches_application_details(self) -> None:
        http = FakeAchieveFormsHttpClient()
        scraper = AchieveFormsPlanningScraper(
            AchieveFormsCouncilConfig("Nuneaton and Bedworth", "https://customer.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://customer.example.gov.uk/en/AchieveForms/?form_uri=sandbox-publish%3A%2F%2Fdefinition.json",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        application = discovery.applications[0]
        self.assertEqual(application.reference, "041761")
        self.assertEqual(application.date_received, "2026-06-04")
        self.assertEqual(application.description, "Application for approval of details reserved by condition.")
        self.assertEqual(application.postcode, "CV10 7PW")
        self.assertTrue(application.raw["detail_complete"])
        self.assertEqual(application.documents[0].url, "https://customer.example.gov.uk/documents/041761.pdf")
        self.assertIn("id=weekly-lookup", http.lookups[0][0])
        self.assertIn("id=detail-lookup", http.lookups[1][0])

    def test_atrium_search_uses_received_dates_and_follows_pages(self) -> None:
        http = FakeAtriumHttpClient()
        scraper = AtriumPlanningScraper(
            AtriumCouncilConfig("Leicester", "https://planning.leicester.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.leicester.gov.uk/Search/Advanced/",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        payload = http.posts[0][1]
        self.assertEqual(payload["DateReceivedFrom"], "08/06/2026")
        self.assertEqual(payload["DateReceivedTo"], "14/06/2026")
        self.assertEqual(len(discovery.applications), 2)
        self.assertEqual(discovery.applications[0].reference, "20260834")
        self.assertEqual(discovery.applications[0].date_validated, "2026-06-13")
        self.assertEqual(discovery.applications[1].reference, "20260844")
        self.assertTrue(discovery.applications[1].raw["detail_complete"])
        self.assertIn("ResultsPage/2", http.gets[-1])

    def test_atrium_expands_legacy_results_without_page_size_form(self) -> None:
        class LegacyAtriumHttp:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, str]]] = []

            def post_form(self, url, data, headers=None):
                self.posts.append((url, data))
                rows = "".join(
                    f'<tr><td><a href="/Planning/Display/26/{index:05d}">26/{index:05d}</a></td><td></td><td>Address {index}</td><td>Proposal {index}</td></tr>'
                    for index in range(23)
                )
                return FetchResponse(
                    url="https://planning.example.gov.uk/Search/Results/1/250",
                    status_code=200,
                    text=f"<table>{rows}</table>",
                )

        http = LegacyAtriumHttp()
        scraper = AtriumPlanningScraper(
            AtriumCouncilConfig("Example", "https://planning.example.gov.uk"),
            http_client=http,
        )
        first_page = FetchResponse(
            url="https://planning.example.gov.uk/Search/Results",
            status_code=200,
            text="""
            <p>Planning (23)</p>
            <table>
              <tr><td><a href="/Planning/Display/26/00001">26/00001</a></td></tr>
            </table>
            <a href="/Search/ResultsPage/2?module=PLA">Next</a>
            """,
        )

        expanded = scraper._expand_results_per_page(first_page)

        self.assertTrue(http.posts[0][0].endswith("/Search/ChangeResultsPerPage"))
        self.assertEqual(http.posts[0][1]["resultsPerPage"], "250")
        self.assertEqual(len(scraper.parse_listing(expanded.text, expanded.url)), 23)

    def test_atrium_discovers_advanced_search_and_keeps_slash_references(self) -> None:
        http = FakeModernAtriumHttpClient()
        scraper = AtriumPlanningScraper(
            AtriumCouncilConfig("BCP", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertIn("/Search/Advanced", http.gets[1][0])
        payload = http.posts[0][1]
        self.assertEqual(payload["DateValidFrom"], "06/07/2026")
        self.assertEqual(payload["SearchPlanning"], "true")
        self.assertEqual(payload["SearchBuildingControl"], "false")
        self.assertEqual(payload["SearchEnforcement"], "false")
        self.assertEqual(http.posts[1][1], {"resultsPerPage": "100"})
        self.assertEqual(
            [application.reference for application in discovery.applications],
            ["P/26/02488/FUL", "CR/2026/0409/192"],
        )
        self.assertEqual(discovery.applications[0].date_validated, "2026-07-06")
        self.assertTrue(discovery.applications[0].raw["date_inferred_from_search_window"])
        self.assertEqual(discovery.applications[1].address, "2 Station Road RH10 1AA")

    def test_tascomi_search_uses_received_dates(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form method="post" action="/planning/index.html">
                  <input name="fa" value="search"><input name="received_date_from"><input name="received_date_to">
                </form>
                """,
                "post": """
                <table>
                  <tr><th>Application Reference</th><th>Location Details</th><th>Proposal</th><th>View</th></tr>
                  <tr><td>PL/1674/26</td><td>1 High Street HA1 1AA</td><td>Install entrance gates</td><td><a href="/planning/index.html?fa=getApplication&id=233493">View</a></td></tr>
                </table>
                """,
            }
        )
        scraper = TascomiPlanningScraper(LegacyFormsCouncilConfig("Harrow", "https://planning.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/planning/index.html?fa=search",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.posts[0][1]["received_date_from"], "08-06-2026")
        self.assertEqual(discovery.applications[0].reference, "PL/1674/26")
        self.assertEqual(discovery.applications[0].description, "Install entrance gates")
        self.assertTrue(discovery.applications[0].raw["detail_complete"])

    def test_enterprisestore_search_uses_ajax_result_path(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form id="frmOnlinePlanningSearch">
                  <input name="urlOnlinePlanningSearchResult" value="/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResults">
                  <input name="FromDate"><input name="ToDate">
                </form>
                """,
                "post": """
                <a href="/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=P%2F2026%2F01498%2FDET&guid=abc">10 King Street W6 9XY</a>
                <a href="/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=P%2F2026%2F01498%2FDET&guid=abc">Details reserved</a>
                <a href="/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=P%2F2026%2F01498%2FDET&guid=abc">New automated gates</a>
                <a href="/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningOverview?applicationNumber=P%2F2026%2F01498%2FDET&guid=abc">Application No: P/2026/01498/DET | Registered : 12 June 2026</a>
                """,
            }
        )
        scraper = EnterpriseStorePlanningScraper(
            LegacyFormsCouncilConfig("Hammersmith and Fulham", "https://property.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://property.example.gov.uk/NECSWS/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearch",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        payload = http.posts[0][1]
        self.assertEqual(payload["FromDate"], "08/06/2026")
        self.assertEqual(payload["ToDate"], "14/06/2026")
        self.assertIn("OnlinePlanningSearchResults", http.posts[0][0])
        self.assertEqual(discovery.applications[0].reference, "P/2026/01498/DET")
        self.assertEqual(discovery.applications[0].date_received, "2026-06-12")

    def test_appsearchserv_uses_received_or_valid_dates(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form name="AppSearchForm" action="../servlets/ApplicationSearchServlet" method="post">
                  <input name="ReceivedDateFrom"><input name="ReceivedDateTo">
                </form>
                """,
                "post": """
                <table>
                  <tr><th>Application number</th><th>Received date</th><th>Site location</th><th>Proposal</th></tr>
                  <tr><td><a href="../servlets/ApplicationSearchServlet?PKID=169307">H/2026/0141</a></td><td>10/06/2026</td><td>12 Marina Way TS24 0AA</td><td>Replacement gates</td></tr>
                </table>
                """,
            }
        )
        scraper = AppSearchServPlanningScraper(LegacyFormsCouncilConfig("Hartlepool", "https://planning.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/portal/servlets/ApplicationSearchServlet",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.posts[0][1]["ReceivedDateFrom"], "08/06/2026")
        self.assertEqual(discovery.applications[0].reference, "H/2026/0141")
        self.assertEqual(discovery.applications[0].date_received, "2026-06-10")

    def test_fastweb_search_follows_next_page(self) -> None:
        first_page = """
        <html><body>
          <table><tr><td><a href="detail.asp?AltRef=RB2026/0808">Details</a></td></tr>
          <tr><td>App. No.: RB2026/0808 Site Address: 1 Main Road S60 1AA Description: New access gates Received Date: 10/06/2026 Decision Sent Date:</td></tr></table>
          <a href="results.asp?Scroll=2">Next</a>
        </body></html>
        """
        second_page = """
        <html><body><table><tr><td><a href="detail.asp?AltRef=RB2026/0810">Details</a></td></tr>
        <tr><td>App. No.: RB2026/0810 Site Address: 2 Main Road S60 1AB Description: Boundary wall Received Date: 11/06/2026 Decision Sent Date:</td></tr></table></body></html>
        """
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form name="SearchForm" action="results.asp" method="post">
                  <input name="DateReceivedStart"><input name="DateReceivedEnd">
                </form>
                """,
                "post": first_page,
                "get:https://planning.example.gov.uk/fastweblive/results.asp?Scroll=2": second_page,
            }
        )
        scraper = FastwebPlanningScraper(LegacyFormsCouncilConfig("Rotherham", "https://planning.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/fastweblive/search.asp",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.posts[0][1]["DateReceivedStart"], "08/06/2026")
        self.assertEqual([app.reference for app in discovery.applications], ["RB2026/0808", "RB2026/0810"])

    def test_cced_accepts_disclaimer_and_parses_result_cards(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form id="aspnetForm" action="./disclaimer.aspx" method="post">
                  <input name="__VIEWSTATE" value="x"><input type="submit" name="ctl00$ContentPlaceHolder1$btnAccept" value="Accept">
                </form>
                """,
                "post": """
                <form id="aspnetForm" action="./advsearch.aspx" method="post">
                  <input name="ctl00$ContentPlaceHolder1$txtDateReceivedFrom"><input name="ctl00$ContentPlaceHolder1$txtDateReceivedTo">
                  <input type="submit" name="ctl00$ContentPlaceHolder1$btnSearch3" value="Search">
                </form>
                """,
                "post:https://planning.example.gov.uk/advsearch.aspx": """
                P/HOU/2026/03140 Location: 22 St Leonards Avenue DT11 7NY Proposal: Erect wall and gates Decision: Decision Date: View this application
                """,
            }
        )
        scraper = CcedPlanningScraper(LegacyFormsCouncilConfig("Dorset", "https://planning.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/advsearch.aspx",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.posts[0][1]["ctl00$ContentPlaceHolder1$btnAccept"], "Accept")
        self.assertEqual(http.posts[1][1]["ctl00$ContentPlaceHolder1$txtDateReceivedFrom"], "08/06/2026")
        self.assertEqual(discovery.applications[0].reference, "P/HOU/2026/03140")

    def test_astun_submits_get_search_dates(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form id="atParentContainer" method="GET" action="https://maps.example.gov.uk/DevelopmentControl.aspx">
                  <input name="template" value="DevelopmentControlResults.tmplt"><input name="requestType" value="parseTemplate">
                  <input name="DATEAPRECV:FROM:DATE"><input name="DATEAPRECV:TO:DATE">
                </form>
                """,
                "get:results": """
                <table>
                  <tr><th>Reference</th><th>Location</th><th>Proposal</th><th>Received Date</th></tr>
                  <tr><td>26/00456/FUL</td><td>1 South Street SS4 1AA</td><td>Install access gate</td><td>09/06/2026</td></tr>
                </table>
                """,
            }
        )
        scraper = AstunPlanningScraper(LegacyFormsCouncilConfig("Rochford", "https://maps.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://maps.example.gov.uk/DevelopmentControl.aspx?RequestType=ParseTemplate&template=DevelopmentControlAdvancedSearch.tmplt",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.gets[1][1]["DATEAPRECV:FROM:DATE"], "08/06/2026")
        self.assertEqual(discovery.applications[0].reference, "26/00456/FUL")

    def test_statmap_posts_page_request(self) -> None:
        http = FakeLegacyFormsHttpClient({"post": '{"records":[{"id":"130473","name":"MO/2026/00790","address":"25 Cleardene RH4 2BY","proposal":"Rear extension","receivedDate":"2026-06-11T05:55:13","status":"Live"}]}'})
        scraper = StatMapPlanningScraper(LegacyFormsCouncilConfig("Mole Valley", "https://molevalley.example"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://molevalley.example/horizoNext/",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertIn("/api/publicportal/planningApplications/pageRequest", http.posts[0][0])
        self.assertEqual(discovery.applications[0].reference, "MO/2026/00790")
        self.assertEqual(discovery.applications[0].date_received, "2026-06-11")

    def test_socrata_uses_dataset_api(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get:results": '[{"pk":"213154","application_number":"2026/1234/P","development_address":"1 Camden Road NW1 1AA","development_description":"New gates","registered_date":"2026-06-10T00:00:00.000"}]'
            }
        )
        scraper = SocrataPlanningScraper(LegacyFormsCouncilConfig("Camden", "https://opendata.camden.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://opendata.camden.gov.uk/Environment/Planning-Applications/2eiu-s2cw/about_data",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertIn("/resource/2eiu-s2cw.json", http.gets[0][0])
        self.assertEqual(discovery.applications[0].reference, "2026/1234/P")

    def test_html_list_parses_application_links(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <article><a href="/planning-application/planning-application-p26049cou">Planning application: P/26/049/COU</a>
                <p>Received 10/06/2026. Replacement boundary gates at Hugh Town TR21 0AA</p></article>
                """
            }
        )
        scraper = HtmlListPlanningScraper(LegacyFormsCouncilConfig("Scilly Isles", "https://www.scilly.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://www.scilly.gov.uk/planning-development/planning-applications",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(discovery.applications[0].reference, "P/26/049/COU")
        self.assertEqual(discovery.applications[0].postcode, "TR21 0AA")

    def test_query_form_submits_date_fields(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form method="get" action="/results"><input name="regdate1"><input name="regdate2"><input name="proposal"></form>
                """,
                "get:results": """
                <table><tr><th>Reference</th><th>Location</th><th>Proposal</th><th>Received Date</th></tr>
                <tr><td>26/00123/FUL</td><td>1 High Street AB1 2CD</td><td>Install gates</td><td>09/06/2026</td></tr></table>
                """,
            }
        )
        scraper = QueryFormPlanningScraper(LegacyFormsCouncilConfig("Classic", "https://planning.example.gov.uk"), http_client=http)

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/search",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        self.assertEqual(http.gets[1][1]["regdate1"], "08/06/2026")
        self.assertEqual(discovery.applications[0].reference, "26/00123/FUL")

    def test_query_form_uses_registration_dates_and_parses_detail_table_results(self) -> None:
        http = FakeLegacyFormsHttpClient(
            {
                "get": """
                <form method="post" action="/results">
                  <input name="regdate1"><input name="regdate2">
                  <input name="dcndate1"><input name="dcndate2">
                  <input name="aplrecdate1"><input name="aplrecdate2">
                </form>
                """,
                "post": """
                <table>
                  <tr><td>Planning Application No:</td><td>
                    <a href="/detail/646267">CB/26/02106/LDCP (click for more details)</a>
                  </td></tr>
                  <tr><td>Registration Date:</td><td>10/06/2026</td></tr>
                  <tr><td>Parish Name:</td><td>Dunstable</td></tr>
                  <tr><td>Location:</td><td>4 Linden Close, Dunstable, LU5 4PF</td></tr>
                </table>
                """,
            }
        )
        scraper = QueryFormPlanningScraper(
            LegacyFormsCouncilConfig("Central Bedfordshire", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/search",
            start_date=date(2026, 6, 8),
            end_date=date(2026, 6, 14),
        )

        payload = http.posts[0][1]
        self.assertEqual(payload["regdate1"], "08/06/2026")
        self.assertEqual(payload["regdate2"], "14/06/2026")
        self.assertEqual(payload["dcndate1"], "")
        self.assertEqual(payload["aplrecdate1"], "")
        self.assertEqual([app.reference for app in discovery.applications], ["CB/26/02106/LDCP"])

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

    def test_civica_keyobject_json_search_uses_received_dates(self) -> None:
        http = FakeCivicaKeyObjectHttpClient()
        scraper = CivicaPlanningScraper(
            CivicaCouncilConfig("Example Civica Council", "https://planning.example.gov.uk/planning"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/planning/search-applications",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        payload = http.posted[0][1]
        self.assertEqual(http.posted[0][0], "https://planning.example.gov.uk/w2webparts/Resource/Civica/Handler.ashx/keyobject/pagedsearch")
        self.assertEqual(payload["searchFields"]["SDate5From"], "01/06/2026")
        self.assertEqual(payload["searchFields"]["SDate5To"], "30/06/2026")
        application = discovery.applications[0]
        self.assertEqual(application.uid, "549349")
        self.assertEqual(application.reference, "WA/2026/01093")
        self.assertEqual(application.date_received, "2026-05-18")
        self.assertEqual(application.description, "Replacement entrance gates")
        self.assertIn("keyobject-viewer", application.url)
        self.assertTrue(application.raw["detail_complete"])

    def test_agile_json_search_uses_registered_date_api(self) -> None:
        http = FakeAgileHttpClient()
        scraper = AgilePlanningScraper(
            AgileCouncilConfig("Dudley", "https://planning.agileapplications.co.uk/dudley/"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.agileapplications.co.uk/dudley/search-applications/",
            start_date=date(2026, 6, 1),
            end_date=date(2026, 6, 30),
        )

        _, params, headers = http.requests[1]
        self.assertEqual(params["status"], "registered")
        self.assertEqual(params["registrationDateFrom"], "2026-06-01T00:00:00+01:00")
        self.assertEqual(params["registrationDateTo"], "2026-06-30T23:59:59+01:00")
        self.assertEqual(headers["x-client"], "DUDLEY")
        application = discovery.applications[0]
        self.assertEqual(application.uid, "76428")
        self.assertEqual(application.reference, "P26/0082")
        self.assertEqual(application.date_received, "2026-06-10")
        self.assertEqual(application.postcode, "B63 3HN")
        self.assertEqual(
            application.url,
            "https://planning.agileapplications.co.uk/dudley/application-details/76428",
        )
        self.assertTrue(application.raw["detail_complete"])

    def test_legacy_agile_apas_search_uses_registered_date_form(self) -> None:
        http = FakeLegacyAgileHttpClient()
        scraper = AgilePlanningScraper(
            AgileCouncilConfig("Warwickshire", "https://planning.example.gov.uk/swiftlg/apas/run/"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/swiftlg/apas/run/wphappcriteria.display",
            start_date=date(2026, 5, 21),
            end_date=date(2026, 6, 20),
        )

        payload = http.posted[0][1]
        self.assertEqual(payload["REGFROMDATE.MAINBODY.WPACIS.1"], "21/05/2026")
        self.assertEqual(payload["REGTODATE.MAINBODY.WPACIS.1"], "20/06/2026")
        stub = discovery.applications[0]
        self.assertEqual(stub.reference, "SDC/26CM004")
        application = scraper.fetch_application(stub.uid, stub.url)
        self.assertEqual(application.reference, "SDC/26CM004")
        self.assertEqual(application.date_validated, "2026-05-21")
        self.assertEqual(application.postcode, "OX15 6DH")

    def test_civica_documents_from_raw_builds_download_links(self) -> None:
        class FakeDocumentHttpClient:
            def post_json(self, url: str, data: object) -> FetchResponse:
                self.url = url
                self.data = data
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    {
                      "CompleteDocument": [
                        {
                          "DocNo": "9510757",
                          "Title": "Existing and Proposed Gates.pdf",
                          "DocDesc": "Elevations",
                          "DocCategory": "Plans",
                          "DocDate": "2026-06-18T17:00:32.0000000"
                        }
                      ]
                    }
                    """,
                )

        fake_http = FakeDocumentHttpClient()
        with patch("lead_generator.planning.adapters.civica.CouncilHttpClient", return_value=fake_http):
            documents = fetch_civica_documents_from_raw(
                {
                    "civica_api_url": "https://planning.example.gov.uk/w2webparts/Resource/Civica/Handler.ashx/",
                    "key_number": "549349",
                    "key_text": "Subject",
                    "ref_type": "GFPlanning",
                },
                source_url="https://planning.example.gov.uk/my-requests/keyobject-viewer/?KeyNo=549349",
            )

        self.assertEqual(fake_http.data["KeyNumb"], "549349")
        self.assertEqual(documents[0].title, "Existing and Proposed Gates.pdf")
        self.assertEqual(documents[0].date_published, "2026-06-18")
        self.assertIn("Doc/pagestream", documents[0].url)

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
