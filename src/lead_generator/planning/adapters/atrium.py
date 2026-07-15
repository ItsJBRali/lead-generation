from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.generic import GenericCouncilConfig, GenericLabelledPlanningScraper
from lead_generator.planning.http import CouncilHttpClient, FetchResponse
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class AtriumCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "atrium"
    uid_query_params: tuple[str, ...] = (
        "id",
        "uid",
        "reference",
        "application",
        "applicationNumber",
        "applicationNo",
    )
    detail_markers: tuple[str, ...] = (
        "/planning/display/",
        "/search/details/",
        "planning/display",
    )


class AtriumPlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for DEF/Atrium planning-register search pages."""

    def __init__(
        self,
        config: AtriumCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config, http_client=http_client)

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **kwargs: object,
    ) -> DiscoveryResult:
        discovery = super().discover_ids(
            listing_url=listing_url,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            **kwargs,
        )
        for application in discovery.applications:
            date_inferred = not (application.date_received or application.date_validated)
            if date_inferred and start_date:
                application.date_validated = start_date.isoformat()
            application.raw = {
                **(application.raw or {}),
                "detail_complete": True,
                "date_range_filtered": bool(start_date or end_date),
                "date_inferred_from_search_window": date_inferred and start_date is not None,
            }
        return discovery

    def _fetch_listing(
        self,
        listing_url: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> FetchResponse:
        response = self.http.get(listing_url)
        if start_date or end_date:
            response = self._submit_date_search(response, start_date=start_date, end_date=end_date)
        response = self._expand_results_per_page(response)
        pages = [response.text]
        seen = {response.url}
        status_code = response.status_code
        final_url = response.url

        for page_url in self._pagination_urls(response.text, response.url)[:100]:
            if page_url in seen:
                continue
            seen.add(page_url)
            page = self.http.get(page_url)
            pages.append(page.text)
            status_code = page.status_code
            final_url = page.url

        if len(pages) == 1:
            return response
        return FetchResponse(
            url=final_url,
            status_code=status_code,
            text="<html><body>" + "\n".join(pages) + "</body></html>",
        )

    def _expand_results_per_page(self, response: FetchResponse) -> FetchResponse:
        document = html.fromstring(response.text)
        forms = document.xpath("//form[.//select[@name='resultsPerPage']]")
        if not forms:
            current_count = len(
                document.xpath(
                    "//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz'), '/planning/display')]"
                )
            )
            page_text = clean_text(" ".join(document.itertext())) or ""
            reported_counts = [
                int(value)
                for value in re.findall(r"\bPlanning\s*\((\d+)\)", page_text, re.IGNORECASE)
            ]
            has_more = any(count > current_count for count in reported_counts) or bool(
                document.xpath(
                    "//a[normalize-space(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', "
                    "'abcdefghijklmnopqrstuvwxyz'))='next']"
                )
            )
            if current_count and has_more:
                requested_count = min(max([250, *reported_counts]), 1000)
                action = urljoin(response.url, "/Search/ChangeResultsPerPage")
                return self.http.post_form(
                    action,
                    {"resultsPerPage": str(requested_count)},
                    headers={"Referer": response.url},
                )
            return response
        form = forms[0]
        values = [
            int(value)
            for value in form.xpath(".//select[@name='resultsPerPage']/option/@value")
            if value.isdigit()
        ]
        if not values:
            return response
        action = urljoin(response.url, form.get("action") or response.url)
        data = {"resultsPerPage": str(max(values))}
        headers = {"Referer": response.url}
        if (form.get("method") or "post").casefold() == "get":
            return self.http.get(action, data, headers=headers)
        return self.http.post_form(action, data, headers=headers)

    def _submit_date_search(
        self,
        response: FetchResponse,
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> FetchResponse:
        document = html.fromstring(response.text)
        form = self._date_search_form(document)
        if form is None:
            advanced_links = document.xpath(
                "//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '/search/advanced')]/@href"
            )
            if advanced_links:
                response = self.http.get(urljoin(response.url, advanced_links[0]))
                document = html.fromstring(response.text)
                form = self._date_search_form(document)
        if form is None:
            return response

        data = self._form_defaults(form)
        input_names = {
            (node.get("name") or "").casefold(): node.get("name") or ""
            for node in form.xpath(".//input[@name]")
        }
        from_field = input_names.get("datereceivedfrom") or input_names.get("datevalidfrom")
        to_field = input_names.get("datereceivedto") or input_names.get("datevalidto")
        if start_date and from_field:
            data[from_field] = start_date.strftime("%d/%m/%Y")
        if end_date and to_field:
            data[to_field] = end_date.strftime("%d/%m/%Y")

        categories = {
            "searchplanning": "true",
            "searchbuildingcontrol": "false",
            "searchenforcement": "false",
            "searchtreepreservationorders": "false",
            "searchappeals": "false",
        }
        for normalized_name, value in categories.items():
            actual_name = input_names.get(normalized_name)
            if actual_name:
                data[actual_name] = value
        if actual_name := input_names.get("outstanding"):
            data[actual_name] = data.get(actual_name) or "false"

        submit_nodes = form.xpath(
            ".//input[(@type='submit' or @type='image') and @name] | .//button[@name]"
        )
        search_submit = next(
            (
                node
                for node in submit_nodes
                if "search" in f"{node.get('name') or ''} {node.get('value') or ''} {' '.join(node.itertext())}".casefold()
            ),
            submit_nodes[0] if submit_nodes else None,
        )
        if search_submit is not None:
            submit_name = search_submit.get("name")
            if submit_name:
                data[submit_name] = search_submit.get("value") or clean_text(" ".join(search_submit.itertext())) or "Search"

        action = urljoin(response.url, form.get("action") or response.url)
        if (form.get("method") or "post").casefold() == "get":
            return self.http.get(action, data)
        return self.http.post_form(action, data)

    def _date_search_form(self, document: html.HtmlElement) -> html.HtmlElement | None:
        forms = document.xpath(
            "//form[.//input[@name='DateReceivedFrom'] or .//input[@name='DateReceivedTo'] "
            "or .//input[@name='DateValidFrom'] or .//input[@name='DateValidTo']]"
        )
        return forms[-1] if forms else None

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        applications: list[PlanningApplication] = []
        seen: set[str] = set()

        anchors = document.xpath(
            "//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '/planning/display')][@href]"
        )
        for anchor in anchors:
            href = anchor.get("href")
            uid = self._extract_uid(href)
            if not uid or uid.casefold() in seen:
                continue
            seen.add(uid.casefold())
            row = anchor.xpath("ancestor::tr[1]")
            cells = [clean_text(" ".join(cell.itertext())) or "" for cell in row[0].xpath("./td")] if row else []
            data = self._row_data_from_element(row[0], cells) if row else self._row_data(cells)
            container = self._result_container(anchor)
            container_text = clean_text(" ".join(container.itertext())) if container is not None else None
            reference = self._reference_for_result(anchor, container, uid)
            if not cells:
                data.update(self._card_data(anchor, container, uid))
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=urljoin(page_url, href),
                    reference=reference or data.get("reference") or uid,
                    address=data.get("address"),
                    description=data.get("description"),
                    status=data.get("status"),
                    decision=data.get("decision"),
                    date_validated=data.get("date_validated"),
                    postcode=extract_postcode(data.get("address")),
                    source_url=page_url,
                    raw={
                        "portal_family": self.config.family,
                        "listing_text": container_text,
                    },
                )
            )

        if applications:
            return applications
        return super().parse_listing(html_text, page_url)

    def _row_data_from_element(self, row: html.HtmlElement, cells: list[str]) -> dict[str, str]:
        labelled: dict[str, str] = {}
        for cell in row.xpath("./td"):
            headings = cell.xpath(
                ".//*[contains(concat(' ', normalize-space(@class), ' '), ' mobile-heading ')]"
            )
            if not headings:
                continue
            heading = clean_text(" ".join(headings[0].itertext())) or ""
            value_parts = [
                clean_text(text)
                for text in cell.xpath(".//text()[not(ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' mobile-heading ')])] ")
            ]
            value = clean_text(" ".join(part for part in value_parts if part))
            normalized = re.sub(r"[^a-z]+", " ", heading.casefold()).strip()
            if value:
                labelled[normalized] = value
        if not labelled:
            return self._row_data(cells)

        data: dict[str, str] = {}
        for label, value in labelled.items():
            if "application" in label and ("no" in label or "number" in label):
                data["reference"] = value
            elif "location" in label or "address" in label:
                data["address"] = value
            elif "proposal" in label or "description" in label:
                data["description"] = value
            elif "status" in label:
                data["status"] = value
            elif "date" in label:
                parsed = parse_council_date(value)
                if parsed:
                    data["date_validated"] = parsed
        return data

    def _result_container(self, anchor: html.HtmlElement) -> html.HtmlElement:
        candidates = anchor.xpath(
            "ancestor::tr[1] | ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' results__item ')][1] "
            "| ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' searchResultsCardRow ')][1]"
        )
        return candidates[0] if candidates else anchor

    def _reference_for_result(
        self,
        anchor: html.HtmlElement,
        container: html.HtmlElement | None,
        uid: str,
    ) -> str:
        candidates = [clean_text(" ".join(anchor.itertext()))]
        if container is not None:
            candidates.extend(clean_text(" ".join(node.itertext())) for node in container.xpath(".//h1 | .//h2 | .//h3"))
        for candidate in candidates:
            if not candidate:
                continue
            candidate = re.sub(r"^Application\s+(?:No\.?|Number)\s*[:.]?\s*", "", candidate, flags=re.IGNORECASE)
            if candidate.casefold() == uid.casefold() or ("/" in candidate and len(candidate) < 80):
                return candidate
        return uid

    def _card_data(
        self,
        anchor: html.HtmlElement,
        container: html.HtmlElement | None,
        uid: str,
    ) -> dict[str, str]:
        if container is None:
            return {}
        data: dict[str, str] = {}
        anchor_text = clean_text(" ".join(anchor.itertext()))
        if anchor_text and anchor_text.casefold() != uid.casefold():
            data["address"] = anchor_text
        text = clean_text(" ".join(container.itertext())) or ""
        for value in re.findall(r"\b\d{1,2}/\d{1,2}/\d{4}\b", text):
            parsed = parse_council_date(value)
            if parsed:
                data.setdefault("date_validated", parsed)
        return data

    def _pagination_urls(self, html_text: str, page_url: str) -> list[str]:
        document = html.fromstring(html_text)
        urls: list[str] = []
        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            text = clean_text(" ".join(anchor.itertext())) or ""
            lowered = href.casefold()
            if "/search/resultspage/" not in lowered:
                continue
            if text and not (text.isdigit() or text.casefold() in {"next", "last"}):
                continue
            urls.append(urljoin(page_url, href))
        return urls

    def _row_data(self, cells: list[str]) -> dict[str, str]:
        data: dict[str, str] = {}
        if cells:
            data["reference"] = cells[0]
        if len(cells) > 2:
            data["address"] = cells[2]
        if len(cells) > 3:
            data["description"] = cells[3]
        if len(cells) > 4:
            parsed_date = parse_council_date(cells[4])
            if parsed_date:
                data["date_validated"] = parsed_date
        if len(cells) > 6:
            status_decision = cells[6]
            if " - " in status_decision:
                status, decision = [part.strip() for part in status_decision.split(" - ", 1)]
                data["status"] = status
                data["decision"] = decision
            else:
                data["status"] = status_decision
        return {key: value for key, value in data.items() if value}

    def _extract_uid(self, url_or_href: str | None) -> str | None:
        if not url_or_href:
            return None
        parts = urlsplit(url_or_href)
        query = parse_qs(parts.query)
        for param in self.config.uid_query_params:
            value = query.get(param)
            if value and value[0]:
                return value[0]
        match = re.search(r"/Planning/Display/(.+?)/?$", parts.path, flags=re.IGNORECASE)
        if match:
            return unquote(match.group(1)).strip("/")
        return super()._extract_uid(url_or_href)
