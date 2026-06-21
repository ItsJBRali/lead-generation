from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient, FetchResponse
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text, extract_postcode, normalize_label, parse_council_date


@dataclass(frozen=True, slots=True)
class IdoxCouncilConfig:
    authority: str
    base_url: str
    application_root: str = "/online-applications/"


class IdoxPublicAccessScraper(PlanningScraper):
    """Scraper for councils using Idox PublicAccess planning portals."""

    MAX_PAGED_RESULT_PAGES = 100

    _label_map = {
        "reference": "reference", "caseno": "reference", "case_no": "reference",
        "application_reference": "reference", "planning_reference": "reference", "application_number": "reference",
        "address": "address", "site_address": "address", "location": "address",
        "proposal": "description", "description": "description", "development_description": "description",
        "status": "status", "application_status": "status", "decision": "decision",
        "date_received": "date_received", "received": "date_received", "application_received": "date_received",
        "valid_date": "date_validated", "date_valid": "date_validated", "date_validated": "date_validated",
        "validated": "date_validated", "application_validated": "date_validated",
        "applicant_name": "applicant_name", "applicant": "applicant_name",
        "agent_name": "agent_name", "agent": "agent_name",
        "case_officer": "case_officer", "officer": "case_officer", "ward": "ward", "parish": "parish",
    }

    def __init__(self, config: IdoxCouncilConfig, *, http_client: CouncilHttpClient | None = None) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient(timeout_seconds=30.0, min_delay_seconds=1.5, retries=4)

    def discover_ids(self, *, listing_url: str | None = None, start_date: date | None = None, end_date: date | None = None, limit: int | None = None) -> DiscoveryResult:
        if listing_url and (start_date or end_date):
            response = self._fetch_advanced_search(listing_url, start_date=start_date, end_date=end_date)
        elif listing_url:
            response = self.http.get(listing_url)
        else:
            response = self._fetch_weekly_list(start_date=start_date, end_date=end_date)
        applications = self._parse_listing_pages(response, limit=limit)
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(authority=self.authority, source_url=response.url, applications=applications)

    def fetch_application(self, uid: str, url: str | None = None, *, include_documents: bool = False) -> PlanningApplication:
        response = self.http.get(url or self.build_detail_url(uid))
        application = self.parse_detail(response.text, response.url, fallback_uid=uid)
        if include_documents:
            application.documents = self.fetch_documents(uid)
        return application

    def build_weekly_list_url(self, *, start_date: date | None = None, end_date: date | None = None) -> str:
        params = {"action": "weeklyList"}
        if start_date:
            params["dateStart"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            params["dateEnd"] = end_date.strftime("%d/%m/%Y")
        return f"{self._portal_url('search.do')}?{urlencode(params)}"

    def build_detail_url(self, uid: str) -> str:
        return f"{self._portal_url('applicationDetails.do')}?{urlencode({'activeTab': 'summary', 'keyVal': uid})}"

    def build_documents_url(self, uid: str) -> str:
        return f"{self._portal_url('applicationDetails.do')}?{urlencode({'activeTab': 'documents', 'keyVal': uid})}"

    def fetch_documents(self, uid: str, url: str | None = None) -> list[PlanningDocument]:
        response = self.http.get(url or self.build_documents_url(uid))
        return self.parse_documents(response.text, response.url)

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        seen: set[str] = set()
        applications: list[PlanningApplication] = []
        for anchor in document.xpath("//a[contains(@href, 'applicationDetails.do')]"):
            href = anchor.get("href")
            uid = self._extract_uid(href)
            if not uid or uid in seen:
                continue
            seen.add(uid)
            row_text = self._nearest_row_text(anchor)
            reference = self._extract_reference(anchor, row_text)
            applications.append(PlanningApplication(
                authority=self.authority, uid=uid, url=self._summary_url(page_url, uid),
                reference=reference, address=self._extract_address(row_text, reference),
                source_url=page_url, raw={"listing_text": row_text} if row_text else {},
            ))
        if len(applications) == 1 and self._looks_like_application_summary(document):
            detail = self.parse_detail(html_text, page_url, fallback_uid=applications[0].uid)
            detail.url = applications[0].url
            detail.source_url = page_url
            return [detail]
        return applications

    def _parse_listing_pages(self, response: FetchResponse, *, limit: int | None = None) -> list[PlanningApplication]:
        applications: list[PlanningApplication] = []
        seen_uids: set[str] = set()
        seen_urls: set[str] = {response.url}
        queued_urls = self._paged_result_urls(response.text, response.url)
        processed_pages = 1

        def add_page(html_text: str, page_url: str) -> None:
            for application in self.parse_listing(html_text, page_url):
                if application.uid in seen_uids:
                    continue
                seen_uids.add(application.uid)
                applications.append(application)

        add_page(response.text, response.url)
        while queued_urls and (limit is None or len(applications) < limit):
            if processed_pages >= self.MAX_PAGED_RESULT_PAGES:
                break
            page_url = queued_urls.pop(0)
            if page_url in seen_urls:
                continue
            seen_urls.add(page_url)
            page = self.http.get(page_url)
            processed_pages += 1
            add_page(page.text, page.url)
            for discovered_url in self._paged_result_urls(page.text, page.url):
                if discovered_url not in seen_urls and discovered_url not in queued_urls:
                    queued_urls.append(discovered_url)
            queued_urls.sort(key=self._paged_result_sort_key)
        return applications

    def _paged_result_urls(self, html_text: str, page_url: str) -> list[str]:
        document = html.fromstring(html_text)
        urls: list[str] = []
        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            parsed = urlsplit(href)
            query = parse_qs(parsed.query)
            if query.get("action", [""])[0] != "page":
                continue
            if not (query.get("searchCriteria.page") or query.get("page")):
                continue
            absolute_url = urljoin(page_url, href)
            if absolute_url not in urls:
                urls.append(absolute_url)
        return sorted(urls, key=self._paged_result_sort_key)

    def _paged_result_sort_key(self, url: str) -> int:
        query = parse_qs(urlsplit(url).query)
        for key in ("searchCriteria.page", "page"):
            values = query.get(key)
            if values and values[0].isdigit():
                return int(values[0])
        return 0

    def parse_detail(self, html_text: str, page_url: str, *, fallback_uid: str | None = None) -> PlanningApplication:
        fields = self._extract_labelled_fields(html.fromstring(html_text))
        raw: dict[str, str] = {}
        mapped: dict[str, str] = {}
        for label, value in fields.items():
            raw[label] = value
            model_field = self._label_map.get(normalize_label(label))
            if model_field and value:
                mapped[model_field] = value
        for field in ("date_received", "date_validated"):
            if mapped.get(field):
                mapped[field] = parse_council_date(mapped[field]) or mapped[field]
        uid = self._extract_uid(page_url) or self._raw_value(raw, "casetechnicalkey", "case_technical_key") or fallback_uid or mapped.get("reference")
        if not uid:
            raise ValueError("Could not determine Idox application uid")
        address = mapped.get("address")
        return PlanningApplication(
            authority=self.authority, uid=uid, url=page_url, reference=mapped.get("reference"),
            address=address, description=mapped.get("description"), status=mapped.get("status"),
            decision=mapped.get("decision"), date_received=mapped.get("date_received"),
            date_validated=mapped.get("date_validated"), applicant_name=mapped.get("applicant_name"),
            agent_name=mapped.get("agent_name"), case_officer=mapped.get("case_officer"),
            ward=mapped.get("ward"), parish=mapped.get("parish"), postcode=extract_postcode(address, raw.get("postcode")),
            source_url=self.config.base_url, raw=raw,
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
            row = anchor.xpath("ancestor::tr[1]")
            cells = [clean_text(" ".join(cell.itertext())) for cell in row[0].xpath("./th|./td")] if row else []
            cells = [cell for cell in cells if cell]
            title = clean_text(" ".join(anchor.itertext())) or self._document_title_from_url(absolute_url)
            metadata = self._document_metadata(cells, row_text, title)
            documents.append(PlanningDocument(
                title=title, url=absolute_url, document_type=metadata.get("document_type"),
                date_published=parse_council_date(metadata.get("date_published")),
                file_size=metadata.get("file_size"), description=metadata.get("description"),
                source_url=page_url,
            ))
        return documents

    def _fetch_weekly_list(self, *, start_date: date | None = None, end_date: date | None = None):
        response = self.http.get(self.build_weekly_list_url(start_date=start_date, end_date=end_date))
        document = html.fromstring(response.text)
        forms = document.xpath("//form[contains(@action, 'weeklyListResults.do')]")
        if not forms:
            return response
        form = forms[0]
        data = self._form_defaults(form)
        data.setdefault("searchType", "Application")
        data.setdefault("dateType", "DC_Validated")
        return self.http.post_form(urljoin(response.url, form.get("action")), data)

    def _fetch_advanced_search(self, listing_url: str, *, start_date: date | None = None, end_date: date | None = None):
        try:
            response = self.http.get(listing_url)
            document = html.fromstring(response.text)
            forms = document.xpath("//form[contains(@action, 'advancedSearchResults.do') or contains(@action, 'searchResults.do')]")
            if not forms:
                params = self._advanced_search_dates(start_date=start_date, end_date=end_date)
                return self.http.get(urljoin(response.url, "advancedSearchResults.do?action=firstPage"), params)
            form = forms[0]
            data = self._form_defaults(form)
            data.update(self._advanced_search_dates(start_date=start_date, end_date=end_date, form_data=data))
            data.setdefault("searchType", "Application")
            action = form.get("action") or "advancedSearchResults.do?action=firstPage"
            return self.http.post_form(urljoin(response.url, action), data)
        except CouncilFetchError:
            if start_date or end_date:
                return self._fetch_weekly_list(start_date=start_date, end_date=end_date)
            raise

    def _advanced_search_dates(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        form_data: dict[str, str] | None = None,
    ) -> dict[str, str]:
        data: dict[str, str] = {}
        if start_date:
            data[self._advanced_date_field(form_data, "start")] = start_date.strftime("%d/%m/%Y")
        if end_date:
            data[self._advanced_date_field(form_data, "end")] = end_date.strftime("%d/%m/%Y")
        return data

    def _advanced_date_field(self, form_data: dict[str, str] | None, which: str) -> str:
        if not form_data:
            return "searchCriteria.dateReceivedStart" if which == "start" else "searchCriteria.dateReceivedEnd"
        candidates = (
            ("date(applicationReceivedStart)", "searchCriteria.dateReceivedStart", "date(applicationValidatedStart)")
            if which == "start"
            else ("date(applicationReceivedEnd)", "searchCriteria.dateReceivedEnd", "date(applicationValidatedEnd)")
        )
        for candidate in candidates:
            if candidate in form_data:
                return candidate
        return candidates[1]

    def _form_defaults(self, form: html.HtmlElement) -> dict[str, str]:
        data: dict[str, str] = {}
        for input_node in form.xpath(".//input[@name]"):
            name = input_node.get("name")
            input_type = (input_node.get("type") or "text").lower()
            if input_type in {"submit", "button", "image", "reset"}:
                continue
            if input_type in {"radio", "checkbox"} and input_node.get("checked") is None and name in data:
                continue
            data[name] = input_node.get("value") or ""
        for select in form.xpath(".//select[@name]"):
            chosen = (select.xpath(".//option[@selected]") or select.xpath(".//option")[:1])
            if chosen:
                option_value = chosen[0].get("value")
                data[select.get("name")] = option_value if option_value is not None else clean_text(" ".join(chosen[0].itertext())) or ""
        return data

    def _portal_url(self, path: str) -> str:
        return urljoin(self.config.base_url.rstrip("/") + "/", self.config.application_root.strip("/") + "/" + path)

    def _summary_url(self, page_url: str, uid: str) -> str:
        return urljoin(page_url, f"applicationDetails.do?{urlencode({'activeTab': 'summary', 'keyVal': uid})}")

    def _extract_uid(self, url_or_href: str | None) -> str | None:
        if not url_or_href:
            return None
        query = parse_qs(urlsplit(url_or_href).query)
        key_val = query.get("keyVal") or query.get("keyval")
        if key_val and key_val[0]:
            return key_val[0]
        match = re.search(r"\bkeyVal=([^&#]+)", url_or_href, flags=re.IGNORECASE)
        return match.group(1) if match else None

    def _nearest_row_text(self, anchor: html.HtmlElement) -> str | None:
        row = anchor.xpath("ancestor::tr[1]")
        if row:
            return clean_text(" ".join(row[0].itertext()))
        item = anchor.xpath("ancestor::li[1] | ancestor::article[1] | ancestor::div[contains(@class, 'searchresult')][1]")
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
        text = row_text.replace(reference, " ") if reference else row_text
        return clean_text(re.sub(r"\b(Application|Reference|Validated|Received|Status)\b:?", " ", text, flags=re.IGNORECASE))

    def _extract_labelled_fields(self, document: html.HtmlElement) -> dict[str, str]:
        fields: dict[str, str] = {}
        for row in document.xpath("//tr[th and td]"):
            label = clean_text(" ".join(row.xpath("./th[1]//text()")))
            value = clean_text(" ".join(row.xpath("./td[1]//text()")))
            if label and value:
                fields[label] = value
        for container in document.xpath("//dl"):
            for term in container.xpath("./dt"):
                label = clean_text(" ".join(term.itertext()))
                values: list[str] = []
                sibling = term.getnext()
                while sibling is not None and sibling.tag.lower() != "dt":
                    if sibling.tag.lower() == "dd":
                        text = clean_text(" ".join(sibling.itertext()))
                        if text:
                            values.append(text)
                    sibling = sibling.getnext()
                if label and values:
                    fields[label] = clean_text(" ".join(values)) or values[0]
        for label_node in document.xpath("//*[contains(@class, 'field') or contains(@class, 'label')]"):
            label = clean_text(" ".join(label_node.itertext()))
            value_node = label_node.getnext()
            if label and value_node is not None:
                value = clean_text(" ".join(value_node.itertext()))
                if value:
                    fields.setdefault(label, value)
        for input_node in document.xpath("//input[@name and @value]"):
            if (input_node.get("type") or "").lower() == "hidden":
                label = input_node.get("name")
                value = clean_text(input_node.get("value"))
                if label and value and not self._is_transient_form_field(label):
                    fields.setdefault(label, value)
        return fields

    def _is_document_href(self, href: str | None) -> bool:
        if not href:
            return False
        href_lower = href.lower()
        if "applicationdetails.do" in href_lower:
            return False
        return any(marker in href_lower for marker in ("documentdownload", "documentviewer", "document.do", ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png"))

    def _document_title_from_url(self, url: str) -> str:
        query = parse_qs(urlsplit(url).query)
        for key in ("name", "docName", "documentName", "filename"):
            if query.get(key):
                return query[key][0]
        return urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1] or "Document"

    def _document_metadata(self, cells: list[str], row_text: str | None, title: str) -> dict[str, str]:
        metadata: dict[str, str] = {}
        for cell in [cell for cell in cells if cell != title]:
            normalized = normalize_label(cell)
            if re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{4}\b", cell) or re.search(r"\b(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+\w+\s+\d{4}\b", cell):
                metadata.setdefault("date_published", cell)
            elif re.search(r"\b\d+(?:\.\d+)?\s*(?:kb|mb|gb)\b", cell, flags=re.IGNORECASE):
                metadata.setdefault("file_size", cell)
            elif normalized in {"drawing", "plan", "decision_notice", "application_form", "supporting_document"}:
                metadata.setdefault("document_type", cell)
            else:
                metadata.setdefault("description", cell)
        if row_text and not metadata.get("date_published"):
            match = re.search(r"\b(?:\d{1,2}[/-]\d{1,2}[/-]\d{4}|(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+\d{1,2}\s+\w+\s+\d{4})\b", row_text)
            if match:
                metadata["date_published"] = match.group(0)
        return metadata

    def _raw_value(self, raw: dict[str, str], *normalized_labels: str) -> str | None:
        for label, value in raw.items():
            if normalize_label(label) in set(normalized_labels):
                return value
        return None

    def _looks_like_application_summary(self, document: html.HtmlElement) -> bool:
        return "application summary" in (clean_text(" ".join(document.xpath("//h1//text() | //title//text()"))) or "").lower()

    def _is_transient_form_field(self, label: str) -> bool:
        normalized = normalize_label(label)
        return normalized.startswith("_") or "token" in normalized or normalized == "csrf"
