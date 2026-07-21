from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit, urlunsplit

from lxml import etree

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class AchieveFormsCouncilConfig:
    authority: str
    base_url: str


@dataclass(frozen=True, slots=True)
class AchieveFormsMetadata:
    listing_url: str
    form_uri: str
    form_id: str
    form_name: str
    weekly_lookup_id: str
    detail_lookup_id: str
    documents_lookup_id: str | None = None
    site: str = ""


class AchieveFormsPlanningScraper(PlanningScraper):
    """Scraper for Firmstep/AchieveForms planning-search forms."""

    def __init__(
        self,
        config: AchieveFormsCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient(timeout_seconds=30.0, min_delay_seconds=0.5)
        self._metadata_by_url: dict[str, AchieveFormsMetadata] = {}
        self._last_metadata: AchieveFormsMetadata | None = None
        self._auth_session: str | None = None

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        metadata = self._metadata(listing_url)
        weekly_rows = self._lookup_rows(metadata, metadata.weekly_lookup_id, {})
        applications: list[PlanningApplication] = []
        seen: set[str] = set()

        for row in weekly_rows:
            reference = self._first_value(row, "referenceNumber", "reference", "applicationReference")
            if not reference or reference in seen:
                continue
            seen.add(reference)
            try:
                application = self._fetch_application_with_metadata(reference, metadata)
            except Exception:
                application = self._stub_from_weekly_row(row, metadata)
            application_date = self._application_date(application)
            if application_date:
                if start_date and application_date < start_date:
                    continue
                if end_date and application_date > end_date:
                    continue
            applications.append(application)
            if limit is not None and len(applications) >= limit:
                break

        return DiscoveryResult(
            authority=self.authority,
            source_url=listing_url,
            applications=applications,
        )

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        metadata = self._last_metadata or self._metadata(url or self.config.base_url)
        application = self._fetch_application_with_metadata(uid, metadata)
        if include_documents:
            application.documents = self._fetch_documents(uid, metadata)
        return application

    def _metadata(self, listing_url: str) -> AchieveFormsMetadata:
        if listing_url in self._metadata_by_url:
            metadata = self._metadata_by_url[listing_url]
            self._last_metadata = metadata
            return metadata

        response = self.http.get(listing_url)
        form_uri = self._extract_form_uri(response.text, response.url)
        definition = self._fetch_definition(response.url, form_uri)
        metadata = self._metadata_from_definition(response.url, form_uri, definition)
        self._metadata_by_url[listing_url] = metadata
        self._last_metadata = metadata
        return metadata

    def _extract_form_uri(self, html_text: str, page_url: str) -> str:
        query_uri = parse_qs(urlsplit(page_url).query).get("form_uri")
        if query_uri and query_uri[0]:
            return query_uri[0]
        match = re.search(r'"publish-uri"\s*:\s*"([^"]+)"', html_text)
        if match:
            return json.loads(f'"{match.group(1)}"')
        match = re.search(r"form_uri=([^\"'&]+)", html_text)
        if match:
            return match.group(1)
        raise CouncilFetchError("Could not find AchieveForms form definition URI")

    def _fetch_definition(self, referer: str, form_uri: str) -> dict[str, Any]:
        response = self.http.get(
            urljoin(self.config.base_url, "/apibroker/"),
            params={"api": "getDocument", "uri": form_uri},
            headers={
                "Accept": "application/json, text/plain, */*",
                "Referer": referer,
            },
        )
        payload = self._json(response.text, response.url)
        content = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(content, str):
            raise CouncilFetchError("AchieveForms definition response did not include form content")
        try:
            return json.loads(base64.b64decode(content).decode("utf-8", errors="replace"))
        except (ValueError, TypeError) as exc:
            raise CouncilFetchError("Could not decode AchieveForms form definition") from exc

    def _metadata_from_definition(
        self,
        listing_url: str,
        form_uri: str,
        definition: dict[str, Any],
    ) -> AchieveFormsMetadata:
        lookup_by_name: dict[str, str] = {}
        for section in definition.get("sections") or []:
            if not isinstance(section, dict):
                continue
            for field in section.get("fields") or []:
                if not isinstance(field, dict):
                    continue
                props = field.get("props") if isinstance(field.get("props"), dict) else {}
                data_name = props.get("dataName")
                lookup_id = props.get("lookup")
                if isinstance(data_name, str) and isinstance(lookup_id, str) and lookup_id:
                    lookup_by_name[data_name] = lookup_id

        weekly_lookup_id = lookup_by_name.get("weeklyList")
        detail_lookup_id = lookup_by_name.get("planningResult")
        if not weekly_lookup_id or not detail_lookup_id:
            raise CouncilFetchError("AchieveForms planning lookups were not found in form definition")

        return AchieveFormsMetadata(
            listing_url=listing_url,
            form_uri=form_uri,
            form_id=str((definition.get("props") or {}).get("id") or ""),
            form_name=str(definition.get("formName") or "Search Planning Applications"),
            weekly_lookup_id=weekly_lookup_id,
            detail_lookup_id=detail_lookup_id,
            documents_lookup_id=lookup_by_name.get("docs"),
            site=urlsplit(self.config.base_url).hostname.split(".")[0] if urlsplit(self.config.base_url).hostname else "",
        )

    def _fetch_application_with_metadata(
        self,
        reference: str,
        metadata: AchieveFormsMetadata,
    ) -> PlanningApplication:
        rows = self._lookup_rows(metadata, metadata.detail_lookup_id, {"applicationReferenc": reference})
        if not rows:
            raise CouncilFetchError(f"No AchieveForms application found for {reference}")
        row = rows[0]
        app_reference = self._first_value(row, "referenceNumber") or reference
        address = self._first_value(row, "location")
        documents = self._fetch_documents(app_reference, metadata)
        return PlanningApplication(
            authority=self.authority,
            uid=app_reference,
            url=self._application_url(metadata, app_reference),
            reference=app_reference,
            address=address,
            description=self._first_value(row, "description"),
            status=self._first_value(row, "applicationStatus"),
            decision=self._first_value(row, "decision"),
            date_received=self._date_value(row, "dateReceived", "ReceivedDate"),
            date_validated=self._date_value(row, "dateAccepted", "Adate"),
            applicant_name=self._first_value(row, "applicant"),
            agent_name=self._first_value(row, "agent"),
            case_officer=self._first_value(row, "officer"),
            postcode=extract_postcode(address),
            source_url=metadata.listing_url,
            documents=documents,
            raw={
                "portal_family": "achieveforms",
                "detail_complete": True,
                "lookup_row": row,
            },
        )

    def _stub_from_weekly_row(
        self,
        row: dict[str, str],
        metadata: AchieveFormsMetadata,
    ) -> PlanningApplication:
        reference = self._first_value(row, "referenceNumber", "reference") or "unknown"
        address = self._first_value(row, "location")
        return PlanningApplication(
            authority=self.authority,
            uid=reference,
            url=self._application_url(metadata, reference),
            reference=reference,
            address=address,
            postcode=extract_postcode(address),
            source_url=metadata.listing_url,
            raw={"portal_family": "achieveforms", "lookup_row": row},
        )

    def _fetch_documents(
        self,
        reference: str,
        metadata: AchieveFormsMetadata,
    ) -> list[PlanningDocument]:
        if not metadata.documents_lookup_id:
            return []
        rows = self._lookup_rows(metadata, metadata.documents_lookup_id, {"selectedReference": reference})
        documents: list[PlanningDocument] = []
        for row in rows:
            url = self._first_value(row, "URL", "url")
            title = self._first_value(row, "display", "value") or "Document"
            if not url:
                continue
            documents.append(
                PlanningDocument(
                    title=title,
                    url=urljoin(self.config.base_url, url),
                    source_url=self._application_url(metadata, reference),
                )
            )
        return documents

    def _lookup_rows(
        self,
        metadata: AchieveFormsMetadata,
        lookup_id: str,
        tokens: dict[str, str],
    ) -> list[dict[str, str]]:
        payload = self._lookup_payload(metadata, tokens)
        response = self.http.post_json(
            self._lookup_url(lookup_id),
            payload,
        )
        data = self._json(response.text, response.url)
        xml_text = data.get("data") if isinstance(data, dict) else None
        if not isinstance(xml_text, str) or not xml_text.strip():
            return []
        return self._parse_lookup_xml(xml_text)

    def _lookup_url(self, lookup_id: str) -> str:
        sid = self._session_id()
        return urljoin(
            self.config.base_url,
            "/apibroker/?" + urlencode({"api": "RunLookup", "app_name": "AchieveForms", "sid": sid, "id": lookup_id}),
        )

    def _lookup_payload(
        self,
        metadata: AchieveFormsMetadata,
        tokens: dict[str, str],
    ) -> dict[str, Any]:
        parts = urlsplit(metadata.listing_url)
        return {
            "stopOnFailure": True,
            "user": {},
            "formId": metadata.form_id,
            "formValues": {"Search": {}},
            "isPublished": True,
            "formName": metadata.form_name,
            "tokens": {
                "port": "",
                "host": parts.netloc,
                "site_url": metadata.listing_url,
                "site_path": parts.path,
                "site_origin": f"{parts.scheme}://{parts.netloc}",
                "user_agent": getattr(self.http, "user_agent", ""),
                "site_protocol": f"{parts.scheme}:",
                "session_id": "",
                "product": "SELF",
                "formLanguage": "en",
                "authenticationType": "",
                "isAuthenticated": False,
                "api_url": "/apibroker/",
                "transactionReference": "",
                "transaction_status": "",
                "published": True,
                "timeZone": "Europe/London",
                **tokens,
            },
            "env_tokens": {"weburl": f"{parts.scheme}://{parts.netloc}"},
            "site": metadata.site,
            "created": "",
            "reference": "",
            "formUri": metadata.form_uri,
            "usePHPIntegrations": True,
        }

    def _session_id(self) -> str:
        if self._auth_session:
            return self._auth_session
        response = self.http.get(
            urljoin(self.config.base_url, "/authapi/isauthenticated"),
            headers={"Accept": "application/json, text/plain, */*"},
        )
        payload = self._json(response.text, response.url)
        session = payload.get("auth-session") if isinstance(payload, dict) else None
        if not isinstance(session, str) or not session:
            raise CouncilFetchError("Could not start AchieveForms session")
        self._auth_session = session
        return session

    def _parse_lookup_xml(self, xml_text: str) -> list[dict[str, str]]:
        try:
            root = etree.fromstring(xml_text.encode("utf-8"))
        except etree.XMLSyntaxError as exc:
            raise CouncilFetchError("AchieveForms lookup response was not valid XML") from exc
        rows: list[dict[str, str]] = []
        for row in root.xpath(".//Rows/Row"):
            values: dict[str, str] = {}
            for result in row.xpath("./result"):
                column = result.get("column")
                if column:
                    values[column] = clean_text(result.text or "") or ""
            if values:
                rows.append(values)
        return rows

    def _application_url(self, metadata: AchieveFormsMetadata, reference: str) -> str:
        parts = urlsplit(metadata.listing_url)
        query = parse_qs(parts.query)
        query["application_reference"] = [reference]
        return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query, doseq=True, safe=":/"), f"application-{quote(reference)}"))

    def _application_date(self, application: PlanningApplication) -> date | None:
        for value in (application.date_received, application.date_validated):
            if not value:
                continue
            try:
                return date.fromisoformat(value[:10])
            except ValueError:
                continue
        return None

    def _date_value(self, row: dict[str, str], *keys: str) -> str | None:
        value = self._first_value(row, *keys)
        if not value:
            return None
        return parse_council_date(value) or value

    def _first_value(self, row: dict[str, str], *keys: str) -> str | None:
        for key in keys:
            value = clean_text(row.get(key) or "")
            if value:
                return value
        return None

    def _json(self, text: str, url: str) -> Any:
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError(f"Invalid JSON from AchieveForms API at {url}") from exc
