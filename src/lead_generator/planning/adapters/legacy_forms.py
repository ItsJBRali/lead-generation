from __future__ import annotations

import re
import json
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, normalize_label, parse_council_date


@dataclass(frozen=True, slots=True)
class LegacyFormsCouncilConfig:
    authority: str
    base_url: str


LABEL_MAP = {
    "application_reference": "reference",
    "application_ref": "reference",
    "application_number": "reference",
    "reference_number": "reference",
    "app_no": "reference",
    "app_number": "reference",
    "case_reference": "reference",
    "site_location": "address",
    "site_address": "address",
    "location": "address",
    "location_details": "address",
    "address": "address",
    "proposed_development": "description",
    "proposal": "description",
    "description": "description",
    "development": "description",
    "received_date": "date_received",
    "date_received": "date_received",
    "registered": "date_received",
    "registered_date": "date_received",
    "valid_date": "date_validated",
    "date_valid": "date_validated",
    "decision": "decision",
    "decision_date": "decision_date",
    "status": "status",
    "ward": "ward",
    "parish": "parish",
}

REFERENCE_RE = re.compile(r"\b(?:[A-Z]{1,4}/)?\d{2,4}[/.-][A-Z0-9/.-]+\b", re.IGNORECASE)


class NativeListingScraper(PlanningScraper):
    family = "legacy"

    def __init__(
        self,
        config: LegacyFormsCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient()

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        applications = self.search(listing_url, start_date=start_date, end_date=end_date, limit=limit)
        return DiscoveryResult(authority=self.authority, source_url=listing_url, applications=applications)

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        raise NotImplementedError

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if not url:
            raise ValueError(f"{self.family} application fetch requires a URL")
        response = self.http.get(url)
        fields = extract_labelled_fields(html.fromstring(response.text))
        return application_from_fields(
            self.authority,
            self.family,
            fields,
            url=response.url,
            source_url=url,
            fallback_uid=uid,
            detail_complete=True,
        )

    def _form_defaults(self, form: html.HtmlElement) -> dict[str, str]:
        data: dict[str, str] = {}
        for input_node in form.xpath(".//input[@name]"):
            input_type = (input_node.get("type") or "text").lower()
            name = input_node.get("name")
            if not name or input_type in {"submit", "button", "image", "reset"}:
                continue
            if input_type in {"checkbox", "radio"} and input_node.get("checked") is None:
                continue
            data[name] = input_node.get("value") or ""
        for select in form.xpath(".//select[@name]"):
            options = select.xpath(".//option[@selected]") or select.xpath(".//option")[:1]
            if options:
                data[select.get("name")] = options[0].get("value") or clean_text(" ".join(options[0].itertext())) or ""
        for textarea in form.xpath(".//textarea[@name]"):
            data[textarea.get("name")] = clean_text(" ".join(textarea.itertext())) or ""
        return data

    def _absolute_action(self, page_url: str, form: html.HtmlElement) -> str:
        return urljoin(page_url, form.get("action") or page_url)


class TascomiPlanningScraper(NativeListingScraper):
    family = "tascomi"

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
        form = first(document.xpath("//form[.//input[@name='received_date_from'] or .//input[@name='valid_date_from']]"))
        if form is None:
            weekly_url = replace_query_action(response.url, "getReceivedWeeklyList")
            response = self.http.get(weekly_url)
        else:
            data = self._form_defaults(form)
            data["fa"] = data.get("fa") or "search"
            data["submitted"] = "true"
            if start_date:
                data["received_date_from"] = start_date.strftime("%d-%m-%Y")
            if end_date:
                data["received_date_to"] = end_date.strftime("%d-%m-%Y")
            response = self.http.post_form(self._absolute_action(response.url, form), data)
        applications = parse_header_tables(response.text, response.url, self.authority, self.family)
        return applications[:limit] if limit is not None else applications


class EnterpriseStorePlanningScraper(NativeListingScraper):
    family = "enterprisestore"
    browser_user_agent = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    )

    def __init__(
        self,
        config: LegacyFormsCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(
            config,
            http_client=http_client or CouncilHttpClient(user_agent=self.browser_user_agent),
        )

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
        form = first(document.xpath("//form[@id='frmOnlinePlanningSearch'] | //form[.//input[@name='SearchFor']]"))
        if form is None:
            return []
        data = self._form_defaults(form)
        result_path = data.get("urlOnlinePlanningSearchResult") or "/ES/Presentation/Planning/OnlinePlanning/OnlinePlanningSearchResults"
        data.update(
            {
                "SearchFor": "PlanningApplications",
                "StatusOptions": "CustomDateRange",
                "AnyStatus": "true",
                "Validated": "true",
                "SortOptions": "SortedByMostRecent",
            }
        )
        if start_date:
            data["FromDate"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data["ToDate"] = end_date.strftime("%d/%m/%Y")
        response = self.http.post_form(
            urljoin(response.url, result_path),
            data,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )
        applications = self.parse_cards(response.text, response.url)
        applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def parse_cards(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        grouped: dict[str, list[str]] = {}
        for anchor in document.xpath("//a[contains(@href, 'OnlinePlanningOverview')]"):
            href = anchor.get("href") or ""
            text = clean_text(" ".join(anchor.itertext()))
            if not text:
                continue
            grouped.setdefault(href, []).append(text)

        applications: list[PlanningApplication] = []
        for href, values in grouped.items():
            query = parse_qs(urlsplit(href).query)
            reference = first(query.get("applicationNumber")) or extract_reference(" ".join(values))
            if not reference:
                continue
            registered = None
            ref_line = first(value for value in values if "registered" in value.casefold())
            if ref_line:
                match = re.search(r"Registered\s*:\s*([^|]+)$", ref_line, re.IGNORECASE)
                registered = parse_council_date(match.group(1)) if match else None
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=urljoin(page_url, href),
                    reference=reference,
                    address=values[0] if values else None,
                    description=values[2] if len(values) > 2 else None,
                    date_received=registered,
                    postcode=extract_postcode(values[0] if values else None),
                    source_url=page_url,
                    raw={"portal_family": self.family, "detail_complete": True, "listing_text": " | ".join(values)},
                )
            )
        return applications


class AppSearchServPlanningScraper(NativeListingScraper):
    family = "appsearchserv"

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
        form = first(document.xpath("//form[@name='AppSearchForm'] | //form[.//input[@name='ValidDateFrom']]"))
        if form is None:
            return []
        data = self._form_defaults(form)
        if start_date:
            data["ReceivedDateFrom"] = start_date.strftime("%d/%m/%Y")
            data["ValidDateFrom"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data["ReceivedDateTo"] = end_date.strftime("%d/%m/%Y")
            data["ValidDateTo"] = end_date.strftime("%d/%m/%Y")
        for key, value in list(data.items()):
            if value.casefold() == "none":
                data[key] = ""
        data["button"] = data.get("button") or "Search"
        response = self.http.post_form(self._absolute_action(response.url, form), data)
        applications = parse_header_tables(response.text, response.url, self.authority, self.family)
        return applications[:limit] if limit is not None else applications


class FastwebPlanningScraper(NativeListingScraper):
    family = "fastweb"

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
        form = first(document.xpath("//form[@name='SearchForm'] | //form[.//input[@name='DateReceivedStart']]"))
        if form is None:
            return []
        data = self._form_defaults(form)
        if start_date:
            data["DateReceivedStart"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data["DateReceivedEnd"] = end_date.strftime("%d/%m/%Y")
        data["Submit"] = data.get("Submit") or "Search"
        response = self.http.post_form(self._absolute_action(response.url, form), data)

        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        next_url: str | None = response.url
        page_text = response.text
        page_url = response.url
        while next_url:
            page_apps = self.parse_results(page_text, page_url, seen)
            applications.extend(page_apps)
            if limit is not None and len(applications) >= limit:
                return applications[:limit]
            next_url = self.next_page(page_text, page_url)
            if not next_url:
                break
            response = self.http.get(next_url)
            page_text = response.text
            page_url = response.url
        return applications

    def parse_results(self, html_text: str, page_url: str, seen: set[str] | None = None) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        seen = seen if seen is not None else set()
        applications: list[PlanningApplication] = []
        for anchor in document.xpath("//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'detail.asp')]"):
            href = anchor.get("href") or ""
            query = parse_qs(urlsplit(href).query)
            container_text = nearest_table_text(anchor) or clean_text(" ".join(anchor.xpath("ancestor::*[1]//text()")))
            fields = labelled_values_from_text(container_text or "")
            reference = first(query.get("AltRef")) or fields.get("App. No.") or extract_reference(href)
            if not reference:
                continue
            reference = reference.strip()
            fields.setdefault("App. No.", reference)
            app = application_from_fields(
                self.authority,
                self.family,
                fields,
                url=urljoin(page_url, href),
                source_url=page_url,
                fallback_uid=reference,
                detail_complete=True,
                listing_text=container_text,
            )
            seen_key = (app.reference or app.uid).casefold().strip()
            if seen_key in seen:
                continue
            seen.add(seen_key)
            applications.append(
                app
            )
        return applications

    def next_page(self, html_text: str, page_url: str) -> str | None:
        document = html.fromstring(html_text)
        for anchor in document.xpath("//a[@href]"):
            text = clean_text(" ".join(anchor.itertext())) or ""
            if "next" in text.casefold():
                return urljoin(page_url, anchor.get("href"))
        return None


class CcedPlanningScraper(NativeListingScraper):
    family = "cced"

    def search(
        self,
        listing_url: str,
        *,
        start_date: date | None,
        end_date: date | None,
        limit: int | None,
    ) -> list[PlanningApplication]:
        response = self.http.get(listing_url)
        response = self._accept_disclaimer(response)
        document = html.fromstring(response.text)
        form = first(document.xpath("//form[@id='aspnetForm'] | //form"))
        if form is None:
            return []
        data = self._form_defaults(form)
        if start_date:
            set_telerik_date(data, "txtDateReceivedFrom", start_date)
        if end_date:
            set_telerik_date(data, "txtDateReceivedTo", end_date)
        submit_name = first(
            node.get("name")
            for node in form.xpath(".//input[@type='submit' and @name]")
            if "btnSearch3" in (node.get("name") or "")
        ) or "ctl00$ContentPlaceHolder1$btnSearch3"
        data[submit_name] = "Search"
        response = self.http.post_form(self._absolute_action(response.url, form), data)
        applications = []
        seen: set[str] = set()
        while True:
            for application in self.parse_results(response.text, response.url):
                key = (application.reference or application.uid).casefold()
                if key in seen:
                    continue
                seen.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return applications[:limit]
            next_target = self.next_page_target(response.text)
            if not next_target:
                break
            response = self.post_results_page(response.text, response.url, next_target)
        if start_date or end_date:
            applications = filter_by_date(applications, start_date, end_date)
        return applications[:limit] if limit is not None else applications

    def _accept_disclaimer(self, response):
        if "disclaimer" not in response.url.casefold() and "btnAccept" not in response.text:
            return response
        document = html.fromstring(response.text)
        form = first(document.xpath("//form"))
        if form is None:
            return response
        data = self._form_defaults(form)
        submit = first(node.get("name") for node in form.xpath(".//input[@type='submit' and @name]"))
        if submit:
            data[submit] = "Accept"
        return self.http.post_form(self._absolute_action(response.url, form), data)

    def parse_results(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        body_text = clean_text(" ".join(document.xpath("//body//text()"))) or ""
        pattern = re.compile(
            r"(?P<ref>P/[A-Z]+/\d{4}/\d+)\s+Location:\s*(?P<address>.*?)\s+Proposal:\s*(?P<proposal>.*?)\s+Decision:\s*(?P<decision>.*?)\s+Decision Date:\s*(?P<decision_date>.*?)(?:View this application|$)",
            re.IGNORECASE,
        )
        for match in pattern.finditer(body_text):
            reference = clean_text(match.group("ref"))
            if not reference or reference in seen:
                continue
            seen.add(reference)
            detail_url = urljoin(page_url, f"applicationdetails.aspx?ref={reference}")
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=detail_url,
                    reference=reference,
                    address=clean_text(match.group("address")),
                    description=clean_text(match.group("proposal")),
                    decision=clean_text(match.group("decision")),
                    postcode=extract_postcode(match.group("address")),
                    source_url=page_url,
                    raw={"portal_family": self.family, "detail_complete": True, "listing_text": match.group(0)},
                )
            )
        return applications

    def next_page_target(self, html_text: str) -> str | None:
        document = html.fromstring(html_text)
        body_text = clean_text(" ".join(document.xpath("//body//text()"))) or ""
        page_match = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", body_text, re.IGNORECASE)
        if not page_match:
            return None
        current_page = int(page_match.group(1))
        final_page = int(page_match.group(2))
        if current_page >= final_page:
            return None
        wanted_text = str(current_page + 1)
        fallback_target: str | None = None
        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            target_match = re.search(r"__doPostBack\('([^']+)'", href)
            if not target_match:
                continue
            text = clean_text(" ".join(anchor.itertext())) or ""
            if text == wanted_text:
                return target_match.group(1)
            if text == "..." and fallback_target is None:
                fallback_target = target_match.group(1)
        return fallback_target

    def post_results_page(self, html_text: str, page_url: str, event_target: str):
        document = html.fromstring(html_text)
        form = first(document.xpath("//form"))
        if form is None:
            raise ValueError("CCED result page did not contain a paging form")
        data = self._form_defaults(form)
        data["__EVENTTARGET"] = event_target
        data["__EVENTARGUMENT"] = ""
        return self.http.post_form(self._absolute_action(page_url, form), data)


class AstunPlanningScraper(NativeListingScraper):
    family = "astun"

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
        form = first(document.xpath("//form[.//input[@name='template'] and .//input[@name='requestType']] | //form"))
        if form is None:
            return []
        data = self._form_defaults(form)
        if start_date:
            data["DATEAPRECV:FROM:DATE"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data["DATEAPRECV:TO:DATE"] = end_date.strftime("%d/%m/%Y")
        action = self._absolute_action(response.url, form)
        method = (form.get("method") or "get").lower()
        response = self.http.post_form(action, data) if method == "post" else self.http.get(action, data)
        applications = parse_header_tables(response.text, response.url, self.authority, self.family)
        if not applications:
            applications = self.parse_text_results(response.text, response.url)
        return applications[:limit] if limit is not None else applications

    def parse_text_results(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        body_text = clean_text(" ".join(html.fromstring(html_text).xpath("//body//text()"))) or ""
        pattern = re.compile(
            r"(?P<ref>\d{2}/\d{5}/[A-Z]+|[A-Z]+/\d{2}/\d{4,5})\s+(?:Location|Address):\s*(?P<address>.*?)\s+(?:Proposal|Description):\s*(?P<proposal>.*?)(?=\s+\d{2}/\d{5}/[A-Z]+|\s+[A-Z]+/\d{2}/\d{4,5}|$)",
            re.IGNORECASE,
        )
        applications: list[PlanningApplication] = []
        for match in pattern.finditer(body_text):
            reference = clean_text(match.group("ref"))
            if not reference:
                continue
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=reference,
                    url=page_url,
                    reference=reference,
                    address=clean_text(match.group("address")),
                    description=clean_text(match.group("proposal")),
                    postcode=extract_postcode(match.group("address")),
                    source_url=page_url,
                    raw={"portal_family": self.family, "detail_complete": True, "listing_text": match.group(0)},
                )
            )
        return applications


def parse_header_tables(html_text: str, page_url: str, authority: str, family: str) -> list[PlanningApplication]:
    document = html.fromstring(html_text)
    applications: list[PlanningApplication] = []
    seen: set[str] = set()
    for table in document.xpath("//table"):
        header_cells = table.xpath(".//tr[th]/*[self::th or self::td]")
        if not header_cells:
            first_row = first(table.xpath(".//tr[1]"))
            header_cells = first_row.xpath("./*[self::th or self::td]") if first_row is not None else []
        headers = [normalize_label(clean_text(" ".join(cell.itertext())) or "") for cell in header_cells]
        if not any(header in LABEL_MAP for header in headers):
            continue
        for row in table.xpath(".//tr[position() > 1]"):
            cells = row.xpath("./*[self::td or self::th]")
            if len(cells) < 2:
                continue
            fields: dict[str, str] = {}
            for index, cell in enumerate(cells):
                if index >= len(headers):
                    continue
                label = headers[index]
                value = clean_text(" ".join(cell.itertext()))
                if label and value:
                    fields[label] = value
            href = first(anchor.get("href") for anchor in row.xpath(".//a[@href]"))
            app = application_from_fields(
                authority,
                family,
                fields,
                url=urljoin(page_url, href) if href else page_url,
                source_url=page_url,
                fallback_uid=extract_reference(clean_text(" ".join(row.itertext()))),
                detail_complete=True,
                listing_text=clean_text(" ".join(row.itertext())),
            )
            if app.reference and app.reference not in seen:
                seen.add(app.reference)
                applications.append(app)
    return applications


def application_from_fields(
    authority: str,
    family: str,
    fields: dict[str, str],
    *,
    url: str,
    source_url: str,
    fallback_uid: str | None = None,
    detail_complete: bool = False,
    listing_text: str | None = None,
) -> PlanningApplication:
    mapped: dict[str, str] = {}
    for label, value in fields.items():
        key = LABEL_MAP.get(normalize_label(label))
        if key and value and key not in mapped:
            mapped[key] = value
    for date_field in ("date_received", "date_validated"):
        if mapped.get(date_field):
            mapped[date_field] = parse_council_date(mapped[date_field]) or mapped[date_field]
    reference = mapped.get("reference") or fallback_uid or extract_reference(listing_text) or extract_reference(" ".join(fields.values()))
    uid = reference or fallback_uid or url
    address = mapped.get("address")
    return PlanningApplication(
        authority=authority,
        uid=uid,
        url=url,
        reference=reference,
        address=address,
        description=mapped.get("description"),
        status=mapped.get("status"),
        decision=mapped.get("decision"),
        date_received=mapped.get("date_received"),
        date_validated=mapped.get("date_validated"),
        ward=mapped.get("ward"),
        parish=mapped.get("parish"),
        postcode=extract_postcode(address),
        source_url=source_url,
        raw={
            "portal_family": family,
            "detail_complete": detail_complete,
            "listing_text": listing_text,
            **fields,
        },
    )


def extract_labelled_fields(document: html.HtmlElement) -> dict[str, str]:
    fields: dict[str, str] = {}
    for row in document.xpath("//tr[th and td] | //tr[count(td) >= 2]"):
        cells = row.xpath("./*[self::th or self::td]")
        if len(cells) < 2:
            continue
        label = clean_text(" ".join(cells[0].itertext()))
        value = clean_text(" ".join(cells[1].itertext()))
        if label and value:
            fields[label] = value
    for term in document.xpath("//dt"):
        sibling = term.getnext()
        if sibling is not None and sibling.tag.lower() == "dd":
            label = clean_text(" ".join(term.itertext()))
            value = clean_text(" ".join(sibling.itertext()))
            if label and value:
                fields[label] = value
    return fields


def labelled_values_from_text(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    labels = ("App. No.", "Site Address:", "Description:", "Received Date:", "Decision Sent Date:", "Decision Date:")
    for index, label in enumerate(labels):
        marker = re.escape(label) + r"\s*:?"
        next_labels = "|".join(re.escape(next_label) for next_label in labels[index + 1 :])
        pattern = rf"{marker}\s*(.*?)(?={next_labels}|$)" if next_labels else rf"{marker}\s*(.*?)$"
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            fields[label.rstrip(":")] = clean_text(match.group(1)) or ""
    return fields


def nearest_table_text(anchor: html.HtmlElement) -> str | None:
    tables = anchor.xpath("ancestor::table")
    for table in tables:
        text = clean_text(" ".join(table.itertext()))
        if text and "App. No." in text:
            return text
    return None


def first(values):
    for value in values:
        if value is not None and value != "":
            return value
    return None


def set_suffix(data: dict[str, str], suffix: str, value: str) -> None:
    for key in list(data):
        if key.endswith(suffix):
            data[key] = value


def set_telerik_date(data: dict[str, str], field: str, value: date) -> None:
    display = value.strftime("%d/%m/%Y")
    iso_value = value.isoformat()
    set_suffix(data, f"${field}", display)
    set_suffix(data, f"${field}$dateInput", display)
    data[f"ctl00_ContentPlaceHolder1_{field}_dateInput_ClientState"] = json.dumps(
        {
            "enabled": True,
            "emptyMessage": "",
            "validationText": f"{iso_value}-00-00-00",
            "valueAsString": f"{iso_value}-00-00-00",
            "minDateStr": "1980-01-01-00-00-00",
            "maxDateStr": "2099-12-31-00-00-00",
            "lastSetTextBoxValue": display,
        },
        separators=(",", ":"),
    )
    data[f"ctl00_ContentPlaceHolder1_{field}_calendar_SD"] = json.dumps(
        [[value.year, value.month, value.day]],
        separators=(",", ":"),
    )
    data[f"ctl00_ContentPlaceHolder1_{field}_ClientState"] = json.dumps(
        {
            "minDateStr": "1980-01-01-00-00-00",
            "maxDateStr": "2099-12-31-00-00-00",
        },
        separators=(",", ":"),
    )


def extract_reference(text: str | None) -> str | None:
    if not text:
        return None
    match = REFERENCE_RE.search(text)
    return match.group(0) if match else None


def replace_query_action(url: str, action: str) -> str:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    query["fa"] = [action]
    return parts._replace(query=urlencode(query, doseq=True)).geturl()


def filter_by_date(
    applications: list[PlanningApplication],
    start_date: date | None,
    end_date: date | None,
) -> list[PlanningApplication]:
    if not start_date and not end_date:
        return applications
    filtered: list[PlanningApplication] = []
    for application in applications:
        value = application.date_received or application.date_validated
        if not value:
            filtered.append(application)
            continue
        try:
            parsed = date.fromisoformat(value)
        except ValueError:
            filtered.append(application)
            continue
        if start_date and parsed < start_date:
            continue
        if end_date and parsed > end_date:
            continue
        filtered.append(application)
    return filtered
