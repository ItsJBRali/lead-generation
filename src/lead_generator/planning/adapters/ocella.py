from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text, extract_postcode, normalize_label, parse_council_date


@dataclass(frozen=True, slots=True)
class OcellaCouncilConfig:
    authority: str
    base_url: str
    uid_query_params: tuple[str, ...] = ("id", "appNo", "appno", "reference", "ref", "caseNo", "case", "application")


class OcellaPlanningScraper(PlanningScraper):
    """Scraper for Ocella-style planning registers with labelled HTML pages."""

    _label_map = {
        "reference": "reference", "application_reference": "reference", "application_number": "reference",
        "application_no": "reference", "case_no": "reference", "site_address": "address",
        "address": "address", "location": "address", "proposal": "description",
        "description": "description", "development_description": "description", "status": "status",
        "application_status": "status", "decision": "decision", "date_received": "date_received",
        "received_date": "date_received", "valid_date": "date_validated", "date_validated": "date_validated",
        "applicant": "applicant_name", "applicant_name": "applicant_name",
        "agent": "agent_name", "agent_name": "agent_name", "case_officer": "case_officer",
        "ward": "ward", "parish": "parish",
    }

    def __init__(self, config: OcellaCouncilConfig, *, http_client: CouncilHttpClient | None = None) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient()

    def discover_ids(self, *, listing_url: str, limit: int | None = None, **_: object) -> DiscoveryResult:
        response = self.http.get(listing_url)
        applications = self.parse_listing(response.text, response.url)
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(authority=self.authority, source_url=response.url, applications=applications)

    def fetch_application(self, uid: str, url: str | None = None, *, include_documents: bool = False) -> PlanningApplication:
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
            if not uid or uid in seen or not self._looks_like_application_link(href, anchor):
                continue
            seen.add(uid)
            row_text = self._nearest_row_text(anchor)
            reference = self._extract_reference(anchor, row_text) or uid
            applications.append(PlanningApplication(
                authority=self.authority, uid=uid, url=urljoin(page_url, href), reference=reference,
                address=self._extract_address(row_text, reference), source_url=page_url,
                raw={"listing_text": row_text} if row_text else {},
            ))
        return applications

    def parse_detail(self, html_text: str, page_url: str, *, fallback_uid: str | None = None) -> PlanningApplication:
        document = html.fromstring(html_text)
        raw: dict[str, str] = {}
        mapped: dict[str, str] = {}
        for label, value in self._extract_labelled_fields(document).items():
            raw[label] = value
            model_field = self._label_map.get(normalize_label(label))
            if model_field and value:
                mapped[model_field] = value
        for field in ("date_received", "date_validated"):
            if mapped.get(field):
                mapped[field] = parse_council_date(mapped[field]) or mapped[field]
        uid = self._extract_uid(page_url) or fallback_uid or mapped.get("reference")
        if not uid:
            raise ValueError("Could not determine Ocella application uid")
        address = mapped.get("address")
        return PlanningApplication(
            authority=self.authority, uid=uid, url=page_url, reference=mapped.get("reference"),
            address=address, description=mapped.get("description"), status=mapped.get("status"),
            decision=mapped.get("decision"), date_received=mapped.get("date_received"),
            date_validated=mapped.get("date_validated"), applicant_name=mapped.get("applicant_name"),
            agent_name=mapped.get("agent_name"), case_officer=mapped.get("case_officer"),
            ward=mapped.get("ward"), parish=mapped.get("parish"), postcode=extract_postcode(address),
            source_url=self.config.base_url, documents=self.parse_documents(html_text, page_url), raw=raw,
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
            title = clean_text(" ".join(anchor.itertext())) or "Document"
            documents.append(PlanningDocument(
                title=title, url=absolute_url, date_published=parse_council_date(self._first_date(row_text)),
                description=self._document_description(row_text, title),
            ))
        return documents

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
        for value in (clean_text(" ".join(anchor.itertext())), row_text):
            if value:
                match = re.search(r"\b\d{2,4}[/.-][A-Z0-9/.-]+\b", value, flags=re.IGNORECASE)
                if match:
                    return match.group(0)
        return clean_text(" ".join(anchor.itertext()))

    def _extract_address(self, row_text: str | None, reference: str | None) -> str | None:
        if not row_text:
            return None
        return clean_text(row_text.replace(reference, " ") if reference else row_text)

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
        return fields

    def _is_document_href(self, href: str | None) -> bool:
        if not href:
            return False
        return any(marker in href.lower() for marker in ("document", "attachment", "download", ".pdf", ".doc", ".docx", ".jpg", ".jpeg", ".png"))

    def _first_date(self, text: str | None) -> str | None:
        if not text:
            return None
        match = re.search(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+\w+\s+\d{4})\b", text)
        return match.group(0) if match else None

    def _document_description(self, row_text: str | None, title: str) -> str | None:
        if not row_text:
            return None
        return clean_text(row_text.replace(title, " "))
