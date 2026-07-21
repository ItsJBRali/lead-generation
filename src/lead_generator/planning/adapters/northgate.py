from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from lxml import html

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)
from lead_generator.planning.http import (
    CouncilBrowserClient,
    CouncilFetchError,
    browser_fallback_recommended,
)
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text


NORTHGATE_PREFIXED_REFERENCE_RE = re.compile(
    r"\b[A-Z]{1,12}[./-]\d{2,4}[/.-][A-Z0-9/.-]+\b",
    flags=re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class NorthgateCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "northgate"
    uid_query_params: tuple[str, ...] = (
        "PARAM0",
        "param0",
        "KEYVAL",
        "keyVal",
        "id",
        "AppNo",
        "appNo",
        "reference",
    )
    detail_markers: tuple[str, ...] = (
        "stddetails.aspx",
        "planningpk.xml",
        "planningexplorer",
        "applicationdetails",
        "detail",
    )


class NorthgatePlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for Northgate Planning Explorer pages."""

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        try:
            return self._discover_ids(
                listing_url=listing_url,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
        except CouncilFetchError as exc:
            if not browser_fallback_recommended(exc) or isinstance(self.http, CouncilBrowserClient):
                raise
            primary_error = exc
            browser = CouncilBrowserClient()
            self.http = browser
            try:
                return self._discover_ids(
                    listing_url=listing_url,
                    start_date=start_date,
                    end_date=end_date,
                    limit=limit,
                )
            except Exception as browser_error:
                browser.close()
                browser_error.add_note(f"Direct portal request also failed: {primary_error}")
                raise

    def _discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> DiscoveryResult:
        first_page = self._fetch_listing(listing_url, start_date=start_date, end_date=end_date)
        result_total = self._result_total(first_page.text)
        pending = [first_page]
        queued_urls = {self._canonical_url(first_page.url)}
        seen_uids: set[str] = set()
        applications: list[PlanningApplication] = []

        while pending and len(queued_urls) <= 500:
            page = pending.pop(0)
            for application in self.parse_listing(page.text, page.url):
                if application.uid in seen_uids:
                    continue
                seen_uids.add(application.uid)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return DiscoveryResult(
                        authority=self.authority,
                        source_url=first_page.url,
                        applications=self._mark_search_window(
                            applications[:limit],
                            start_date,
                            end_date,
                        ),
                    )
            if result_total is not None and len(applications) >= result_total:
                break

            for page_url in self._pagination_urls(page.text, page.url):
                canonical_url = self._canonical_url(page_url)
                if canonical_url in queued_urls:
                    continue
                queued_urls.add(canonical_url)
                pending.append(self.http.get(page_url, headers={"Referer": page.url}))

        return DiscoveryResult(
            authority=self.authority,
            source_url=first_page.url,
            applications=self._mark_search_window(
                applications[:result_total] if result_total is not None else applications,
                start_date,
                end_date,
            ),
        )

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        applications = super().parse_listing(html_text, page_url)
        for application in applications:
            application.url = self._clean_href(application.url)
        return applications

    def _fetch_listing(self, listing_url: str, *, start_date: date | None = None, end_date: date | None = None):
        landing_page = self.http.get(listing_url)
        if not (start_date or end_date):
            return landing_page

        document = html.fromstring(landing_page.text)
        forms = document.xpath("//form[.//input[@name='dateStart'] and .//input[@name='dateEnd']]")
        if not forms:
            return super()._fetch_listing(listing_url, start_date=start_date, end_date=end_date)

        form = forms[-1]
        data = self._form_defaults(form)
        data["cboSelectDateValue"] = "DATE_RECEIVED"
        data["rbGroup"] = "rbRange"
        date_format = self._date_format(landing_page.text)
        if start_date:
            data["dateStart"] = start_date.strftime(date_format)
        if end_date:
            data["dateEnd"] = end_date.strftime(date_format)
        data["edrDateSelection"] = data.get("edrDateSelection", "")
        data["csbtnSearch"] = "Search"

        action = urljoin(landing_page.url, form.get("action") or listing_url)
        parts = urlsplit(landing_page.url)
        response = self.http.post_form(
            action,
            data,
            headers={
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Origin": f"{parts.scheme}://{parts.netloc}",
                "Referer": landing_page.url,
            },
        )
        if "generalsearch.aspx" in urlsplit(response.url).path.casefold():
            result_document = html.fromstring(response.text)
            if result_document.xpath("//form[.//input[@name='dateStart'] and .//input[@name='dateEnd']]"):
                raise CouncilFetchError(f"Northgate date search was not accepted by {self.authority}")
        return response

    def _date_format(self, html_text: str) -> str:
        visible_text = clean_text(" ".join(html.fromstring(html_text).xpath("//body//text()"))) or ""
        if re.search(r"format\s+DD-MM-YYYY", visible_text, flags=re.IGNORECASE):
            return "%d-%m-%Y"
        return "%d/%m/%Y"

    def _result_total(self, html_text: str) -> int | None:
        visible_text = clean_text(" ".join(html.fromstring(html_text).xpath("//body//text()"))) or ""
        match = re.search(r"\bRecords\s+\d+\s+to\s+\d+\s+of\s+(\d+)\b", visible_text, flags=re.IGNORECASE)
        return int(match.group(1)) if match else None

    def _mark_search_window(
        self,
        applications: list[PlanningApplication],
        start_date: date | None,
        end_date: date | None,
    ) -> list[PlanningApplication]:
        inferred_date = start_date or end_date
        for application in applications:
            inferred = bool(inferred_date and not (application.date_received or application.date_validated))
            if inferred:
                application.date_received = inferred_date.isoformat()
            application.raw = {
                **(application.raw or {}),
                "date_range_filtered": bool(start_date or end_date),
                "date_inferred_from_search_window": inferred,
            }
        return applications

    def _looks_like_application_link(self, href: str | None, anchor: html.HtmlElement) -> bool:
        if not href or href.startswith("#") or not self._extract_uid(href):
            return False
        lowered = href.casefold()
        return any(
            marker in lowered
            for marker in ("stddetails.aspx", "planningpk.xml", "applicationdetails", "/detail")
        )

    def _extract_reference(self, anchor: html.HtmlElement, row_text: str | None) -> str | None:
        anchor_text = clean_text(" ".join(anchor.itertext())) or ""
        prefixed_match = NORTHGATE_PREFIXED_REFERENCE_RE.search(anchor_text)
        if prefixed_match:
            return prefixed_match.group(0)
        return super()._extract_reference(anchor, row_text) or self._extract_uid(anchor.get("href"))

    def _pagination_urls(self, html_text: str, page_url: str) -> list[str]:
        document = html.fromstring(html_text)
        urls: list[str] = []
        seen: set[str] = set()
        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            if "stdresults.aspx" not in href.casefold():
                continue
            absolute_url = self._clean_href(urljoin(page_url, href))
            query = dict(parse_qsl(urlsplit(absolute_url).query, keep_blank_values=True))
            if "p" not in {key.casefold() for key in query}:
                continue
            canonical_url = self._canonical_url(absolute_url)
            if canonical_url not in seen:
                seen.add(canonical_url)
                urls.append(absolute_url)
        return urls

    def _clean_href(self, value: str) -> str:
        value = re.sub(r"[\r\n\t]+", "", value).strip()
        parts = urlsplit(value)
        query = urlencode(
            [(key.strip(), item.strip()) for key, item in parse_qsl(parts.query, keep_blank_values=True)]
        )
        return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))

    def _canonical_url(self, value: str) -> str:
        return self._clean_href(value).casefold()

