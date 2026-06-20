from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilHttpClient, FetchResponse
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import (
    clean_text,
    extract_postcode,
    normalize_label,
    parse_council_date,
)


@dataclass(frozen=True, slots=True)
class OcellaCouncilConfig:
    authority: str
    base_url: str
    uid_query_params: tuple[str, ...] = (
        "id",
        "appNo",
        "appno",
        "reference",
        "ref",
        "caseNo",
        "case",
        "application",
    )


class OcellaPlanningScraper(PlanningScraper):
    """Scraper for Ocella-style planning registers with labelled HTML pages."""

    _label_map = {
        "reference": "reference",
        "application_reference": "reference",
        "application_number": "reference",
        "application_no": "reference",
        "case_no": "reference",
        "site_address": "address",
        "address": "address",
        "location": "address",
        "proposal": "description",
        "description": "description",
        "development_description": "description",
        "status": "status",
        "application_status": "status",
        "decision": "decision",
        "date_received": "date_received",
        "received_date": "date_received",
        "received": "date_received",
        "accepted": "date_validated",
        "valid_date": "date_validated",
        "date_validated": "date_validated",
        "validated": "date_validated",
        "applicant": "applicant_name",
        "applicant_name": "applicant_name",
        "agent": "agent_name",
        "agent_name": "agent_name",
        "case_officer": "case_officer",
        "ward": "ward",
        "parish": "parish",
    }

    def __init__(
        self,
        config: OcellaCouncilConfig,
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
        response = self._fetch_listing(listing_url, start_date=start_date, end_date=end_date)
        applications = self.parse_listing(response.text, response.url)
        if start_date:
            for application in applications:
                if not (application.date_received or application.date_validated):
                    application.date_received = start_date.isoformat()
                    application.raw = {
                        **application.raw,
                        "date_range_filtered": True,
                        "date_inferred_from_search_window": True,
                    }
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(
            authority=self.authority,
            source_url=response.url,
            applications=applications,
        )

    def _fetch_listing(
        self,
        listing_url: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ):
        response = self.http.get(listing_url)
        if not (start_date or end_date):
            return response
        document = html.fromstring(response.text)
        forms = document.xpath("//form[.//input[@name='receivedFrom'] or .//input[@name='receivedTo']]")
        if not forms:
            return self._fetch_aspnet_keyword_listing(
                response,
                document,
                start_date=start_date,
                end_date=end_date,
            )
        form = forms[0]
        data = self._form_defaults(form)
        if start_date:
            data["receivedFrom"] = start_date.strftime("%d-%m-%y")
        if end_date:
            data["receivedTo"] = end_date.strftime("%d-%m-%y")
        data["action"] = "Search"
        action = form.get("action") or listing_url
        return self.http.post_form(urljoin(response.url, action), data)

    def _fetch_aspnet_keyword_listing(
        self,
        response: FetchResponse,
        document: html.HtmlElement,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> FetchResponse:
        forms = document.xpath("//form[.//input[@name='ctl00$BodyPlaceHolder$uxTextSearchKeywords']]")
        if not forms or not (start_date or end_date):
            return response
        form = forms[0]
        start_year = (start_date or end_date).year
        end_year = (end_date or start_date).year
        action = form.get("action") or response.url
        search_url = urljoin(response.url, action)
        responses: list[FetchResponse] = []
        for year in range(start_year, end_year + 1):
            data = self._form_defaults(form)
            data["ctl00$BodyPlaceHolder$uxTextSearchKeywords"] = f"P/{year % 100:02d}"
            submit_name, submit_value = self._first_submit(form)
            if submit_name:
                data[submit_name] = submit_value or "Search"
            responses.append(self.http.post_form(search_url, data))
        if len(responses) == 1:
            return responses[0]
        return FetchResponse(
            url=responses[-1].url,
            status_code=responses[-1].status_code,
            text="<html><body>" + "\n".join(item.text for item in responses) + "</body></html>",
        )

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if not url:
            raise ValueError("Ocella application fetch requires a detail URL")
        response = self.http.get(url)
        application = self.parse_detail(response.text, response.url, fallback_uid=uid)
        if include_documents:
            application.documents = self.parse_documents(response.text, response.url)
        return application

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        seen: set[str] = set()
        applications: list[PlanningApplication] = []

        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href")
            uid = self._extract_uid(href)
            if not uid or uid in seen:
                continue
            if not self._looks_like_application_link(href, anchor):
                continue
            seen.add(uid)

            row_text = self._nearest_row_text(anchor)
            query = parse_qs(urlsplit(href).query)
            reference = uid if query.get("reference") else self._extract_reference(anchor, row_text) or uid
            row_data = self._result_row_data(anchor)
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=urljoin(page_url, href),
                    reference=reference,
                    address=row_data.get("address") or self._extract_address(row_text, reference),
                    description=row_data.get("description"),
                    status=row_data.get("status"),
                    date_received=row_data.get("date_received"),
                    postcode=extract_postcode(row_data.get("address") or row_text),
                    source_url=page_url,
                    raw={"portal_family": "ocella", "detail_complete": bool(row_data), "listing_text": row_text},
                )
            )

        return applications

    def parse_detail(
        self,
        html_text: str,
        page_url: str,
        *,
        fallback_uid: str | None = None,
    ) -> PlanningApplication:
        document = html.fromstring(html_text)
        fields = self._extract_labelled_fields(document)
        raw: dict[str, str] = {}
        mapped: dict[str, str] = {}

        for label, value in fields.items():
            raw[label] = value
            model_field = self._label_map.get(normalize_label(label))
            if model_field and value:
                mapped[model_field] = value

        for date_field in ("date_received", "date_validated"):
            if mapped.get(date_field):
                mapped[date_field] = parse_council_date(mapped[date_field]) or mapped[date_field]

        uid = self._extract_uid(page_url) or fallback_uid or mapped.get("reference")
        if not uid:
            raise ValueError("Could not determine Ocella application uid")

        address = mapped.get("address")
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=page_url,
            reference=mapped.get("reference"),
            address=address,
            description=mapped.get("description"),
            status=mapped.get("status"),
            decision=mapped.get("decision"),
            date_received=mapped.get("date_received"),
            date_validated=mapped.get("date_validated"),
            applicant_name=mapped.get("applicant_name"),
            agent_name=mapped.get("agent_name"),
            case_officer=mapped.get("case_officer"),
            ward=mapped.get("ward"),
            parish=mapped.get("parish"),
            postcode=extract_postcode(address),
            source_url=self.config.base_url,
            documents=self.parse_documents(html_text, page_url),
            raw=raw,
        )

    def parse_documents(self, html_text: str, page_url: str) -> list[PlanningDocument]:
        document = html.fromstring(html_text)
        documents: list[PlanningDocument] = []
        seen: set[str] = set()

        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href")
            if not self._is_document_href(href):
                continue
            absolute_url = urljoin(page_url, href)
            if absolute_url in seen:
                continue
            seen.add(absolute_url)
            row_text = self._nearest_row_text(anchor)
            documents.append(
                PlanningDocument(
                    title=clean_text(" ".join(anchor.itertext())) or "Document",
                    url=absolute_url,
                    date_published=parse_council_date(self._first_date(row_text)),
                    description=self._document_description(row_text, anchor),
                    source_url=page_url,
                )
            )

        return documents

    def _form_defaults(self, form: html.HtmlElement) -> dict[str, str]:
        data: dict[str, str] = {}
        for element in form.xpath(".//input|.//select|.//textarea"):
            name = element.get("name")
            if not name:
                continue
            tag = element.tag.lower()
            input_type = (element.get("type") or "").lower()
            if tag == "select":
                selected = element.xpath(".//option[@selected]") or element.xpath(".//option")
                data[name] = selected[0].get("value") or "" if selected else ""
            elif input_type in {"checkbox", "radio"}:
                if element.get("checked"):
                    data[name] = element.get("value") or "on"
            elif input_type != "submit":
                data[name] = element.get("value") or ""
        return data

    def _first_submit(self, form: html.HtmlElement) -> tuple[str | None, str | None]:
        for element in form.xpath(".//input[@type='submit' and @name]"):
            return element.get("name"), element.get("value")
        return None, None

    def _result_row_data(self, anchor: html.HtmlElement) -> dict[str, str]:
        row = anchor.xpath("ancestor::tr[1]")
        if not row:
            return {}
        cells = [clean_text(" ".join(cell.itertext())) or "" for cell in row[0].xpath("./td")]
        if len(cells) < 3:
            return {}
        data: dict[str, str] = {
            "address": cells[1],
            "description": cells[2],
        }
        if len(cells) >= 5:
            data["date_received"] = parse_council_date(cells[3])
            data["status"] = cells[4]
        elif len(cells) >= 4:
            if re.search(r"\d{1,2}[-/]\d{1,2}[-/]\d{2,4}", cells[3]):
                data["date_received"] = parse_council_date(cells[3])
            else:
                data["status"] = cells[3]
        return {key: value for key, value in data.items() if value}

    def _extract_uid(self, url_or_href: str | None) -> str | None:
        if not url_or_href:
            return None
        query = parse_qs(urlsplit(url_or_href).query)
        for param in self.config.uid_query_params:
            value = query.get(param)
            if value and value[0]:
                return value[0]
        return None

    def _looks_like_application_link(self, href: str | None, anchor: html.HtmlElement) -> bool:
        text = clean_text(" ".join(anchor.itertext())) or ""
        combined = f"{href or ''} {text}".lower()
        return any(token in combined for token in ("planning", "application", "detail", "case", "app"))

    def _nearest_row_text(self, anchor: html.HtmlElement) -> str | None:
        row = anchor.xpath("ancestor::tr[1]")
        if row:
            return clean_text(" ".join(row[0].itertext()))
        item = anchor.xpath("ancestor::li[1] | ancestor::article[1] | ancestor::div[1]")
        if item:
            return clean_text(" ".join(item[0].itertext()))
        return clean_text(" ".join(anchor.itertext()))

    def _extract_reference(self, anchor: html.HtmlElement, row_text: str | None) -> str | None:
        anchor_text = clean_text(" ".join(anchor.itertext()))
        for value in (anchor_text, row_text):
            if not value:
                continue
            match = re.search(r"\b\d{2,4}[/.-][A-Z0-9/.-]+\b", value, flags=re.IGNORECASE)
            if match:
                return match.group(0)
        return anchor_text

    def _extract_address(self, row_text: str | None, reference: str | None) -> str | None:
        if not row_text:
            return None
        text = row_text
        if reference:
            text = text.replace(reference, " ")
        return clean_text(text)

    def _extract_labelled_fields(self, document: html.HtmlElement) -> dict[str, str]:
        fields: dict[str, str] = {}
        for row in document.xpath("//*[contains(concat(' ', normalize-space(@class), ' '), ' docGridRow ')]"):
            label = clean_text(
                " ".join(
                    row.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' detailsFieldNames ')]//text()")
                )
            )
            value = clean_text(
                " ".join(
                    row.xpath(".//*[contains(concat(' ', normalize-space(@class), ' '), ' detailsValues ')]//text()")
                )
            )
            if label and value:
                fields[label] = value

        for row in document.xpath("//tr[th and td]"):
            label = clean_text(" ".join(row.xpath("./th[1]//text()")))
            value = clean_text(" ".join(row.xpath("./td[1]//text()")))
            if label and value:
                fields[label] = value

        for row in document.xpath("//tr[td and count(td) >= 2]"):
            label = clean_text(" ".join(row.xpath("./td[1]//text()")))
            value = clean_text(" ".join(row.xpath("./td[2]//text()")))
            if label and value and len(label) < 80:
                fields.setdefault(label, value)

        for container in document.xpath("//dl"):
            for term in container.xpath("./dt"):
                label = clean_text(" ".join(term.itertext()))
                sibling = term.getnext()
                if sibling is not None and sibling.tag.lower() == "dd":
                    value = clean_text(" ".join(sibling.itertext()))
                    if label and value:
                        fields[label] = value
        return fields

    def _is_document_href(self, href: str | None) -> bool:
        if not href:
            return False
        href_lower = href.lower()
        return any(
            marker in href_lower
            for marker in (
                "document",
                "attachment",
                "download",
                ".pdf",
                ".doc",
                ".docx",
                ".jpg",
                ".jpeg",
                ".png",
            )
        )

    def _first_date(self, text: str | None) -> str | None:
        if not text:
            return None
        match = re.search(
            r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+\w+\s+\d{4})\b",
            text,
        )
        return match.group(0) if match else None

    def _document_description(self, row_text: str | None, anchor: html.HtmlElement) -> str | None:
        title = clean_text(" ".join(anchor.itertext()))
        if not row_text or not title:
            return row_text
        return clean_text(row_text.replace(title, " "))
