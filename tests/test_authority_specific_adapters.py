from __future__ import annotations

import base64
import json
import unittest
from datetime import date

from lead_generator.planning.adapters.authority_specific import (
    AshfordPlanningScraper,
    CentralBedfordshirePlanningScraper,
    EastSussexPlanningScraper,
    EastleighPlanningScraper,
    ElmbridgePlanningScraper,
    StratfordOnAvonPlanningScraper,
    SouthOxfordshirePlanningScraper,
    TandridgePlanningScraper,
    TauntonDeanePlanningScraper,
)
from lead_generator.planning.adapters.arcus import ArcusCouncilConfig
from lead_generator.planning.adapters.atrium import AtriumCouncilConfig
from lead_generator.planning.adapters.legacy_forms import LegacyFormsCouncilConfig
from lead_generator.planning.http import FetchResponse
from lead_generator.planning.leads import CouncilTarget, planning_scraper_for_target


EXPECTED_ADAPTERS = {
    "Ashford": "AshfordPlanningScraper",
    "BCP": "BcpPlanningScraper",
    "Wychavon": "WychavonPlanningScraper",
    "Barking and Dagenham": "BarkingAndDagenhamPlanningScraper",
    "Worcestershire": "WorcestershirePlanningScraper",
    "Worcester": "WorcesterPlanningScraper",
    "Wokingham": "WokinghamPlanningScraper",
    "Wiltshire": "WiltshirePlanningScraper",
    "West Sussex": "WestSussexPlanningScraper",
    "West Northamptonshire": "WestNorthamptonshirePlanningScraper",
    "Welwyn Hatfield": "WelwynHatfieldPlanningScraper",
    "Waltham Forest": "WalthamForestPlanningScraper",
    "Vale of White Horse": "ValeOfWhiteHorsePlanningScraper",
    "Bromley": "BromleyPlanningScraper",
    "Broxbourne": "BroxbournePlanningScraper",
    "Taunton Deane": "TauntonDeanePlanningScraper",
    "Camden": "CamdenPlanningScraper",
    "Central Bedfordshire": "CentralBedfordshirePlanningScraper",
    "Tandridge": "TandridgePlanningScraper",
    "Surrey": "SurreyPlanningScraper",
    "Stratford on Avon": "StratfordOnAvonPlanningScraper",
    "Coventry": "CoventryPlanningScraper",
    "South Oxfordshire": "SouthOxfordshirePlanningScraper",
    "Crawley": "CrawleyPlanningScraper",
    "Devon": "DevonPlanningScraper",
    "Dorset": "DorsetPlanningScraper",
    "East Sussex": "EastSussexPlanningScraper",
    "Eastleigh": "EastleighPlanningScraper",
    "Elmbridge": "ElmbridgePlanningScraper",
    "Somerset": "SomersetPlanningScraper",
    "Essex": "EssexPlanningScraper",
    "Exmoor": "ExmoorPlanningScraper",
    "Gloucestershire": "GloucestershirePlanningScraper",
    "Shepway": "ShepwayPlanningScraper",
}


class AuthoritySpecificAdapterTest(unittest.TestCase):
    def test_every_requested_authority_has_an_explicit_adapter(self) -> None:
        for authority, expected_class_name in EXPECTED_ADAPTERS.items():
            with self.subTest(authority=authority):
                scraper = planning_scraper_for_target(
                    CouncilTarget(
                        authority=authority,
                        portal_family="unknown",
                        scraper_type="Custom",
                        base_url="https://planning.example.gov.uk",
                        listing_url="https://planning.example.gov.uk/search",
                        geometry={},
                    )
                )
                self.assertEqual(scraper.__class__.__name__, expected_class_name)

    def test_ashford_uses_its_received_date_arcus_fields(self) -> None:
        scraper = AshfordPlanningScraper(
            ArcusCouncilConfig("Ashford", "https://planning.example.gov.uk")
        )

        filters = scraper._search_filters(date(2026, 7, 6), date(2026, 7, 12))

        self.assertEqual(filters[0]["fieldName"], "arcusbuiltenv__Received_Date__c")
        self.assertEqual(filters[0]["fieldDeveloperName"], "PA_ADV_DateReceivedFrom")
        self.assertEqual(filters[1]["fieldDeveloperName"], "PA_ADV_DateReceivedTo")

    def test_requested_authorities_do_not_share_adapter_classes(self) -> None:
        class_names = list(EXPECTED_ADAPTERS.values())
        self.assertEqual(len(class_names), len(set(class_names)))

    def test_central_bedfordshire_forces_get_and_follows_next_page(self) -> None:
        class FakeCentralBedsHttp:
            def __init__(self) -> None:
                self.gets: list[tuple[str, dict[str, str] | None]] = []

            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.gets.append((url, params))
                if params is not None:
                    text = """
                    <a href="/PgeResultDetail/1">CB/26/02106/LDCP (click for more details)</a>
                    <a href="/results/next">Next</a>
                    """
                elif url.endswith("/results/next"):
                    text = '<a href="/PgeResultDetail/2">CB/26/02178/GPDE (click for more details)</a>'
                else:
                    text = """
                    <form method="post" action="/results">
                      <input name="regdate1"><input name="regdate2">
                    </form>
                    """
                return FetchResponse(url=url, status_code=200, text=text)

        http = FakeCentralBedsHttp()
        scraper = CentralBedfordshirePlanningScraper(
            LegacyFormsCouncilConfig("Central Bedfordshire", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/search",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertEqual(http.gets[1][1]["regdate1"], "06/07/2026")
        self.assertEqual(
            [application.reference for application in discovery.applications],
            ["CB/26/02106/LDCP", "CB/26/02178/GPDE"],
        )

    def test_taunton_deane_requests_full_list_and_parses_result_tables(self) -> None:
        class FakeTauntonHttp:
            def __init__(self) -> None:
                self.posts: list[tuple[str, dict[str, str]]] = []

            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    <form method="post" action="PLAppList.asp">
                      <input name="regdate1"><input name="regdate2">
                      <input type="submit" name="submit" value="Search">
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
                if len(self.posts) == 1:
                    text = """
                    <form method="post" action="PLAppList.asp">
                      <input type="hidden" name="ViewAll" value="All">
                      <input type="hidden" name="regdate1" value="06/07/2026">
                    </form>
                    """
                else:
                    text = """
                    <table>
                      <tr><td><a href="PlAppDets.asp?casefullref=26/26/0010">Application number : 26/26/0010</a></td><td>Registered : 10/07/2026</td></tr>
                      <tr><td colspan="2">Erection of entrance gates at 1 High Street, Taunton, TA1 1AA</td></tr>
                    </table>
                    """
                return FetchResponse(url=url, status_code=200, text=text)

        http = FakeTauntonHttp()
        scraper = TauntonDeanePlanningScraper(
            LegacyFormsCouncilConfig("Taunton Deane", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/search",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertEqual(http.posts[0][1]["regdate1"], "06/07/2026")
        self.assertEqual(http.posts[1][1]["ViewAll"], "All")
        self.assertEqual(discovery.applications[0].reference, "26/26/0010")
        self.assertEqual(discovery.applications[0].postcode, "TA1 1AA")

    def test_tandridge_uses_two_stage_acknowledged_date_search(self) -> None:
        class FakeTandridgeHttp:
            def __init__(self) -> None:
                self.posts: list[dict[str, str]] = []

            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    <form method="post">
                      <input type="hidden" name="__VIEWSTATE" value="one">
                      <select name="ctl00$MainContent$ddlSearchCriteria"><option value="">Please select</option></select>
                    </form>
                    """,
                )

            def post_form(
                self,
                url: str,
                data: dict[str, str],
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.posts.append(data)
                if len(self.posts) == 1:
                    text = """
                    <form method="post">
                      <input type="hidden" name="__VIEWSTATE" value="two">
                      <select name="ctl00$MainContent$ddlSearchCriteria"><option selected>Acknowledged date</option></select>
                      <input name="ctl00$MainContent$txtStartDate">
                      <input name="ctl00$MainContent$txtEndDate">
                    </form>
                    """
                else:
                    text = """
                    <table>
                      <tr><th>Application number</th><th>Address</th><th>Description</th></tr>
                      <tr><td>2026/835</td><td>16 Woodland Way, CR3 6ER</td><td>Erection of rear gates</td></tr>
                    </table>
                    """
                return FetchResponse(url=url, status_code=200, text=text)

        http = FakeTandridgeHttp()
        scraper = TandridgePlanningScraper(
            LegacyFormsCouncilConfig("Tandridge", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertEqual(http.posts[0][scraper.SEARCH_CRITERIA_FIELD], "Acknowledged date")
        self.assertEqual(http.posts[1][scraper.START_DATE_FIELD], "2026-07-06")
        self.assertEqual(http.posts[1]["__VIEWSTATE"], "two")
        self.assertEqual(discovery.applications[0].reference, "2026/835")

    def test_stratford_uses_received_date_json_api(self) -> None:
        class FakeStratfordHttp:
            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.url = url
                self.params = params
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    [{
                      "id":"app-1", "reference":"26/01738/FUL",
                      "validDate":"11/07/2026", "proposal":"Entrance gates",
                      "address":"1 High Street CV37 9AA", "status":"Pending",
                      "link":"https://apps.example.gov.uk/EplanningV2/AppDetail/Index/app-1"
                    }]
                    """,
                )

        http = FakeStratfordHttp()
        scraper = StratfordOnAvonPlanningScraper(
            LegacyFormsCouncilConfig("Stratford on Avon", "https://apps.example.gov.uk/EplanningV2"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://apps.example.gov.uk/EplanningV2/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertTrue(http.url.endswith("/EplanningV2/API/v1/Search"))
        self.assertEqual(http.params["dateAppReceivedFrom"], "2026-07-06")
        self.assertEqual(discovery.applications[0].reference, "26/01738/FUL")
        self.assertEqual(discovery.applications[0].postcode, "CV37 9AA")

    def test_south_oxfordshire_uses_official_weekly_csv(self) -> None:
        class FakeWeeklyCsvHttp:
            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                return FetchResponse(url=url, status_code=200, text="accepted")

            def post_form(
                self,
                url: str,
                data: dict[str, str],
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.url = url
                self.data = data
                csv_text = (
                    "ReportTitle\r\nWeekly applications\r\n\r\n"
                    "application_number,received_complete_date,parish_description,ps2_category,location1,proposal1\r\n"
                    '"Application No.\r\nP26/S2249/LB","Valid\r\n11/07/2026",Beckley,Other,'
                    '"1 High Street\r\nOX3 9UT","Entrance gates"\r\n'
                )
                encoded = base64.b64encode(csv_text.encode("utf-8-sig")).decode("ascii")
                return FetchResponse(url=url, status_code=200, text=f'"{encoded}"')

        http = FakeWeeklyCsvHttp()
        scraper = SouthOxfordshirePlanningScraper(
            AtriumCouncilConfig("South Oxfordshire", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/Search/Results",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertTrue(http.url.endswith("/Planning/GetWeeklyListCSV"))
        self.assertEqual(http.data["fromDate"], "06/07/2026")
        application = discovery.applications[0]
        self.assertEqual(application.reference, "P26/S2249/LB")
        self.assertEqual(application.date_received, "2026-07-06")
        self.assertEqual(application.date_validated, "2026-07-11")
        self.assertIn("applicationNumber=P26%2FS2249%2FLB", application.url)

    def test_east_sussex_uses_official_received_date_results_url(self) -> None:
        class FakeEastSussexHttp:
            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.url = url
                self.params = params
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    <table>
                      <tr><th>Application number</th><th>Site address</th><th>Proposal</th><th>Date received</th></tr>
                      <tr><td>RR/9000/CM</td><td>1 High Street, BN7 1AA</td><td>Entrance gates</td><td>09/07/2026</td></tr>
                    </table>
                    """,
                )

        http = FakeEastSussexHttp()
        scraper = EastSussexPlanningScraper(
            LegacyFormsCouncilConfig("East Sussex", "https://apps.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://apps.example.gov.uk/environment/planning/applications/register/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertTrue(http.url.endswith("/register/results"))
        self.assertEqual(http.params["sd"], "06/07/2026")
        self.assertEqual(http.params["typ"], "dmw_planning")
        self.assertEqual(discovery.applications[0].reference, "RR/9000/CM")

    def test_eastleigh_uses_received_date_salesforce_action(self) -> None:
        class FakeEastleighHttp:
            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                return FetchResponse(url=url, status_code=200, text="bootstrap")

            def post_form(
                self,
                url: str,
                data: dict[str, str],
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                self.url = url
                self.data = data
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text="""
                    {"actions":[{"state":"SUCCESS","returnValue":{"arcusbuilt__PApplication__c":[{
                      "Id":"a1M1", "Name":"F/26/101703",
                      "Portal_Site_Address__c":"1 High Street, SO50 1AA",
                      "arcusbuilt__Proposal__c":"Entrance gates",
                      "arcusbuilt__ReceivedDate__c":"2026-07-06",
                      "arcusbuilt__Validation_Date__c":"2026-07-14",
                      "arcusbuilt__Status__c":"Valid"
                    }]}}]}
                    """,
                )

        class TestEastleighPlanningScraper(EastleighPlanningScraper):
            def _aura_context(self, text: str) -> dict[str, object]:
                return {"mode": "PROD", "fwuid": "test", "app": "siteforce:napiliApp", "loaded": {}}

        http = FakeEastleighHttp()
        scraper = TestEastleighPlanningScraper(
            LegacyFormsCouncilConfig("Eastleigh", "https://planning.example.gov.uk"),
            http_client=http,
        )

        discovery = scraper.discover_ids(
            listing_url="https://planning.example.gov.uk/s/public-register",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        message = json.loads(http.data["message"])
        self.assertEqual(message["actions"][0]["params"]["dateRecFrom"], "2026-07-06")
        self.assertIn("LCPublicRegCont.advancedSearch", http.url)
        application = discovery.applications[0]
        self.assertEqual(application.reference, "F/26/101703")
        self.assertEqual(application.date_received, "2026-07-06")
        self.assertEqual(application.postcode, "SO50 1AA")

    def test_elmbridge_retries_empty_busy_results_with_bounded_page_size(self) -> None:
        class FakeElmbridgeHttp:
            def __init__(self) -> None:
                self.result_requests: list[dict[str, str]] = []

            def get(
                self,
                url: str,
                params: dict[str, str] | None = None,
                headers: dict[str, str] | None = None,
            ) -> FetchResponse:
                if params is None:
                    return FetchResponse(
                        url=url,
                        status_code=200,
                        text="""
                        <form>
                          <input name="daterec_from:PARAM"><input name="daterec_to:PARAM">
                          <select name="pagerecs"><option value="10">10</option><option value="50">50</option><option value="500">500</option></select>
                        </form>
                        """,
                    )
                self.result_requests.append(params)
                rows = ""
                if len(self.result_requests) == 3:
                    rows = "<tr><td>2026/1234</td><td>1 High Street, KT10 1AA</td><td>Entrance gates</td><td>08/07/2026</td></tr>"
                return FetchResponse(
                    url=url,
                    status_code=200,
                    text=f"""
                    <table>
                      <tr><th>Application number</th><th>Address</th><th>Proposal</th><th>Date received</th></tr>
                      {rows}
                    </table>
                    """,
                )

        http = FakeElmbridgeHttp()
        scraper = ElmbridgePlanningScraper(
            LegacyFormsCouncilConfig("Elmbridge", "https://emaps.example.gov.uk"),
            http_client=http,
        )
        scraper.busy_retry_seconds = 0

        discovery = scraper.discover_ids(
            listing_url="https://emaps.example.gov.uk/ebc_planning.aspx?template=AdvancedSearchTab.tmplt",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertEqual(len(http.result_requests), 3)
        self.assertEqual(http.result_requests[0]["pagerecs"], "50")
        self.assertEqual(http.result_requests[0]["daterec_from:PARAM"], "2026-07-06")
        self.assertEqual(discovery.applications[0].reference, "2026/1234")


if __name__ == "__main__":
    unittest.main()
