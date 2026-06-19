from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import (
    clean_text,
    extract_postcode,
    normalize_label,
    parse_council_date,
)


REFERENCE_RE = re.compile(r"\b\d{2,4}[/.-][A-Z0-9/.-]+\b", flags=re.IGNORECASE)


GENERIC_LABEL_MAP = {
    "reference": "reference",
    "application_reference": "reference",
    "application_ref": "reference",
    "application_number": "reference",
    "application_no": "reference",
    "planning_reference": "reference",
    "planning_application_reference": "reference",
    "case_no": "reference",
    "site_address": "address",
    "development_address": "address",
    "address": "address",
    "location": "address",
    "location_address": "address",
    "site_location": "address",
    "location_of_development": "address",
    "proposal": "description",
    "proposed_development": "description",
    "description": "description",
    "description_of_development": "description",
    "development_description": "description",
    "status": "status",
    "application_status": "status",
    "current_status": "status",
    "decision": "decision",
    "decision_type": "decision",
    "date_received": "date_received",
    "application_received": "date_received",
    "application_received_date": "date_received",
    "received_date": "date_received",
    "received": "date_received",
    "valid_date": "date_validated",
    "application_valid": "date_validated",
    "application_valid_date": "date_validated",
    "date_validated": "date_validated",
    "validated_date": "date_validated",
    "validated": "date_validated",
    "date_registered": "date_validated",
    "registration_date": "date_validated",
    "application_registered": "date_validated",
    "applicant": "applicant_name",
    "applicant_name": "applicant_name",
    "agent": "agent_name",
    "agent_name": "agent_name",
    "case_officer": "case_officer",
    "officer": "case_officer",
    "ward": "ward",
    "ward_name": "ward",
    "wards": "ward",
    "parish": "parish",
}


@dataclass(frozen=True, slots=True)
class GenericCouncilConfig:
    authority: str
    base_url: str
    family: str
    uid_query_params: tuple[str, ...]
    detail_markers: tuple[str, ...]
    label_map: dict[str, str] | None = None


class GenericLabelledPlanningScraper(PlanningScraper):
    """Scraper for non-Idox portals that expose labelled HTML records."""

    def __init__(
        self,
        config: GenericCouncilConfig,
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
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(
            authority=self.authority,
            source_url=response.url,
            applications=applications,
        )

    def _fetch_listing(self, listing_url: str, *, start_date: date | None = None, end_date: date | None = None):
        response = self.http.get(listing_url)
        if not (start_date or end_date):
            return response
        document = html.fromstring(response.text)
        forms = document.xpath(
            "//form[.//input[@name='DateReceivedFrom'] or .//input[@name='DateReceivedTo'] "
            "or .//input[@name='DateValidFrom'] or .//input[@name='DateValidTo']]"
        )
        if not forms:
            return response
        form = forms[-1]
        data = self._form_defaults(form)
        if start_date:
            data[self._date_field(data, "DateReceivedFrom", "DateValidFrom")] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data[self._date_field(data, "DateReceivedTo", "DateValidTo")] = end_date.strftime("%d/%m/%Y")
        if "SearchPlanning" in data:
            data["SearchPlanning"] = "true"
        data["Outstanding"] = data.get("Outstanding") or "false"
        action = form.get("action") or listing_url
        return self.http.post_form(urljoin(response.url, action), data)

    def _date_field(self, data: dict[str, str], preferred: str, fallback: str) -> str:
        return preferred if preferred in data else fallback

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if not url:
            raise ValueError(f"{self.config.family} application fetch requires a detail URL")
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
            if not self._looks_like_application_link(href, anchor):
                continue
            row_text = self._nearest_row_text(anchor)
            reference = self._extract_reference(anchor, row_text)
            uid = self._extract_uid(href) or reference
            if not uid or uid in seen:
                continue
            seen.add(uid)
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=urljoin(page_url, href),
                    reference=reference or uid,
                    address=self._extract_address(row_text, reference or uid),
                    source_url=page_url,
                    raw={
                        "portal_family": self.config.family,
                        "listing_text": row_text,
                    },
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
        label_map = self.config.label_map or GENERIC_LABEL_MAP

        for label, value in fields.items():
            raw[label] = value
            model_field = label_map.get(normalize_label(label))
            if model_field and value and model_field not in mapped:
                mapped[model_field] = value

        for date_field in ("date_received", "date_validated"):
            if mapped.get(date_field):
                mapped[date_field] = parse_council_date(mapped[date_field]) or mapped[date_field]

        uid = (
            self._extract_uid(page_url)
            or fallback_uid
            or mapped.get("reference")
            or self._extract_reference_from_text(clean_text(" ".join(document.xpath("//body//text()"))))
        )
        if not uid:
            raise ValueError(f"Could not determine {self.config.family} application uid")

        address = mapped.get("address")
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=page_url,
            reference=mapped.get("reference") or fallback_uid,
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
            raw={"portal_family": self.config.family, **raw},
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

    def _extract_uid(self, url_or_href: str | None) -> str | None:
        if not url_or_href:
            return None
        parts = urlsplit(url_or_href)
        query = parse_qs(parts.query)
        for param in self.config.uid_query_params:
            value = query.get(param)
            if value and value[0]:
                return value[0]
        path_match = re.search(r"/(?:planning/)?(?:display|application|details?)/([A-Z0-9][A-Z0-9/.-]{3,})/?$", parts.path, flags=re.IGNORECASE)
        if path_match:
            return path_match.group(1)
        return self._extract_reference_from_text(url_or_href)

    def _looks_like_application_link(self, href: str | None, anchor: html.HtmlElement) -> bool:
        if not href or href.startswith("#"):
            return False
        text = clean_text(" ".join(anchor.itertext())) or ""
        combined = f"{href or ''} {text}".lower()
        if any(marker in combined for marker in self.config.detail_markers):
            return True
        if self._extract_reference(anchor, self._nearest_row_text(anchor)):
            return any(token in combined for token in ("planning", "application", "detail", "case", "app"))
        return False

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
            reference = self._extract_reference_from_text(value)
            if reference:
                return reference
        return None

    def _extract_reference_from_text(self, value: str | None) -> str | None:
        if not value:
            return None
        match = REFERENCE_RE.search(value)
        return match.group(0) if match else None

    def _extract_address(self, row_text: str | None, reference: str | None) -> str | None:
        if not row_text:
            return None
        text = row_text
        if reference:
            text = text.replace(reference, " ")
        return clean_text(text)

    def _extract_labelled_fields(self, document: html.HtmlElement) -> dict[str, str]:
        fields: dict[str, str] = {}
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

        for label_node in document.xpath(
            "//*[contains(translate(@class, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'label')]"
        ):
            label = clean_text(" ".join(label_node.itertext()))
            sibling = label_node.getnext()
            if sibling is None:
                continue
            value = clean_text(" ".join(sibling.itertext()))
            if label and value and len(label) < 80:
                fields.setdefault(label, value)

        self._extract_sequential_text_fields(document, fields)
        return fields

    def _form_defaults(self, form: html.HtmlElement) -> dict[str, str]:
        data: dict[str, str] = {}
        for input_node in form.xpath(".//input[@name]"):
            name = input_node.get("name")
            input_type = (input_node.get("type") or "text").lower()
            if input_type in {"submit", "button", "image", "reset"}:
                continue
            if input_type in {"radio", "checkbox"} and input_node.get("checked") is None:
                continue
            if input_type == "hidden" and name in data and (input_node.get("value") or "").casefold() == "false":
                continue
            data[name] = input_node.get("value") or ""
        for select in form.xpath(".//select[@name]"):
            chosen = select.xpath(".//option[@selected]") or select.xpath(".//option")[:1]
            if chosen:
                option_value = chosen[0].get("value")
                data[select.get("name")] = option_value if option_value is not None else clean_text(" ".join(chosen[0].itertext())) or ""
        for textarea in form.xpath(".//textarea[@name]"):
            data[textarea.get("name")] = clean_text(" ".join(textarea.itertext())) or ""
        return data

    def _extract_sequential_text_fields(
        self,
        document: html.HtmlElement,
        fields: dict[str, str],
    ) -> None:
        label_keys = set((self.config.label_map or GENERIC_LABEL_MAP).keys())
        text_nodes = [clean_text(text) for text in document.xpath("//body//text()")]
        texts = [text for text in text_nodes if text]

        for index, label in enumerate(texts):
            normalized_label = normalize_label(label)
            if normalized_label not in label_keys:
                continue
            if (self.config.label_map or GENERIC_LABEL_MAP).get(normalized_label) == "case_officer":
                continue
            for value in texts[index + 1 : index + 6]:
                normalized_value = normalize_label(value)
                if normalized_value in label_keys:
                    continue
                if normalized_value.startswith("view_"):
                    continue
                if normalized_value in ("select_your_property", "find_your_nearest"):
                    continue
                if len(label) < 80:
                    fields.setdefault(label, value)
                break

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
                "docview",
                "doclist",
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
