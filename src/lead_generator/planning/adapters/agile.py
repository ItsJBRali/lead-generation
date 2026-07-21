from __future__ import annotations

import json
import http.client
import ssl
from dataclasses import dataclass
from datetime import date, datetime, time
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlsplit
from zoneinfo import ZoneInfo

from lxml import html

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient, FetchResponse
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class AgileCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "agile"
    uid_query_params: tuple[str, ...] = (
        "theApnID",
        "theApnId",
        "apnID",
        "appID",
        "appId",
        "id",
        "reference",
        "appNo",
    )
    detail_markers: tuple[str, ...] = (
        "wphappdetail.displayurl",
        "wphappcriteria.display",
        "/apas/run/",
        "appdetail",
        "planning",
    )


class AgilePlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for Agile Applications / APAS planning pages."""

    IDENTITY_URL = "https://identity.agileapplications.co.uk/api/client/get"
    API_URL = "https://planningapi.agileapplications.co.uk//api/"
    PORTAL_URL = "https://planning.agileapplications.co.uk/"
    UK_TZ = ZoneInfo("Europe/London")

    def __init__(
        self,
        config: AgileCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config, http_client=http_client)
        self._client_code: str | None = None
        self._client_slug: str | None = None

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        if not self._is_modern_agile_url(listing_url):
            return super().discover_ids(
                listing_url=listing_url,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
        slug = self._slug_from_url(listing_url)
        client_code = self._lookup_client_code(slug)
        params = self._search_params(start_date=start_date, end_date=end_date)
        response_text, response_url = self._api_get("application/search", params, client_code)
        payload = self._json(response_text, response_url)
        records = payload.get("results") if isinstance(payload, dict) else payload
        if not isinstance(records, list):
            records = []

        applications = [
            self._application_from_record(record, listing_url, slug, client_code)
            for record in records
            if isinstance(record, dict)
        ]
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(
            authority=self.authority,
            source_url=response_url,
            applications=applications,
        )

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if url and not self._is_modern_agile_url(url):
            return super().fetch_application(uid, url, include_documents=include_documents)
        slug = self._client_slug or self._slug_from_url(url or self.config.base_url)
        client_code = self._client_code or self._lookup_client_code(slug)
        response_text, response_url = self._api_get(f"application/{quote(str(uid), safe='')}", {}, client_code)
        payload = self._json(response_text, response_url)
        if not isinstance(payload, dict):
            raise ValueError("Agile application detail response was not a JSON object")
        application = self._application_from_record(payload, url or self.config.base_url, slug, client_code)
        application.raw = {**application.raw, "detail_complete": True}
        if include_documents:
            application.documents = self.fetch_documents(uid, client_code=client_code)
        return application

    def fetch_documents(self, uid: str, *, client_code: str | None = None) -> list[PlanningDocument]:
        slug = self._client_slug or self._slug_from_url(self.config.base_url)
        code = client_code or self._client_code or self._lookup_client_code(slug)
        response_text, response_url = self._api_get(
            f"application/{quote(str(uid), safe='')}/document",
            {},
            code,
        )
        payload = self._json(response_text, response_url)
        if not isinstance(payload, list):
            return []
        documents: list[PlanningDocument] = []
        for record in payload:
            if not isinstance(record, dict) or not record.get("documentHash"):
                continue
            name = self._first_value(record, "name", "description", "mediaDescription") or "Document"
            document_hash = quote(str(record["documentHash"]), safe="")
            documents.append(
                PlanningDocument(
                    title=name,
                    url=f"{self.API_URL}application/document/{quote(code, safe='')}/{document_hash}",
                    document_type=self._first_value(record, "mediaDescription"),
                    date_published=self._date_value(record, "receivedDate"),
                    description=self._first_value(record, "description"),
                )
            )
        return documents

    def parse_listing(self, html_text: str, page_url: str) -> list[PlanningApplication]:
        applications = super().parse_listing(html_text, page_url)
        for application in applications:
            if application.reference and parse_council_date(application.reference) and application.uid:
                application.reference = application.uid
        return applications

    def _fetch_listing(
        self,
        listing_url: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> FetchResponse:
        response = self.http.get(listing_url)
        if not (start_date or end_date):
            return response
        document = html.fromstring(response.text)
        forms = document.xpath(
            "//form[.//input[starts-with(@name, 'REGFROMDATE.')] or .//input[starts-with(@name, 'REGTODATE.')]]"
        )
        if not forms:
            return super()._fetch_listing(listing_url, start_date=start_date, end_date=end_date)
        form = forms[0]
        data = self._form_defaults(form)
        for key in list(data):
            if key.startswith("REGFROMDATE.") and start_date:
                data[key] = start_date.strftime("%d/%m/%Y")
            elif key.startswith("REGTODATE.") and end_date:
                data[key] = end_date.strftime("%d/%m/%Y")
        submit_name, submit_value = self._last_submit(form)
        if submit_name:
            data[submit_name] = submit_value or "Search"
        action = form.get("action") or listing_url
        search_response = self.http.post_form(urljoin(response.url, action), data)
        return self._with_legacy_apas_pages(search_response)

    def _lookup_client_code(self, slug: str) -> str:
        if self._client_code and self._client_slug == slug:
            return self._client_code
        response = self.http.get(
            self.IDENTITY_URL,
            params={"url": slug},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": self.PORTAL_URL,
            },
        )
        payload = self._json(response.text, response.url)
        if not isinstance(payload, dict) or not payload.get("code"):
            raise CouncilFetchError(f"Could not resolve Agile client code for {slug}")
        self._client_slug = slug
        self._client_code = str(payload["code"])
        return self._client_code

    def _search_params(
        self,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, str]:
        params = {"status": "registered"}
        if start_date:
            params["registrationDateFrom"] = self._agile_datetime(start_date, time(0, 0, 0))
        if end_date:
            params["registrationDateTo"] = self._agile_datetime(end_date, time(23, 59, 59))
        return params

    def _agile_datetime(self, value: date, day_time: time) -> str:
        return datetime.combine(value, day_time, tzinfo=self.UK_TZ).isoformat()

    def _api_headers(self, client_code: str) -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "en",
            "referer": self.PORTAL_URL,
            "user-agent": getattr(self.http, "user_agent", "Mozilla/5.0 LeadGeneratorPlanningScraper/0.1"),
            "x-client": client_code,
            "x-service": "PA",
            "x-product": "CITIZENPORTAL",
        }

    def _last_submit(self, form: html.HtmlElement) -> tuple[str | None, str | None]:
        submits = form.xpath(".//input[@type='submit' and @name]")
        if not submits:
            return None, None
        submit = submits[-1]
        return submit.get("name"), submit.get("value")

    def _extract_reference(self, anchor: html.HtmlElement, row_text: str | None) -> str | None:
        anchor_text = clean_text(" ".join(anchor.itertext()))
        if anchor_text and not parse_council_date(anchor_text):
            return anchor_text
        return super()._extract_reference(anchor, row_text)

    def _with_legacy_apas_pages(self, response: FetchResponse) -> FetchResponse:
        document = html.fromstring(response.text)
        page_urls: list[str] = []
        seen = {response.url}
        for anchor in document.xpath("//a[@href]"):
            href = anchor.get("href") or ""
            lowered = href.casefold()
            if "wphappsearchres.displayresultsurl" not in lowered or "startindex=" not in lowered:
                continue
            page_url = urljoin(response.url, href)
            if page_url in seen:
                continue
            seen.add(page_url)
            page_urls.append(page_url)

        pages = [response.text]
        status_code = response.status_code
        final_url = response.url
        for page_url in page_urls[:50]:
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

    def _api_get(self, path: str, params: dict[str, str], client_code: str) -> tuple[str, str]:
        if not isinstance(self.http, CouncilHttpClient):
            response = self.http.get(f"{self.API_URL}{path.lstrip('/')}", params=params, headers=self._api_headers(client_code))
            return response.text, response.url

        base = urlsplit(self.API_URL)
        query = urlencode(params, safe=":")
        request_path = f"{base.path.rstrip('/')}/{path.lstrip('/')}"
        if query:
            request_path = f"{request_path}?{query}"
        response_url = f"{base.scheme}://{base.netloc}{request_path}"

        verify_options = (self.http.verify_tls, False) if self.http.verify_tls else (False,)
        for verify_tls in verify_options:
            context = ssl.create_default_context() if verify_tls else ssl._create_unverified_context()
            connection = http.client.HTTPSConnection(
                base.netloc,
                timeout=self.http.timeout_seconds,
                context=context,
            )
            try:
                connection.putrequest("GET", request_path, skip_accept_encoding=True)
                for key, value in self._api_headers(client_code).items():
                    connection.putheader(key, value)
                connection.endheaders()
                response = connection.getresponse()
                body = response.read()
                text = body.decode(response.headers.get_content_charset() or "utf-8", errors="replace")
                if response.status >= 400:
                    raise CouncilFetchError(f"HTTP {response.status} while fetching {response_url}")
                return text, response_url
            except ssl.SSLCertVerificationError:
                if not verify_tls:
                    raise
            finally:
                connection.close()
        raise CouncilFetchError(f"Could not establish a secure connection to {response_url}")

    def _application_from_record(
        self,
        record: dict[str, Any],
        source_url: str,
        slug: str,
        client_code: str,
    ) -> PlanningApplication:
        uid = self._first_value(record, "id", "applicationId", "applicationID")
        reference = self._first_value(record, "reference", "fullReference", "webReference")
        if not uid:
            uid = reference
        if not uid:
            raise ValueError("Could not determine Agile application uid")

        address = self._first_value(record, "location", "address", "siteAddress")
        date_received = self._date_value(record, "registrationDate", "receivedDate", "applicationDate")
        date_validated = self._date_value(record, "validDate")
        decision = self._first_value(record, "decisionText", "decision")
        status = self._first_value(record, "status", "statusText")
        if not status and decision:
            status = decision

        url = f"{self.PORTAL_URL}{slug}/application-details/{quote(str(uid), safe='')}"
        return PlanningApplication(
            authority=self.authority,
            uid=str(uid),
            url=url,
            reference=reference,
            address=address,
            description=self._first_value(record, "proposal", "description"),
            status=status,
            decision=decision,
            date_received=date_received,
            date_validated=date_validated,
            applicant_name=self._first_value(record, "applicantSurname", "applicantName"),
            agent_name=self._first_value(record, "agentName"),
            ward=self._first_value(record, "ward"),
            parish=self._first_value(record, "parish"),
            postcode=extract_postcode(address),
            source_url=source_url,
            raw={
                "portal_family": self.config.family,
                "api": "agile_application_search",
                "client_code": client_code,
                "detail_complete": True,
                "record": record,
            },
        )

    def _date_value(self, record: dict[str, Any], *keys: str) -> str | None:
        value = self._first_value(record, *keys)
        if not value:
            return None
        return parse_council_date(value.split("T", 1)[0]) or value

    def _first_value(self, record: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = record.get(key)
            if value is None:
                continue
            cleaned = clean_text(str(value))
            if cleaned:
                return cleaned
        return None

    def _json(self, text: str, url: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError(f"Invalid JSON from Agile API at {url}") from exc

    def _is_modern_agile_url(self, url: str) -> bool:
        return urlsplit(url).netloc.casefold().endswith("planning.agileapplications.co.uk")

    def _slug_from_url(self, url: str) -> str:
        parts = urlsplit(url)
        path_parts = [part for part in parts.path.split("/") if part]
        if parts.netloc.endswith("planning.agileapplications.co.uk") and path_parts:
            return path_parts[0]
        if parts.netloc:
            host_part = parts.netloc.split(".")[0]
            if host_part and host_part not in {"www", "planning"}:
                return host_part
        raise ValueError(f"Could not determine Agile council slug from {url}")
