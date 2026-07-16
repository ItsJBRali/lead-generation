from __future__ import annotations

import base64
import html as stdlib_html
import json
import re
import struct
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

from lxml import html

from lead_generator.planning.adapters.arcus import ArcusPlanningScraper
from lead_generator.planning.adapters.legacy_forms import (
    LegacyFormsCouncilConfig,
    NativeListingScraper,
    filter_by_date,
)
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient
from lead_generator.planning.models import PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


class BathPlanningScraper(NativeListingScraper):
    """Bath and North East Somerset's public PlanningAPI register."""

    family = "bath_planning_api"
    api_url = "https://api.bathnes.gov.uk/webapi/api/PlanningAPI/v2/planningdata/search/"

    def __init__(
        self,
        config: LegacyFormsCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config, http_client=http_client or CouncilHttpClient(verify_tls=False, retries=5))

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        page = self.http.get(listing_url)
        payloads: list[dict[str, str]] = []
        if start_date and end_date:
            payloads.extend(
                (
                    {
                        "application_validated_from": start_date.isoformat(),
                        "application_validated_to": end_date.isoformat(),
                    },
                    {
                        "application_isharedate_from": start_date.isoformat(),
                        "application_isharedate_to": end_date.isoformat(),
                    },
                )
            )
        else:
            payloads.append({})

        records: list[dict[str, Any]] = []
        seen: set[str] = set()
        for payload in payloads:
            response = self.http.post_json(self.api_url, payload)
            try:
                result = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise CouncilFetchError("Bath planning API returned invalid JSON") from exc
            if not isinstance(result, list):
                raise CouncilFetchError("Bath planning API returned an unexpected response")
            for record in result:
                if not isinstance(record, dict):
                    continue
                reference = clean_text(str(record.get("refval") or ""))
                if reference and reference.casefold() not in seen:
                    seen.add(reference.casefold())
                    records.append(record)

        applications = [self._application_from_record(record, page.url) for record in records]
        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def _application_from_record(self, record: dict[str, Any], listing_url: str) -> PlanningApplication:
        reference = clean_text(str(record.get("refval") or ""))
        if not reference:
            raise CouncilFetchError("Bath planning API returned a record without a reference")
        address = clean_text(str(record.get("addressline") or record.get("address") or ""))
        detail_url = urljoin(listing_url, f"details.html?{urlencode({'refval': reference})}")
        return PlanningApplication(
            authority=self.authority,
            uid=reference,
            url=detail_url,
            reference=reference,
            address=address,
            description=clean_text(str(record.get("proposal") or "")),
            status=clean_text(str(record.get("dcstat_text") or record.get("apstat_text") or "")),
            date_received=parse_council_date(str(record.get("dateaprecv") or "")),
            date_validated=parse_council_date(str(record.get("dateapval") or "")),
            ward=clean_text(str(record.get("ward_text") or "")),
            parish=clean_text(str(record.get("parish_text") or "")),
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": self.family,
                "api": "bath_planning_api_v2",
                "detail_complete": True,
                "date_range_filtered": True,
                "record": record,
            },
        )


class ColchesterPlanningScraper(NativeListingScraper):
    """Colchester's Microsoft Power Pages planning register."""

    family = "power_pages"
    page_size = 25

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        page = self.http.get(listing_url)
        document = html.fromstring(page.text)
        grids = document.xpath("//*[@data-get-url and @data-view-layouts]")
        if not grids:
            raise CouncilFetchError("Could not find Colchester's planning result grid")
        grid = grids[0]
        endpoint = urljoin(page.url, grid.get("data-get-url") or "")
        secure_configuration = self._secure_configuration(grid.get("data-view-layouts") or "")
        token_page = self.http.get(urljoin(page.url, "/_layout/tokenhtml"), headers={"Referer": page.url})
        token_document = html.fromstring(token_page.text)
        tokens = token_document.xpath("//input[@name='__RequestVerificationToken']/@value")
        if not tokens:
            raise CouncilFetchError("Could not obtain Colchester's request-verification token")

        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        paging_cookie = ""
        for page_number in range(1, 251):
            payload = {
                "base64SecureConfiguration": secure_configuration,
                "sortExpression": "new_registration_date DESC,new_concatenatedaddress ASC",
                "search": "",
                "page": page_number,
                "pageSize": self.page_size,
                "pagingCookie": paging_cookie,
                "filter": None,
                "metaFilter": None,
                "nlSearchFilter": "",
                "timezoneOffset": -60,
                "customParameters": [],
            }
            response = self.http.post_json(
                endpoint,
                payload,
                headers={
                    "Accept": "application/json, text/javascript, */*; q=0.01",
                    "Content-Type": "application/json; charset=UTF-8",
                    "X-Requested-With": "XMLHttpRequest",
                    "__RequestVerificationToken": tokens[0],
                    "Referer": page.url,
                },
            )
            try:
                result = json.loads(response.text)
            except json.JSONDecodeError as exc:
                raise CouncilFetchError("Colchester's planning grid returned invalid JSON") from exc
            records = result.get("Records") if isinstance(result, dict) else None
            if not isinstance(records, list):
                raise CouncilFetchError("Colchester's planning grid returned an unexpected response")

            page_dates: list[date] = []
            for record in records:
                if not isinstance(record, dict):
                    continue
                application = self._application_from_record(record, page.url)
                parsed = self._iso_date(application.date_received)
                if parsed:
                    page_dates.append(parsed)
                if parsed and end_date and parsed > end_date:
                    continue
                if parsed and start_date and parsed < start_date:
                    continue
                key = (application.reference or application.uid).casefold()
                if key in seen:
                    continue
                seen.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return applications

            if start_date and page_dates and min(page_dates) < start_date:
                break
            if not result.get("MoreRecords") or not records:
                break
            paging_cookie = str(result.get("NextPagePagingCookie") or "")
        return applications

    def _secure_configuration(self, encoded_layouts: str) -> str:
        try:
            encoded = stdlib_html.unescape(encoded_layouts)
            encoded += "=" * (-len(encoded) % 4)
            layouts = json.loads(base64.b64decode(encoded).decode("utf-8"))
            layout = layouts[0] if isinstance(layouts, list) else layouts
            configuration = layout.get("Base64SecureConfiguration") if isinstance(layout, dict) else None
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise CouncilFetchError("Could not decode Colchester's planning grid configuration") from exc
        if not configuration:
            raise CouncilFetchError("Colchester's planning grid configuration is missing")
        return str(configuration)

    def _application_from_record(self, record: dict[str, Any], listing_url: str) -> PlanningApplication:
        values: dict[str, Any] = {}
        for attribute in record.get("Attributes") or []:
            if not isinstance(attribute, dict) or not attribute.get("Name"):
                continue
            values[str(attribute["Name"])] = attribute.get("DisplayValue") or attribute.get("FormattedValue") or attribute.get("Value")
        uid = clean_text(str(record.get("Id") or values.get("new_wamplanningid") or ""))
        reference = clean_text(str(values.get("new_name") or ""))
        if not uid or not reference:
            raise CouncilFetchError("Colchester's planning grid returned an incomplete record")
        address = clean_text(str(values.get("new_concatenatedaddress") or ""))
        received = parse_council_date(str(values.get("new_registration_date") or ""))
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=urljoin(listing_url, f"/planning-app-details/?id={quote(uid)}"),
            reference=reference,
            address=address,
            description=clean_text(str(values.get("new_development_desc") or "")),
            status=clean_text(str(values.get("new_application_status") or "")),
            date_received=received,
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": self.family,
                "api": "power_pages_entity_grid",
                "detail_complete": True,
                "date_range_filtered": True,
                "record_id": uid,
            },
        )

    def _iso_date(self, value: str | None) -> date | None:
        if not value:
            return None
        try:
            return date.fromisoformat(value)
        except ValueError:
            return None


class TelfordPlanningScraper(NativeListingScraper):
    """Telford's ASP.NET register, searched in one-day windows to avoid its ten-row cap."""

    family = "telford_webforms"

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        windows: list[date | None]
        if start_date and end_date:
            windows = [start_date + timedelta(days=offset) for offset in range((end_date - start_date).days + 1)]
        else:
            windows = [start_date or end_date]
        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        for search_date in windows:
            for application in self._search_day(listing_url, search_date):
                key = (application.reference or application.uid).casefold()
                if key in seen:
                    continue
                seen.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return applications
        return applications

    def _search_day(self, listing_url: str, search_date: date | None) -> list[PlanningApplication]:
        page = self.http.get(listing_url)
        document = html.fromstring(page.text)
        forms = document.xpath("//form[.//input[contains(@name,'DCdatefrom')]]")
        if not forms:
            raise CouncilFetchError("Could not find Telford's planning search form")
        data = self._form_defaults(forms[0])
        if search_date:
            value = search_date.strftime("%d-%m-%Y")
            data["ctl00$ContentPlaceHolder1$DCdatefrom"] = value
            data["ctl00$ContentPlaceHolder1$DCdateto"] = value
        data["ctl00$ContentPlaceHolder1$btnSearchPlanningDetails"] = "Search"
        result = self.http.post_form(urljoin(page.url, "default.aspx"), data, headers={"Referer": page.url})
        result_document = html.fromstring(result.text)
        applications: list[PlanningApplication] = []
        for anchor in result_document.xpath("//a[contains(translate(@href,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'pa-applicationsummary.aspx')]"):
            rows = anchor.xpath("ancestor::tr[1]")
            if not rows:
                continue
            cells = rows[0].xpath("./*[self::td or self::th]")
            reference = clean_text(" ".join(anchor.itertext()))
            if not reference:
                continue
            valid_date = parse_council_date(clean_text(" ".join(cells[1].itertext())) if len(cells) > 1 else None)
            address = clean_text(" ".join(cells[2].itertext())) if len(cells) > 2 else None
            description = clean_text(" ".join(cells[3].itertext())) if len(cells) > 3 else None
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=urljoin(result.url, anchor.get("href") or ""),
                    reference=reference,
                    address=address,
                    description=description,
                    date_validated=valid_date,
                    postcode=extract_postcode(address),
                    source_url=result.url,
                    raw={
                        "portal_family": self.family,
                        "detail_complete": True,
                        "date_range_filtered": True,
                    },
                )
            )
        return applications


class WestDunbartonshirePlanningScraper(NativeListingScraper):
    """West Dunbartonshire's classic ASP public register."""

    family = "west_dunbartonshire_asp"

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        page = self.http.get(listing_url)
        document = html.fromstring(page.text)
        forms = document.xpath("//form[contains(translate(@action,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'dcdisplayinitial.asp')]")
        if not forms:
            raise CouncilFetchError("Could not find West Dunbartonshire's planning search form")
        data = self._form_defaults(forms[0])
        if start_date:
            data["vDateRcvFr"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data["vDateRcvTo"] = end_date.strftime("%d/%m/%Y")
        action = self._absolute_action(page.url, forms[0])
        result = self.http.get(action, data)
        result_document = html.fromstring(result.text)
        applications: list[PlanningApplication] = []
        for form in result_document.xpath("//form[.//input[@name='vUPRN']]"):
            references = form.xpath(".//input[@name='vUPRN']/@value")
            if not references:
                continue
            reference = clean_text(references[0])
            if not reference:
                continue
            rows = form.xpath("ancestor::tr[1]")
            cells = rows[0].xpath("./*[self::td or self::th]") if rows else []
            address = clean_text(" ".join(cells[0].itertext())) if cells else None
            detail_data = self._form_defaults(form)
            detail_url = f"{self._absolute_action(result.url, form)}?{urlencode(detail_data)}"
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=detail_url,
                    reference=reference,
                    address=address,
                    postcode=extract_postcode(address),
                    source_url=result.url,
                    raw={"portal_family": self.family, "detail_complete": False},
                )
            )
            if limit is not None and len(applications) >= limit:
                break
        return applications


class _RbkcBinaryReader:
    _DOTNET_EPOCH_TICKS = 621355968000000000
    _TICKS_MASK = 0x3FFFFFFFFFFFFFFF
    _LONDON = ZoneInfo("Europe/London")

    def __init__(self, body: bytes) -> None:
        self._body = memoryview(body)
        self._index = 0

    @property
    def remaining(self) -> int:
        return len(self._body) - self._index

    def read_byte(self) -> int:
        return self._unpack("<B", 1)

    def read_uint32(self) -> int:
        return self._unpack("<I", 4)

    def read_uint64(self) -> int:
        return self._unpack("<Q", 8)

    def read_float64(self) -> float:
        return self._unpack("<d", 8)

    def read_string(self) -> str:
        length = self.read_uint32()
        if length > self.remaining:
            raise ValueError("String length exceeds the remaining response")
        start = self._index
        self._index += length
        return self._body[start : start + length].tobytes().decode("utf-8")

    def read_date(self) -> str | None:
        ticks = self.read_uint64() & self._TICKS_MASK
        if ticks < self._DOTNET_EPOCH_TICKS:
            return None
        milliseconds = (ticks - self._DOTNET_EPOCH_TICKS) // 10_000
        value = datetime.fromtimestamp(milliseconds / 1000, tz=self._LONDON)
        return value.date().isoformat()

    def _unpack(self, format_string: str, size: int):
        if size > self.remaining:
            raise ValueError("Planning response ended unexpectedly")
        value = struct.unpack_from(format_string, self._body, self._index)[0]
        self._index += size
        return value


class KensingtonPlanningScraper(NativeListingScraper):
    """Kensington and Chelsea's bespoke binary planning-search API."""

    family = "rbkc_planning_api"
    api_path = "/planningsearch/api/cases/everywhere"
    documents_url = "https://planningsearch.rbkc.gov.uk/publisher/mvc/listDocuments"
    london_timezone = ZoneInfo("Europe/London")

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        page = self.http.get(listing_url)
        page_text = (clean_text(" ".join(html.fromstring(page.text).itertext())) or "").casefold()
        if "cybersecurity issue" in page_text or "cyber recovery" in page_text:
            raise CouncilFetchError(
                "Kensington and Chelsea's planning register is unavailable during the council's cyber recovery"
            )
        if "rbkc planning portal" not in page_text and "/_build/" not in page.text:
            raise CouncilFetchError("Kensington and Chelsea's planning register returned an unexpected page")

        params = {"sort": "1"}
        if start_date:
            params["dateFrom"] = str(self._date_milliseconds(start_date))
        if end_date:
            params["dateTo"] = str(self._date_milliseconds(end_date + timedelta(days=1)) - 1)
        response = self.http.get_bytes(
            urljoin(page.url, self.api_path),
            params,
            headers={
                "Accept": "application/octet-stream",
                "Referer": page.url,
            },
        )
        if response.body.lstrip().startswith((b"<", b"{")):
            raise CouncilFetchError("Kensington and Chelsea's planning API returned an unexpected response")

        records = self._decode_records(response.body)
        applications: list[PlanningApplication] = []
        for record in records:
            reference = clean_text(record["case_reference"])
            uid = clean_text(record["case_reference_id"])
            if not reference or not uid or record["is_enforcement"]:
                continue
            address = clean_text(record["address"])
            detail_url = urljoin(page.url, f"/planningsearch/cases/{quote(reference, safe='/')}")
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=detail_url,
                    reference=reference,
                    address=address,
                    description=clean_text(record["description_short"]),
                    status=(
                        clean_text(record["current_stage_status_name_long"])
                        or clean_text(record["current_stage_status_name"])
                        or clean_text(record["current_planning_state"])
                    ),
                    date_received=record["date_registered"],
                    postcode=extract_postcode(address),
                    source_url=response.url,
                    raw={
                        "portal_family": self.family,
                        "detail_complete": True,
                        "date_range_filtered": bool(start_date or end_date),
                        "docs_url": f"{self.documents_url}?{urlencode({'identifier': 'Planning', 'ref': reference})}",
                        "record": record,
                    },
                )
            )

        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def _date_milliseconds(self, value: date) -> int:
        local_midnight = datetime.combine(value, time.min, tzinfo=self.london_timezone)
        return int(local_midnight.timestamp() * 1000)

    def _decode_records(self, body: bytes) -> list[dict[str, Any]]:
        reader = _RbkcBinaryReader(body)
        try:
            count = reader.read_uint32()
            if count > 100_000:
                raise ValueError("Planning response contains an invalid record count")
            records = [self._decode_record(reader) for _ in range(count)]
            if reader.remaining:
                raise ValueError("Planning response contains unexpected trailing data")
            return records
        except (UnicodeDecodeError, ValueError, OSError, OverflowError) as exc:
            raise CouncilFetchError(
                "Kensington and Chelsea's planning API returned an unreadable response"
            ) from exc

    def _decode_record(self, reader: _RbkcBinaryReader) -> dict[str, Any]:
        record: dict[str, Any] = {
            "is_enforcement": bool(reader.read_byte()),
            "is_direct_match": bool(reader.read_byte()),
            "is_related_match": bool(reader.read_byte()),
            "distance_metres": reader.read_uint32(),
            "local_uprn_point_id": reader.read_uint32(),
            "latest_date": reader.read_date(),
            "date_registered": reader.read_date(),
            "current_stage_date": reader.read_date(),
            "latitude": reader.read_float64(),
            "longitude": reader.read_float64(),
        }
        for key in (
            "uprn",
            "address",
            "case_type",
            "case_reference",
            "description_short",
            "case_reference_id",
            "current_stage_type",
            "current_planning_state",
            "current_stage_status_name",
            "current_stage_date_heading",
            "current_stage_status_name_long",
        ):
            record[key] = reader.read_string()
        return record


class CarmarthenshirePlanningScraper(ArcusPlanningScraper):
    """Carmarthenshire's custom Arcus/Salesforce Planning Register."""

    def _fetch_search_records(
        self,
        listing_url: str,
        context: dict[str, object],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        criteria: dict[str, str] = {}
        if start_date:
            criteria["arcusbuiltenv__Registration_Date__c:from"] = start_date.isoformat()
        if end_date:
            criteria["arcusbuiltenv__Registration_Date__c:to"] = end_date.isoformat()
        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "apex://PR_SearchCont/ACTION$query",
                    "callingDescriptor": "markup://c:PR_Search",
                    "params": {
                        "searchable_resources": "be_searchables_CARM",
                        "resource_name": "be_adv_categories_CARM",
                        "category_name": "PApplication",
                        "search_criteria": criteria,
                    },
                }
            ]
        }
        path_prefix = self._path_prefix(urlsplit(listing_url).path)
        response = self.http.post_form(
            self._aura_endpoint(listing_url),
            {
                "message": json.dumps(message, separators=(",", ":")),
                "aura.context": json.dumps(context, separators=(",", ":")),
                "aura.pageURI": f"{path_prefix}/s/pr-search-results",
                "aura.token": "null",
            },
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError("Carmarthenshire's planning register returned invalid JSON") from exc
        records: list[dict[str, Any]] = []
        for action in payload.get("actions", []):
            return_value = action.get("returnValue") if isinstance(action, dict) else None
            if isinstance(return_value, str):
                try:
                    return_value = json.loads(return_value)
                except json.JSONDecodeError:
                    continue
            if isinstance(return_value, dict) and isinstance(return_value.get("records"), list):
                records.extend(record for record in return_value["records"] if isinstance(record, dict))
        return records, False

    def _application_from_record(
        self,
        record: dict[str, Any],
        listing_url: str,
        *,
        fallback_date: date | None = None,
    ) -> PlanningApplication:
        uid = clean_text(str(record.get("Id") or ""))
        reference = clean_text(str(record.get("Name") or ""))
        if not uid or not reference:
            raise CouncilFetchError("Carmarthenshire's planning register returned an incomplete record")
        address = clean_text(str(record.get("arcusbuiltenv__Site_Address__c") or ""))
        parts = urlsplit(listing_url)
        path_prefix = self._path_prefix(parts.path)
        detail_url = urlunsplit((parts.scheme, parts.netloc, f"{path_prefix}/s/planning-application/{uid}", "", ""))
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=detail_url,
            reference=reference,
            address=address,
            description=clean_text(str(record.get("Hidden_Proposal__c") or "")),
            status=clean_text(str(record.get("arcusbuiltenv__Status__c") or "")),
            date_received=parse_council_date(str(record.get("arcusbuiltenv__Registration_Date__c") or "")),
            ward=clean_text(str(record.get("arcusbuiltenv__Wards__c") or "")),
            parish=clean_text(str(record.get("arcusbuiltenv__Parishes__c") or "")),
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": "arcus",
                "api": "carmarthenshire_pr_search",
                "detail_complete": True,
                "date_range_filtered": True,
                "docs_url": record.get("Documents_URL__c"),
                "record": record,
            },
        )


__all__ = [
    "BathPlanningScraper",
    "CarmarthenshirePlanningScraper",
    "ColchesterPlanningScraper",
    "KensingtonPlanningScraper",
    "TelfordPlanningScraper",
    "WestDunbartonshirePlanningScraper",
]
