from __future__ import annotations

from datetime import date
import unittest
from pathlib import Path
from unittest.mock import patch

from lead_generator.planning.http import FetchResponse
from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.civica import (
    CivicaCouncilConfig,
    CivicaPlanningScraper,
    fetch_civica_documents_from_raw,
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
