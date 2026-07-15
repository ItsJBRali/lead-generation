from __future__ import annotations

import base64
import csv
import io
import json
import re
from collections.abc import Callable
from datetime import date, timedelta
from time import sleep
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.arcus import ArcusCouncilConfig, ArcusPlanningScraper
from lead_generator.planning.adapters.atrium import AtriumCouncilConfig, AtriumPlanningScraper
from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.adapters.legacy_forms import (
    AstunPlanningScraper,
    CcedPlanningScraper,
    EnterpriseStorePlanningScraper,
    FastwebPlanningScraper,
    HtmlListPlanningScraper,
    LegacyFormsCouncilConfig,
    QueryFormPlanningScraper,
    SocrataPlanningScraper,
    TascomiPlanningScraper,
    filter_by_date,
    parse_header_tables,
)
from lead_generator.planning.adapters.wiltshire import WiltshirePlanningScraper
from lead_generator.planning.http import CouncilFetchError
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


class AshfordPlanningScraper(ArcusPlanningScraper):
    """Ashford's Arcus register."""

    def _search_filters(
        self,
        start_date: date | None,
        end_date: date | None,
    ) -> list[dict[str, str]]:
        return [
            {
                "fieldName": "arcusbuiltenv__Received_Date__c",
                "fieldValue": start_date.isoformat() if start_date else "",
                "fieldDeveloperName": "PA_ADV_DateReceivedFrom",
            },
            {
                "fieldName": "arcusbuiltenv__Received_Date__c",
                "fieldValue": end_date.isoformat() if end_date else "",
                "fieldDeveloperName": "PA_ADV_DateReceivedTo",
            },
        ]


class BcpPlanningScraper(AtriumPlanningScraper):
    """BCP's Atrium register, including slash-delimited references."""


class WychavonPlanningScraper(AtriumPlanningScraper):
    """Wychavon's Atrium register."""


class BarkingAndDagenhamPlanningScraper(TascomiPlanningScraper):
    """Barking and Dagenham's Tascomi register."""


class WorcestershirePlanningScraper(AtriumPlanningScraper):
    """Worcestershire County Council's Atrium register."""


class WorcesterPlanningScraper(AtriumPlanningScraper):
    """Worcester City Council's Atrium register."""


class WokinghamPlanningScraper(FastwebPlanningScraper):
    """Wokingham's Fastweb register."""


class WestSussexPlanningScraper(AtriumPlanningScraper):
    """West Sussex County Council's Atrium register."""


class WestNorthamptonshirePlanningScraper(AtriumPlanningScraper):
    """West Northamptonshire's Atrium register."""


class WelwynHatfieldPlanningScraper(AtriumPlanningScraper):
    """Welwyn Hatfield's Atrium register."""


class WalthamForestPlanningScraper(TascomiPlanningScraper):
    """Waltham Forest's Tascomi register."""


class WeeklyCsvAtriumPlanningScraper(AtriumPlanningScraper):
    """Atrium variant whose recaptcha-free weekly CSV is the authoritative search."""

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        parts = urlsplit(listing_url)
        root = f"{parts.scheme}://{parts.netloc}"
        self.http.get(root + "/")
        cursor = start_date or end_date or date.today()
        final_date = end_date or cursor
        applications: list[PlanningApplication] = []
        seen: set[str] = set()

        while cursor <= final_date:
            response = self.http.post_form(
                root + "/Planning/GetWeeklyListCSV",
                {"fromDate": cursor.strftime("%d/%m/%Y")},
            )
            for application in self._parse_weekly_csv(
                response.text,
                root,
                response.url,
                report_date=cursor,
            ):
                key = (application.reference or application.uid).casefold()
                if key in seen:
                    continue
                seen.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return DiscoveryResult(
                        authority=self.authority,
                        source_url=response.url,
                        applications=applications,
                    )
            cursor += timedelta(days=7)

        return DiscoveryResult(
            authority=self.authority,
            source_url=root + "/Planning/GetWeeklyListCSV",
            applications=applications,
        )

    def _parse_weekly_csv(
        self,
        response_text: str,
        root: str,
        source_url: str,
        *,
        report_date: date | None = None,
    ) -> list[PlanningApplication]:
        encoded = json.loads(response_text)
        csv_text = base64.b64decode(encoded).decode("utf-8-sig")
        rows = list(csv.reader(io.StringIO(csv_text)))
        header_index = next(
            (index for index, row in enumerate(rows) if row and row[0].strip() == "application_number"),
            None,
        )
        if header_index is None:
            return []
        headers = rows[header_index]
        applications: list[PlanningApplication] = []
        for values in rows[header_index + 1 :]:
            if not values or not any(value.strip() for value in values):
                continue
            record = dict(zip(headers, values, strict=False))
            reference_text = clean_text(record.get("application_number")) or ""
            reference = re.sub(
                r"^Application(?:\s+No\.)?\s*",
                "",
                reference_text,
                flags=re.IGNORECASE,
            ).strip()
            if not reference:
                continue
            address = clean_text(record.get("location1") or record.get("location"))
            valid_text = self._weekly_value(record.get("received_complete_date"), "Valid")
            received_text = self._weekly_value(record.get("valid_start_date"), "Registered")
            received_date = parse_council_date(received_text)
            date_inferred = received_date is None and report_date is not None
            if date_inferred:
                received_date = report_date.isoformat()
            detail_url = root + "/Planning/Display?" + urlencode({"applicationNumber": reference})
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=detail_url,
                    reference=reference,
                    address=address,
                    description=clean_text(record.get("proposal1") or record.get("proposal")),
                    date_received=received_date,
                    date_validated=parse_council_date(valid_text),
                    postcode=extract_postcode(address),
                    source_url=source_url,
                    raw={
                        "portal_family": "atrium_weekly_csv",
                        "detail_complete": True,
                        "date_range_filtered": True,
                        "date_inferred_from_weekly_report": date_inferred,
                        "record": record,
                    },
                )
            )
        return applications

    def _weekly_value(self, value: str | None, label: str) -> str | None:
        text = clean_text(value)
        if not text:
            return None
        return re.sub(rf"^{re.escape(label)}\s*", "", text, flags=re.IGNORECASE).strip()


class ValeOfWhiteHorsePlanningScraper(WeeklyCsvAtriumPlanningScraper):
    """Vale of White Horse's Atrium register."""


class BromleyPlanningScraper(ArcusPlanningScraper):
    """Bromley's Arcus register."""


class BroxbournePlanningScraper(EnterpriseStorePlanningScraper):
    """Broxbourne's NEC Enterprise register."""


class TauntonDeanePlanningScraper(QueryFormPlanningScraper):
    """The legacy Taunton Deane planning search."""

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        response = self.http.get(listing_url)
        form = self._pick_form(html.fromstring(response.text))
        if form is None:
            return []
        data = self._form_defaults(form)
        self._set_dates(data, start_date, end_date)
        data["submit"] = "Search"
        result = self.http.post_form(self._absolute_action(response.url, form), data)

        document = html.fromstring(result.text)
        full_list_forms = document.xpath("//form[.//input[@name='ViewAll' and @value='All']]")
        if full_list_forms:
            full_list_form = full_list_forms[0]
            result = self.http.post_form(
                self._absolute_action(result.url, full_list_form),
                self._form_defaults(full_list_form),
            )

        applications = self._parse_results(result.text, result.url)
        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def _parse_results(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        for table in document.xpath("//table[.//a[contains(@href, 'PlAppDets') or contains(@href, 'plappdets')]]"):
            anchors = table.xpath(".//a[contains(@href, 'PlAppDets') or contains(@href, 'plappdets')]")
            if not anchors:
                continue
            href = anchors[0].get("href") or ""
            detail_url = urljoin(page_url, href)
            reference = (parse_qs(urlsplit(detail_url).query).get("casefullref") or [None])[0]
            if not reference:
                match = re.search(r"Application number\s*:\s*(\S+)", clean_text(" ".join(table.itertext())) or "", re.IGNORECASE)
                reference = match.group(1) if match else None
            if not reference or reference.casefold() in seen:
                continue
            seen.add(reference.casefold())

            table_text = clean_text(" ".join(table.itertext())) or ""
            registered_match = re.search(r"Registered\s*:\s*(\d{1,2}/\d{1,2}/\d{4})", table_text, re.IGNORECASE)
            date_received = parse_council_date(registered_match.group(1)) if registered_match else None
            descriptions = [
                clean_text(" ".join(cell.itertext()))
                for cell in table.xpath(".//td[@colspan='2'][not(.//form)]")
            ]
            descriptions = [
                value
                for value in descriptions
                if value and "decision has" not in value.casefold()
            ]
            description = max(descriptions, key=len) if descriptions else None
            address = None
            if description and " at " in description.casefold():
                address = re.split(r"\s+at\s+", description, maxsplit=1, flags=re.IGNORECASE)[-1].rstrip(".")
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=detail_url,
                    reference=reference,
                    address=address,
                    description=description,
                    date_received=date_received,
                    postcode=extract_postcode(address),
                    source_url=page_url,
                    raw={
                        "portal_family": self.family,
                        "detail_complete": True,
                        "listing_text": table_text,
                    },
                )
            )
        return applications


class CamdenPlanningScraper(SocrataPlanningScraper):
    """Camden's public Socrata planning dataset."""


class CentralBedfordshirePlanningScraper(QueryFormPlanningScraper):
    """Central Bedfordshire's custom date-search register."""

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        response = self.http.get(listing_url)
        form = self._pick_form(html.fromstring(response.text))
        if form is None:
            return super().search(
                listing_url,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
        data = self._form_defaults(form)
        self._set_dates(data, start_date, end_date)
        result = self.http.get(self._absolute_action(response.url, form), data)

        applications: list[PlanningApplication] = []
        seen_references: set[str] = set()
        seen_pages: set[str] = set()
        while result.url not in seen_pages:
            seen_pages.add(result.url)
            page_apps = parse_header_tables(result.text, result.url, self.authority, self.family)
            if not page_apps:
                page_apps = HtmlListPlanningScraper(
                    self.config,
                    http_client=self.http,
                ).parse_listing(result.text, result.url)
            for application in page_apps:
                key = (application.reference or application.uid).casefold()
                if key in seen_references:
                    continue
                seen_references.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return filter_by_date(applications, start_date, end_date)

            document = html.fromstring(result.text)
            next_links = [
                anchor.get("href")
                for anchor in document.xpath("//a[@href]")
                if (clean_text(" ".join(anchor.itertext())) or "").casefold() == "next"
            ]
            if not next_links:
                break
            next_url = urljoin(result.url, next_links[0])
            if next_url in seen_pages:
                break
            result = self.http.get(next_url)

        return filter_by_date(applications, start_date, end_date)


class TandridgePlanningScraper(QueryFormPlanningScraper):
    """Tandridge's custom planning search."""

    SEARCH_CRITERIA_FIELD = "ctl00$MainContent$ddlSearchCriteria"
    SEARCH_BUTTON_FIELD = "ctl00$MainContent$btnSearch"
    START_DATE_FIELD = "ctl00$MainContent$txtStartDate"
    END_DATE_FIELD = "ctl00$MainContent$txtEndDate"

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        response = self.http.get(listing_url)
        forms = html.fromstring(response.text).xpath("//form")
        if not forms:
            return []

        criteria_data = self._form_defaults(forms[0])
        criteria_data[self.SEARCH_CRITERIA_FIELD] = "Acknowledged date"
        criteria_data[self.SEARCH_BUTTON_FIELD] = "Search"
        date_page = self.http.post_form(
            self._absolute_action(response.url, forms[0]),
            criteria_data,
        )

        date_forms = html.fromstring(date_page.text).xpath("//form")
        if not date_forms:
            return []
        date_form = date_forms[0]
        data = self._form_defaults(date_form)
        data[self.SEARCH_CRITERIA_FIELD] = "Acknowledged date"
        if start_date:
            data[self.START_DATE_FIELD] = start_date.isoformat()
        if end_date:
            data[self.END_DATE_FIELD] = end_date.isoformat()
        data[self.SEARCH_BUTTON_FIELD] = "Search"
        result = self.http.post_form(self._absolute_action(date_page.url, date_form), data)

        applications = parse_header_tables(result.text, result.url, self.authority, self.family)
        return applications[:limit] if limit is not None else applications


class SurreyPlanningScraper(AtriumPlanningScraper):
    """Surrey County Council's legacy planning register."""


class StratfordOnAvonPlanningScraper(HtmlListPlanningScraper):
    """Stratford-on-Avon's public planning application list."""

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        parts = urlsplit(listing_url)
        app_root = parts.path.rstrip("/") or "/EplanningV2"
        if not app_root.casefold().endswith("/eplanningv2"):
            marker = app_root.casefold().find("/eplanningv2")
            app_root = app_root[: marker + len("/eplanningv2")] if marker >= 0 else "/EplanningV2"
        api_url = f"{parts.scheme}://{parts.netloc}{app_root}/API/v1/Search"
        params = {"activeOnly": "false"}
        if start_date:
            params["dateAppReceivedFrom"] = start_date.isoformat()
        if end_date:
            params["dateAppReceivedTo"] = end_date.isoformat()
        response = self.http.get(api_url, params)
        records = json.loads(response.text)
        applications = [self._from_api_record(record, api_url) for record in records]
        return applications[:limit] if limit is not None else applications

    def _from_api_record(self, record: dict[str, object], source_url: str) -> PlanningApplication:
        reference = clean_text(str(record.get("reference") or ""))
        uid = clean_text(str(record.get("id") or reference or source_url)) or source_url
        address = clean_text(str(record.get("address") or ""))
        detail_url = clean_text(str(record.get("link") or "")) or source_url
        valid_date = parse_council_date(clean_text(str(record.get("validDate") or "")))
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=detail_url,
            reference=reference,
            address=address,
            description=clean_text(str(record.get("proposal") or "")),
            status=clean_text(str(record.get("status") or "")),
            decision=clean_text(str(record.get("decision") or "")),
            date_validated=valid_date,
            postcode=extract_postcode(address),
            source_url=source_url,
            raw={"portal_family": "stratford_api", "detail_complete": True, "record": record},
        )


class CoventryPlanningScraper(TascomiPlanningScraper):
    """Coventry's Tascomi register."""


class SouthOxfordshirePlanningScraper(WeeklyCsvAtriumPlanningScraper):
    """South Oxfordshire's Atrium register."""


class CrawleyPlanningScraper(AtriumPlanningScraper):
    """Crawley's card-based Atrium register."""


class DevonPlanningScraper(AtriumPlanningScraper):
    """Devon County Council's Atrium register."""


class DorsetPlanningScraper(CcedPlanningScraper):
    """Dorset's CCED register."""


class EastSussexPlanningScraper(QueryFormPlanningScraper):
    """East Sussex County Council's custom planning search."""

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        parts = urlsplit(listing_url)
        root_path = parts.path.rstrip("/")
        if not root_path.casefold().endswith("/register"):
            marker = root_path.casefold().find("/register")
            root_path = root_path[: marker + len("/register")] if marker >= 0 else root_path
        result_url = f"{parts.scheme}://{parts.netloc}{root_path}/results"
        params = {"typ": "dmw_planning"}
        if start_date:
            params["sd"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            params["ed"] = end_date.strftime("%d/%m/%Y")
        response = self.http.get(result_url, params)
        applications = parse_header_tables(response.text, response.url, self.authority, self.family)
        if not applications:
            applications = HtmlListPlanningScraper(
                self.config,
                http_client=self.http,
            ).parse_listing(response.text, response.url)
        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications


class EastleighPlanningScraper(WiltshirePlanningScraper):
    """Eastleigh's Salesforce public register received-date search."""

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        page = self.http.get(listing_url)
        context = self._aura_context(page.text)
        params = {
            "keywords": "",
            "ward": "",
            "parish": "",
            "determination": "",
            "undecidedOnly": "",
            "recordTypeName": "",
            "dateRecFrom": start_date.isoformat() if start_date else "",
            "dateRecTo": end_date.isoformat() if end_date else "",
            "dateDecFrom": "",
            "dateDecTo": "",
        }
        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "apex://LCPublicRegCont/ACTION$advancedSearch",
                    "callingDescriptor": "markup://c:PubRegAdvSearch",
                    "params": params,
                    "version": None,
                }
            ]
        }
        parts = urlsplit(listing_url)
        endpoint = (
            f"{parts.scheme}://{parts.netloc}{self._path_prefix(parts.path)}"
            "/s/sfsites/aura?r=10&other.LCPublicRegCont.advancedSearch=1"
        )
        response = self.http.post_form(
            endpoint,
            {
                "message": json.dumps(message, separators=(",", ":")),
                "aura.context": json.dumps(context, separators=(",", ":")),
                "aura.pageURI": self._page_uri(listing_url),
                "aura.token": "null",
            },
        )
        records = self._search_records_from_response(response.text)
        applications = [self._application_from_eastleigh_record(record, listing_url) for record in records]
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(
            authority=self.authority,
            source_url=listing_url,
            applications=applications,
        )

    def _search_records_from_response(self, response_text: str) -> list[dict[str, object]]:
        try:
            payload = json.loads(response_text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError("Invalid Eastleigh planning search response") from exc
        for action in payload.get("actions", []):
            if not isinstance(action, dict):
                continue
            if str(action.get("state") or "").upper() == "ERROR":
                raise CouncilFetchError(f"Eastleigh planning search failed: {action.get('error')}")
            result = action.get("returnValue")
            if not isinstance(result, dict):
                continue
            records = result.get("arcusbuilt__PApplication__c")
            if isinstance(records, list):
                return [record for record in records if isinstance(record, dict)]
        raise CouncilFetchError("Eastleigh planning search action was missing from the response")

    def _application_from_eastleigh_record(
        self,
        record: dict[str, object],
        listing_url: str,
    ) -> PlanningApplication:
        uid = clean_text(str(record.get("Id") or ""))
        reference = clean_text(str(record.get("Name") or ""))
        if not uid:
            raise CouncilFetchError("Eastleigh returned an application without an identifier")
        location = record.get("arcusbuilt__Location__r")
        related_address = (
            location.get("arcusgazetteer__Address__c")
            if isinstance(location, dict)
            else None
        )
        address = clean_text(
            str(record.get("Portal_Site_Address__c") or related_address or "")
        )
        parts = urlsplit(listing_url)
        detail_url = (
            f"{parts.scheme}://{parts.netloc}{self._path_prefix(parts.path)}/s/detail/{uid}"
        )
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=detail_url,
            reference=reference or uid,
            address=address,
            description=clean_text(str(record.get("arcusbuilt__Proposal__c") or "")),
            status=clean_text(str(record.get("arcusbuilt__Status__c") or "")),
            decision=clean_text(str(record.get("arcusbuilt__Last_Decision__c") or "")),
            date_received=parse_council_date(
                clean_text(str(record.get("arcusbuilt__ReceivedDate__c") or ""))
            ),
            date_validated=parse_council_date(
                clean_text(str(record.get("arcusbuilt__Validation_Date__c") or ""))
            ),
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": "eastleigh_salesforce",
                "api": "LCPublicRegCont.advancedSearch",
                "detail_complete": True,
                "date_range_filtered": True,
                "record": record,
            },
        )


class ElmbridgePlanningScraper(AstunPlanningScraper):
    """Elmbridge's Astun register."""

    busy_retry_seconds = 3.0

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        response = self.http.get(listing_url)
        document = html.fromstring(response.text)
        forms = document.xpath("//form")
        if not forms:
            return []
        form = forms[0]
        data = self._form_defaults(form)
        if start_date:
            data["daterec_from:PARAM"] = start_date.isoformat()
        if end_date:
            data["daterec_to:PARAM"] = end_date.isoformat()
        page_sizes = {
            int(value)
            for value in form.xpath(".//select[@name='pagerecs']/option/@value")
            if value.isdigit()
        }
        if page_sizes:
            data["pagerecs"] = str(50 if 50 in page_sizes else min(page_sizes))
        parts = urlsplit(response.url)
        action = parts._replace(query="", fragment="").geturl()
        applications: list[PlanningApplication] = []
        result = response
        for attempt in range(3):
            result = self.http.get(action, data)
            applications = parse_header_tables(result.text, result.url, self.authority, self.family)
            if applications or self._explicit_no_results(result.text):
                break
            if attempt < 2:
                sleep(self.busy_retry_seconds)
        if not applications and self._busy_empty_results(result.text):
            raise CouncilFetchError(
                "Elmbridge returned its documented busy-page empty result table"
            )
        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def _explicit_no_results(self, html_text: str) -> bool:
        page_text = (clean_text(" ".join(html.fromstring(html_text).itertext())) or "").casefold()
        return any(
            marker in page_text
            for marker in (
                "no matching applications",
                "no applications found",
                "no records found",
                "your search returned no results",
            )
        )

    def _busy_empty_results(self, html_text: str) -> bool:
        document = html.fromstring(html_text)
        has_empty_result_table = bool(
            document.xpath(
                "//table[.//th[contains(translate(normalize-space(.), "
                "'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), "
                "'application number')]][count(.//tr) = 1]"
            )
        )
        page_text = (clean_text(" ".join(document.itertext())) or "").casefold()
        return has_empty_result_table and "website is busy" in page_text


class SomersetPlanningScraper(AtriumPlanningScraper):
    """Somerset Council's Atrium register."""


class EssexPlanningScraper(AtriumPlanningScraper):
    """Essex County Council's Atrium register."""


class ExmoorPlanningScraper(WeeklyCsvAtriumPlanningScraper):
    """Exmoor National Park's Atrium register."""


class GloucestershirePlanningScraper(TascomiPlanningScraper):
    """Gloucestershire County Council's Tascomi register."""


class ShepwayPlanningScraper(ArcusPlanningScraper):
    """Folkestone and Hythe's Arcus register under its legacy name."""


def _arcus(adapter: type[ArcusPlanningScraper]) -> Callable[[str, str], PlanningScraper]:
    return lambda authority, base_url: adapter(ArcusCouncilConfig(authority=authority, base_url=base_url))


def _atrium(adapter: type[AtriumPlanningScraper]) -> Callable[[str, str], PlanningScraper]:
    return lambda authority, base_url: adapter(AtriumCouncilConfig(authority=authority, base_url=base_url))


def _legacy(adapter: type[PlanningScraper]) -> Callable[[str, str], PlanningScraper]:
    return lambda authority, base_url: adapter(LegacyFormsCouncilConfig(authority=authority, base_url=base_url))


AUTHORITY_ADAPTER_FACTORIES: dict[str, Callable[[str, str], PlanningScraper]] = {
    "ashford": _arcus(AshfordPlanningScraper),
    "bcp": _atrium(BcpPlanningScraper),
    "wychavon": _atrium(WychavonPlanningScraper),
    "barking and dagenham": _legacy(BarkingAndDagenhamPlanningScraper),
    "worcestershire": _atrium(WorcestershirePlanningScraper),
    "worcester": _atrium(WorcesterPlanningScraper),
    "wokingham": _legacy(WokinghamPlanningScraper),
    "west sussex": _atrium(WestSussexPlanningScraper),
    "west northamptonshire": _atrium(WestNorthamptonshirePlanningScraper),
    "welwyn hatfield": _atrium(WelwynHatfieldPlanningScraper),
    "waltham forest": _legacy(WalthamForestPlanningScraper),
    "vale of white horse": _atrium(ValeOfWhiteHorsePlanningScraper),
    "bromley": _arcus(BromleyPlanningScraper),
    "broxbourne": _legacy(BroxbournePlanningScraper),
    "taunton deane": _legacy(TauntonDeanePlanningScraper),
    "camden": _legacy(CamdenPlanningScraper),
    "central bedfordshire": _legacy(CentralBedfordshirePlanningScraper),
    "tandridge": _legacy(TandridgePlanningScraper),
    "surrey": _atrium(SurreyPlanningScraper),
    "stratford on avon": _legacy(StratfordOnAvonPlanningScraper),
    "coventry": _legacy(CoventryPlanningScraper),
    "south oxfordshire": _atrium(SouthOxfordshirePlanningScraper),
    "crawley": _atrium(CrawleyPlanningScraper),
    "devon": _atrium(DevonPlanningScraper),
    "dorset": _legacy(DorsetPlanningScraper),
    "east sussex": _legacy(EastSussexPlanningScraper),
    "eastleigh": _legacy(EastleighPlanningScraper),
    "elmbridge": _legacy(ElmbridgePlanningScraper),
    "somerset": _atrium(SomersetPlanningScraper),
    "essex": _atrium(EssexPlanningScraper),
    "exmoor": _atrium(ExmoorPlanningScraper),
    "gloucestershire": _legacy(GloucestershirePlanningScraper),
    "shepway": _arcus(ShepwayPlanningScraper),
}


def authority_specific_scraper(authority: str, base_url: str) -> PlanningScraper | None:
    factory = AUTHORITY_ADAPTER_FACTORIES.get(authority.casefold())
    return factory(authority, base_url) if factory else None


__all__ = [
    "AUTHORITY_ADAPTER_FACTORIES",
    "AshfordPlanningScraper",
    "BarkingAndDagenhamPlanningScraper",
    "BcpPlanningScraper",
    "BromleyPlanningScraper",
    "BroxbournePlanningScraper",
    "CamdenPlanningScraper",
    "CentralBedfordshirePlanningScraper",
    "CoventryPlanningScraper",
    "CrawleyPlanningScraper",
    "DevonPlanningScraper",
    "DorsetPlanningScraper",
    "EastSussexPlanningScraper",
    "EastleighPlanningScraper",
    "ElmbridgePlanningScraper",
    "EssexPlanningScraper",
    "ExmoorPlanningScraper",
    "GloucestershirePlanningScraper",
    "ShepwayPlanningScraper",
    "SomersetPlanningScraper",
    "SouthOxfordshirePlanningScraper",
    "StratfordOnAvonPlanningScraper",
    "SurreyPlanningScraper",
    "TandridgePlanningScraper",
    "TauntonDeanePlanningScraper",
    "ValeOfWhiteHorsePlanningScraper",
    "WalthamForestPlanningScraper",
    "WelwynHatfieldPlanningScraper",
    "WestNorthamptonshirePlanningScraper",
    "WestSussexPlanningScraper",
    "WokinghamPlanningScraper",
    "WorcesterPlanningScraper",
    "WorcestershirePlanningScraper",
    "WychavonPlanningScraper",
    "authority_specific_scraper",
]
