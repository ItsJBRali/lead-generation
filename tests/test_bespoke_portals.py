from __future__ import annotations

import base64
import json
import struct
import unittest
from datetime import date, datetime
from urllib.parse import quote
from zoneinfo import ZoneInfo

from lead_generator.planning.adapters.arcus import ArcusCouncilConfig
from lead_generator.planning.adapters.bespoke_portals import (
    BathPlanningScraper,
    CarmarthenshirePlanningScraper,
    ColchesterPlanningScraper,
    KensingtonPlanningScraper,
    TelfordPlanningScraper,
    WestDunbartonshirePlanningScraper,
)
from lead_generator.planning.adapters.legacy_forms import LegacyFormsCouncilConfig
from lead_generator.planning.http import BinaryFetchResponse, CouncilFetchError, FetchResponse


class BathHttp:
    def __init__(self) -> None:
        self.payloads: list[dict[str, str]] = []

    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        return FetchResponse("https://app.bathnes.gov.uk/webforms/planning/search.html", 200, "<html></html>")

    def post_json(self, url: str, data: object, headers=None) -> FetchResponse:
        self.payloads.append(data)
        duplicate = {
            "refval": "26/00001/FUL",
            "addressline": "1 High Street, Bath, BA1 1AA",
            "proposal": "New shopfront",
            "dateaprecv": "2026-07-10T00:00:00",
            "dateapval": "2026-07-11T00:00:00",
        }
        records = [duplicate]
        if "application_isharedate_from" in data:
            records.append(
                {
                    "refval": "26/00002/FUL",
                    "addressline": "2 High Street, Bath, BA1 1AB",
                    "proposal": "Rear extension",
                    "dateaprecv": "2026-07-12T00:00:00",
                    "dateapval": "2026-07-13T00:00:00",
                }
            )
        return FetchResponse(url, 200, json.dumps(records))


class ColchesterHttp:
    def __init__(self) -> None:
        self.pages: list[int] = []
        configuration = base64.b64encode(json.dumps([{"Base64SecureConfiguration": "secure"}]).encode()).decode()
        self.listing = f'<div data-get-url="/_services/entity-grid-data.json/grid" data-view-layouts="{configuration}"></div>'

    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        if url.endswith("/_layout/tokenhtml"):
            return FetchResponse(url, 200, '<input name="__RequestVerificationToken" value="token">')
        return FetchResponse("https://www.colchester.gov.uk/planning-search-results/", 200, self.listing)

    def post_json(self, url: str, data: object, headers=None) -> FetchResponse:
        page = data["page"]
        self.pages.append(page)
        records = (
            [
                self._record("newer", "261200", "13/07/2026"),
                self._record("wanted", "261201", "10/07/2026"),
            ]
            if page == 1
            else [self._record("older", "261100", "05/07/2026")]
        )
        return FetchResponse(
            url,
            200,
            json.dumps(
                {
                    "Records": records,
                    "MoreRecords": page == 1,
                    "NextPagePagingCookie": "next" if page == 1 else "",
                }
            ),
        )

    def _record(self, uid: str, reference: str, received: str) -> dict[str, object]:
        values = {
            "new_name": reference,
            "new_registration_date": received,
            "new_concatenatedaddress": "1 North Hill, Colchester, CO1 1AA",
            "new_development_desc": "New dwelling",
            "new_application_status": "Application Valid",
        }
        return {
            "Id": uid,
            "Attributes": [
                {"Name": name, "DisplayValue": value, "Value": value}
                for name, value in values.items()
            ],
        }


class TelfordHttp:
    search_form = """
        <form>
          <input type="hidden" name="__VIEWSTATE" value="state">
          <select name="kind"><option value="0">All</option></select>
          <input name="ctl00$ContentPlaceHolder1$DCdatefrom">
          <input name="ctl00$ContentPlaceHolder1$DCdateto">
        </form>
    """

    def __init__(self) -> None:
        self.posts: list[dict[str, str]] = []

    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        return FetchResponse("https://secure.telford.gov.uk/planningsearch/", 200, self.search_form)

    def post_form(self, url: str, data: dict[str, str], headers=None) -> FetchResponse:
        self.posts.append(data)
        day = data["ctl00$ContentPlaceHolder1$DCdatefrom"][:2]
        result = f"""
            <table>
              <tr><th>Application number</th><th>Date valid</th><th>Site address</th><th>Description</th></tr>
              <tr>
                <td><a href="pa-applicationsummary.aspx?applicationnumber=TWC%2F2026%2F00{day}">TWC/2026/00{day}</a></td>
                <td>{data['ctl00$ContentPlaceHolder1$DCdatefrom'].replace('-', '/')}</td>
                <td>{day} High Street, Telford, TF1 1AA</td><td>New dwelling</td>
              </tr>
            </table>
        """
        return FetchResponse(url, 200, result)


class WestDunbartonshireHttp:
    def __init__(self) -> None:
        self.search_params: dict[str, str] | None = None

    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        if "dcdisplayinitial.asp" in url:
            self.search_params = params
            return FetchResponse(
                url,
                200,
                """
                <table><tr><td>410 Glasgow Road, Clydebank, G81 1PW</td><td>
                  <form action="dcdisplayfullx.asp">
                    <input name="vUPRN" value="DC26/133/FUL">
                    <input name="vPassword" value="">
                    <input type="submit" name="View1" value="View">
                  </form>
                </td></tr></table>
                """,
            )
        return FetchResponse(
            url,
            200,
            """
            <form action="dcdisplayinitial.asp" method="get">
              <select name="vStatus"><option value="">All</option></select>
              <input name="vDateRcvFr"><input name="vDateRcvTo">
            </form>
            """,
        )


class CarmarthenshireHttp:
    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        boot = quote(
            json.dumps(
                {
                    "mode": "PROD",
                    "fwuid": "framework-id",
                    "app": "siteforce:communityApp",
                    "loaded": {"APPLICATION@markup://siteforce:communityApp": "1"},
                }
            ),
            safe="",
        )
        return FetchResponse(url, 200, f'<script src="/s/sfsites/l/{boot}/bootstrap.js"></script>')

    def post_form(self, url: str, data: dict[str, str], headers=None) -> FetchResponse:
        message = json.loads(data["message"])
        criteria = message["actions"][0]["params"]["search_criteria"]
        assert criteria["arcusbuiltenv__Registration_Date__c:from"] == "2026-07-06"
        record = {
            "Id": "a0b123",
            "Name": "PL/10926",
            "arcusbuiltenv__Registration_Date__c": "2026-07-10",
            "arcusbuiltenv__Status__c": "Under Consultation",
            "Hidden_Proposal__c": "New employment building",
            "arcusbuiltenv__Site_Address__c": "Bynea, Llanelli, SA14 9LS",
            "Documents_URL__c": "https://documents.example.test/PL-10926",
        }
        return FetchResponse(url, 200, json.dumps({"actions": [{"returnValue": json.dumps({"records": [record]})}]}))


class KensingtonHttp:
    def get(self, url: str, params=None, headers=None) -> FetchResponse:
        return FetchResponse(
            "https://www.rbkc.gov.uk/planningsearch",
            200,
            '<html><title>RBKC Planning Portal</title><script src="/_build/client.js"></script></html>',
        )

    def get_bytes(self, url: str, params=None, headers=None) -> BinaryFetchResponse:
        assert params["dateFrom"] == "1783292400000"
        assert params["dateTo"] == "1783897199999"
        assert headers["Referer"] == "https://www.rbkc.gov.uk/planningsearch"
        records = [
            self._record(False, "PP/26/01234", "Planning application", "2026-07-10"),
            self._record(True, "ENF/26/00001", "Enforcement investigation", "2026-07-10"),
        ]
        return BinaryFetchResponse(url, 200, struct.pack("<I", len(records)) + b"".join(records))

    def _record(self, enforcement: bool, reference: str, description: str, received: str) -> bytes:
        values = (
            "100023000001",
            "1 High Street, London, W8 4PU",
            "Planning",
            reference,
            description,
            f"case-{reference}",
            "Application",
            "Application",
            "Under consideration",
            "Registered",
            "Application under consideration",
        )
        received_date = datetime.fromisoformat(received).replace(tzinfo=ZoneInfo("Europe/London"))
        ticks = int(received_date.timestamp() * 1000) * 10_000 + 621355968000000000
        prefix = struct.pack(
            "<BBBIIQQQdd",
            enforcement,
            True,
            False,
            0,
            100,
            ticks,
            ticks,
            ticks,
            51.5,
            -0.19,
        )
        return prefix + b"".join(self._string(value) for value in values)

    def _string(self, value: str) -> bytes:
        encoded = value.encode("utf-8")
        return struct.pack("<I", len(encoded)) + encoded


class BespokePortalTests(unittest.TestCase):
    def test_bath_merges_validated_and_publication_searches(self) -> None:
        http = BathHttp()
        scraper = BathPlanningScraper(LegacyFormsCouncilConfig("Bath", "https://app.bathnes.gov.uk"), http_client=http)
        result = scraper.discover_ids(
            listing_url="https://app.bathnes.gov.uk/webforms/planning/search.html",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )
        self.assertEqual([app.reference for app in result.applications], ["26/00001/FUL", "26/00002/FUL"])
        self.assertEqual(len(http.payloads), 2)

    def test_colchester_pages_until_results_are_older_than_window(self) -> None:
        http = ColchesterHttp()
        scraper = ColchesterPlanningScraper(
            LegacyFormsCouncilConfig("Colchester", "https://www.colchester.gov.uk"),
            http_client=http,
        )
        result = scraper.discover_ids(
            listing_url="https://www.colchester.gov.uk/planning-search-results/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )
        self.assertEqual([app.reference for app in result.applications], ["261201"])
        self.assertEqual(http.pages, [1, 2])

    def test_telford_searches_each_day_to_avoid_result_cap(self) -> None:
        http = TelfordHttp()
        scraper = TelfordPlanningScraper(
            LegacyFormsCouncilConfig("Telford", "https://secure.telford.gov.uk"),
            http_client=http,
        )
        result = scraper.discover_ids(
            listing_url="https://secure.telford.gov.uk/planningsearch/",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )
        self.assertEqual(len(result.applications), 7)
        self.assertEqual(len(http.posts), 7)

    def test_west_dunbartonshire_preserves_blank_all_filter(self) -> None:
        http = WestDunbartonshireHttp()
        scraper = WestDunbartonshirePlanningScraper(
            LegacyFormsCouncilConfig("West Dunbartonshire", "https://apps.west-dunbarton.gov.uk"),
            http_client=http,
        )
        result = scraper.discover_ids(
            listing_url="https://apps.west-dunbarton.gov.uk/dcsearch_app.asp",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )
        self.assertEqual(http.search_params["vStatus"], "")
        self.assertEqual(result.applications[0].reference, "DC26/133/FUL")

    def test_carmarthenshire_parses_custom_salesforce_response(self) -> None:
        scraper = CarmarthenshirePlanningScraper(
            ArcusCouncilConfig("Carmarthenshire", "https://carmarthenshire.my.site.com/en"),
            http_client=CarmarthenshireHttp(),
        )
        result = scraper.discover_ids(
            listing_url="https://carmarthenshire.my.site.com/en/s/?tabset-a3431=3",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )
        self.assertEqual(result.applications[0].reference, "PL/10926")
        self.assertEqual(result.applications[0].raw["docs_url"], "https://documents.example.test/PL-10926")

    def test_kensington_decodes_binary_results_and_excludes_enforcement(self) -> None:
        scraper = KensingtonPlanningScraper(
            LegacyFormsCouncilConfig("Kensington", "https://www.rbkc.gov.uk"),
            http_client=KensingtonHttp(),
        )
        result = scraper.discover_ids(
            listing_url="https://www.rbkc.gov.uk/planningsearch",
            start_date=date(2026, 7, 6),
            end_date=date(2026, 7, 12),
        )

        self.assertEqual([application.reference for application in result.applications], ["PP/26/01234"])
        self.assertEqual(result.applications[0].date_received, "2026-07-10")
        self.assertIn("identifier=Planning", result.applications[0].raw["docs_url"])

    def test_kensington_reports_the_council_side_outage(self) -> None:
        class OutageHttp:
            def get(self, url: str, params=None, headers=None) -> FetchResponse:
                return FetchResponse(url, 200, "<h1>We are responding to a cybersecurity issue</h1><p>Cyber recovery</p>")

        scraper = KensingtonPlanningScraper(
            LegacyFormsCouncilConfig("Kensington", "https://www.rbkc.gov.uk"),
            http_client=OutageHttp(),
        )
        with self.assertRaisesRegex(CouncilFetchError, "cyber recovery"):
            scraper.discover_ids(
                listing_url="https://www.rbkc.gov.uk/planningsearch",
                start_date=date(2026, 7, 6),
                end_date=date(2026, 7, 12),
            )


if __name__ == "__main__":
    unittest.main()
