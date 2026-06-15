from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import (
    clean_text,
    extract_postcode,
    normalize_label,
    parse_council_date,
)


@dataclass(frozen=True, slots=True)
class IdoxCouncilConfig:
    authority: str
    base_url: str
    application_root: str = "/online-applications/"


class IdoxPublicAccessScraper(PlanningScraper):
    """Scraper for councils using Idox PublicAccess planning portals."""

    _label_map = {
        "reference": "reference",
        "caseno": "reference",
        "case_no": "reference",
        "application_reference": "reference",
        "planning_reference": "reference",
        "application_number": "reference",
        "address": "address",
        "site_address": "address",
        "location": "address",
        "proposal": "description",
        "description": "description",
        "development_description": "description",
        "status": "status",
        "application_status": "status",
        "decision": "decision",
        "date_received": "date_received",
        "received": "date_received",
        "application_received": "date_received",
        "valid_date": "date_validated",
        "date_valid": "date_validated",
        "date_validated": "date_validated",
        "validated": "date_validated",
        "application_validated": "date_validated",
        "applicant_name": "applicant_name",
        "applicant": "applicant_name",
        "agent_name": "agent_name",
        "agent": "agent_name",
        "case_officer": "case_officer",
        "officer": "case_officer",
        "ward": "ward",
        "parish": "parish",
    }

    def __init__(
        self,
        config: IdoxCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient()

    def discover_ids(
        self,
        *,
        listing_url: str | None = None,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> DiscoveryResult:
        if listing_url:
            response = self.http.get(listing_url)
        else:
            response = self._fetch_weekly_list(start_date=start_date, end_date=end_date)
        applications = self.parse_listing(response.text, response.url)
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(
            authority=self.authority,
            source_url=response.url,
            applications=applications,
        )

    def fetch_application(self, uid: str, url: str | None = None) -> PlanningApplication:
        detail_url = url or self.build_detail_url(uid)
        response = self.http.get(detail_url)
        return self.parse_detail(response.text, response.url, fallback_uid=uid)

    def build_weekly_list_url(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> str:
        base = self._portal_url("search.do")
        params = {"action": "weeklyList"}
        if start_date:
            params["dateStart"] = start_date.strftime("%d/%m/%Y")
        if end_date:
            params["dateEnd"] = end_date.strftime("%d/%m/%Y")
        return f"{base}?{urlencode(params)}"

    def build_detail_url(self, uid: str) -> str:
        return f"{self._portal_url('applicationDetails.do')}?{urlencode({'activeTab': 'summary', 'keyVal': uid})}"

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

            absolute_url = self._summary_url(page_url, uid)
            row_text = self._nearest_row_text(anchor)
            reference = self._extract_reference(anchor, row_text)
            address = self._extract_address(row_text, reference)

            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=absolute_url,
                    reference=reference,
                    address=address,
                    source_url=page_url,
                    raw={"listing_text": row_text} if row_text else {},
                )
            )

        if len(applications) == 1 and self._looks_like_application_summary(document):
            detail = self.parse_detail(html_text, page_url, fallback_uid=applications[0].uid)
            detail.url = applications[0].url
            detail.source_url = page_url
            return [detail]

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
        mapped: dict[str, str] = {}
        raw: dict[str, str] = {}

        for label, value in fields.items():
            raw[label] = value
            model_field = self._label_map.get(normalize_label(label))
            if model_field and value:
                mapped[model_field] = value

        for date_field in ("date_received", "date_validated"):
            if mapped.get(date_field):
                mapped[date_field] = parse_council_date(mapped[date_field]) or mapped[date_field]

        uid = (
            self._extract_uid(page_url)
            or self._raw_value(raw, "casetechnicalkey", "case_technical_key")
            or fallback_uid
            or mapped.get("reference")
        )
        if not uid:
            raise ValueError("Could not determine Idox application uid")

        address = mapped.get("address")
        description = mapped.get("description")
        postcode = extract_postcode(address, raw.get("postcode"))

        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=page_url,
            reference=mapped.get("reference"),
            address=address,
            description=description,
            status=mapped.get("status"),
            decision=mapped.get("decision"),
            date_received=mapped.get("date_received"),
            date_validated=mapped.get("date_validated"),
            applicant_name=mapped.get("applicant_name"),
            agent_name=mapped.get("agent_name"),
            case_officer=mapped.get("case_officer"),
            ward=mapped.get("ward"),
            parish=mapped.get("parish"),
            postcode=postcode,
            source_url=self.config.base_url,
            raw=raw,
        )

    def _fetch_weekly_list(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ):
        if start_date or end_date:
            response = self.http.get(self.build_weekly_list_url(start_date=start_date, end_date=end_date))
        else:
            response = self.http.get(self.build_weekly_list_url())

        document = html.fromstring(response.text)
        forms = document.xpath("//form[contains(@action, 'weeklyListResults.do')]")
        if not forms:
            return response

        form = forms[0]
        action = urljoin(response.url, form.get("action"))
        data = self._form_defaults(form)
        data.setdefault("searchType", "Application")
        data.setdefault("dateType", "DC_Validated")
        return self.http.post_form(action, data)

    def _form_defaults(self, form: html.HtmlElement) -> dict[str, str]:
        data: dict[str, str] = {}

        for input_node in form.xpath(".//input[@name]"):
            name = input_node.get("name")
            input_type = (input_node.get("type") or "text").lower()
            value = input_node.get("value") or ""
            if input_type in {"submit", "button", "image", "reset"}:
                continue
            if input_type in {"radio", "checkbox"}:
                if input_node.get("checked") is not None or name not in data:
                    data[name] = value
                continue
            data[name] = value

        for select in form.xpath(".//select[@name]"):
            name = select.get("name")
            options = select.xpath(".//option")
            selected = select.xpath(".//option[@selected]")
            chosen = selected[0] if selected else (options[0] if options else None)
            if chosen is not None:
                option_value = chosen.get("value")
                data[name] = (
                    option_value
                    if option_value is not None
                    else clean_text(" ".join(chosen.itertext())) or ""
                )

        return data

    def _portal_url(self, path: str) -> str:
        root = self.config.application_root.strip("/") + "/"
        return urljoin(self.config.base_url.rstrip("/") + "/", root + path)

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
        anchor_text = clean_text(" ".join(anchor.itertext()))
        if anchor_text and re.search(r"\d{2,4}[/.-][A-Z0-9/.-]+", anchor_text, flags=re.IGNORECASE):
            return anchor_text
        if row_text:
            match = re.search(r"\b\d{2,4}[/.-][A-Z0-9/.-]+\b", row_text, flags=re.IGNORECASE)
            if match:
                return match.group(0)
        return anchor_text

    def _extract_address(self, row_text: str | None, reference: str | None) -> str | None:
        if not row_text:
            return None
        text = row_text
        if reference:
            text = text.replace(reference, " ")
        text = re.sub(r"\b(Application|Reference|Validated|Received|Status)\b:?", " ", text, flags=re.IGNORECASE)
        return clean_text(text)

    def _extract_labelled_fields(self, document: html.HtmlElement) -> dict[str, str]:
        fields: dict[str, str] = {}

        for row in document.xpath("//tr[th and td]"):
            label = clean_text(" ".join(row.xpath("./th[1]//text()")))
            value = clean_text(" ".join(row.xpath("./td[1]//text()")))
            if label and value:
                fields[label] = value

        for container in document.xpath("//dl"):
            terms = container.xpath("./dt")
            for term in terms:
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
            input_type = (input_node.get("type") or "").lower()
            if input_type == "hidden":
                label = input_node.get("name")
                value = clean_text(input_node.get("value"))
                if label and value and not self._is_transient_form_field(label):
                    fields.setdefault(label, value)

        return fields

    def _raw_value(self, raw: dict[str, str], *normalized_labels: str) -> str | None:
        expected = set(normalized_labels)
        for label, value in raw.items():
            if normalize_label(label) in expected:
                return value
        return None

    def _looks_like_application_summary(self, document: html.HtmlElement) -> bool:
        page_text = clean_text(" ".join(document.xpath("//h1//text() | //title//text()"))) or ""
        return "application summary" in page_text.lower()

    def _is_transient_form_field(self, label: str) -> bool:
        normalized = normalize_label(label)
        return normalized.startswith("_") or "token" in normalized or normalized == "csrf"
