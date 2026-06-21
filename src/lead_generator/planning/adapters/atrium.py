from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, urljoin, urlsplit

from lxml import html

from lead_generator.planning.adapters.generic import GenericCouncilConfig, GenericLabelledPlanningScraper
from lead_generator.planning.http import CouncilHttpClient, FetchResponse
from lead_generator.planning.models import PlanningApplication
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

    def _fetch_listing(
        self,
        listing_url: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> FetchResponse:
        response = super()._fetch_listing(listing_url, start_date=start_date, end_date=end_date)
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

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        document = html.fromstring(html_text)
        applications: list[PlanningApplication] = []
        seen: set[str] = set()

        for row in document.xpath("//tr[.//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '/planning/display/')]]"):
            cells = [clean_text(" ".join(cell.itertext())) or "" for cell in row.xpath("./td")]
            anchor = row.xpath(".//a[contains(translate(@href, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), '/planning/display/')][1]")
            if not anchor:
                continue
            href = anchor[0].get("href")
            uid = self._extract_uid(href) or clean_text(" ".join(anchor[0].itertext()))
            if not uid or uid in seen:
                continue
            seen.add(uid)
            data = self._row_data(cells)
            applications.append(
                PlanningApplication(
                    authority=self.authority,
                    uid=uid,
                    url=urljoin(page_url, href),
                    reference=data.get("reference") or uid,
                    address=data.get("address"),
                    description=data.get("description"),
                    status=data.get("status"),
                    decision=data.get("decision"),
                    date_validated=data.get("date_validated"),
                    postcode=extract_postcode(data.get("address")),
                    source_url=page_url,
                    raw={
                        "portal_family": self.config.family,
                        "listing_text": clean_text(" ".join(row.itertext())),
                    },
                )
            )

        if applications:
            return applications
        return super().parse_listing(html_text, page_url)

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
        match = re.search(r"/Planning/Display/([^/?#]+)", parts.path, flags=re.IGNORECASE)
        if match:
            return match.group(1)
        return super()._extract_uid(url_or_href)
