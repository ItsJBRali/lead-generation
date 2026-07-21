from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlsplit

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)
from lead_generator.planning.http import CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class CivicaCouncilConfig(GenericCouncilConfig):
    authority: str
    base_url: str
    family: str = "civica"
    uid_query_params: tuple[str, ...] = (
        "REFVAL",
        "refval",
        "reference",
        "ApplicationNumber",
        "applicationNumber",
        "caseNo",
        "case",
        "id",
    )
    detail_markers: tuple[str, ...] = (
        "planningexplorer",
        "planningdetails",
        "applicationdetails",
        "apa",
        "detail",
    )


class CivicaPlanningScraper(GenericLabelledPlanningScraper):
    """Scraper for Civica / Authority Public Access planning pages."""

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **kwargs: object,
    ) -> DiscoveryResult:
        response = self.http.get(listing_url)
        api_url = self._extract_civica_api_url(response.text, response.url)
        ref_type = self._extract_planning_ref_type(response.text)
        if api_url and ref_type:
            applications = self._discover_json_applications(
                api_url,
                response.url,
                response.text,
                ref_type,
                start_date=start_date,
                end_date=end_date,
                limit=limit,
            )
            return DiscoveryResult(authority=self.authority, source_url=response.url, applications=applications)
        return super().discover_ids(
            listing_url=listing_url,
            start_date=start_date,
            end_date=end_date,
            limit=limit,
            **kwargs,
        )

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if url and self._looks_like_keyobject_viewer(url):
            application = self._fetch_keyobject_application(uid, url)
            if include_documents:
                application.documents = fetch_civica_documents_from_raw(application.raw, source_url=application.url)
            return application
        if url and self._looks_like_webforms_detail(url):
            return self._fetch_webforms_application(uid, url)
        return super().fetch_application(uid, url, include_documents=include_documents)

    def _discover_json_applications(
        self,
        api_url: str,
        page_url: str,
        html_text: str,
        ref_type: str,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
    ) -> list[PlanningApplication]:
        applications: list[PlanningApplication] = []
        seen: set[str] = set()
        page_size = min(limit or 100, 100)
        from_row = 1
        total_rows: int | None = None
        while True:
            to_row = from_row + page_size - 1
            payload: dict[str, object] = {
                "refType": ref_type,
                "fromRow": from_row,
                "toRow": to_row,
                "searchFields": self._json_search_fields(api_url, ref_type, start_date=start_date, end_date=end_date),
                "NoTotalRows": False,
            }
            response = self.http.post_json(urljoin(api_url, "keyobject/pagedsearch"), payload)
            data = json.loads(response.text)
            records = data.get("KeyObjects") or []
            if total_rows is None and data.get("TotalRows") is not None:
                try:
                    total_rows = int(data.get("TotalRows"))
                except (TypeError, ValueError):
                    total_rows = None
            for record in records:
                if not isinstance(record, dict):
                    continue
                application = self._application_from_keyobject(record, page_url, html_text, api_url, ref_type)
                key = application.reference or application.uid
                if key in seen:
                    continue
                seen.add(key)
                applications.append(application)
                if limit is not None and len(applications) >= limit:
                    return applications
            if not records:
                break
            if total_rows is not None and len(applications) >= total_rows:
                break
            if len(records) < page_size:
                break
            from_row += page_size
        return applications

    def _fetch_keyobject_application(self, uid: str, url: str) -> PlanningApplication:
        api_url = self._api_url_from_keyobject_url(url)
        ref_type = self._query_value(url, "RefType") or "GFPlanning"
        key_number = self._query_value(url, "KeyNo") or self._query_value(url, "KeyNumber") or uid
        key_text = self._query_value(url, "KeyText") or "Subject"
        response = self.http.post_json(
            urljoin(api_url, "keyobject/search"),
            {"refType": ref_type, "fromRow": 1, "toRow": 2, "keyNumb": key_number, "keyText": key_text},
        )
        data = json.loads(response.text)
        if not isinstance(data, list) or not data:
            raise ValueError(f"Could not fetch Civica key object {key_number}")
        return self._application_from_keyobject(data[0], url, "", api_url, ref_type)

    def _json_search_fields(
        self,
        api_url: str | None = None,
        ref_type: str | None = None,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
    ) -> dict[str, str]:
        from_field, to_field = self._preferred_date_search_fields(api_url, ref_type)
        fields: dict[str, str] = {}
        if start_date:
            fields[from_field] = start_date.strftime("%d/%m/%Y")
        if end_date:
            fields[to_field] = end_date.strftime("%d/%m/%Y")
        return fields

    def _preferred_date_search_fields(self, api_url: str | None, ref_type: str | None) -> tuple[str, str]:
        if not api_url or not ref_type:
            return ("SDate5From", "SDate5To")
        try:
            response = self.http.get(urljoin(api_url, f"keyobject/getsearchcriteria/{quote(ref_type)}?format=json"))
            criteria = json.loads(response.text)
        except Exception:
            return ("SDate5From", "SDate5To")
        pairs: dict[str, dict[str, str]] = {}
        labels: dict[str, str] = {}
        for item in criteria.get("SearchItems") or []:
            if not isinstance(item, dict):
                continue
            display = item.get("Display")
            if not isinstance(display, dict):
                continue
            field = self._string(display.get("FieldName"))
            label = self._string(display.get("Label")) or ""
            if not field or not field.endswith(("From", "To")):
                continue
            if item.get("DataType") != "D" and "date" not in f"{field} {label}".casefold():
                continue
            suffix = "from" if field.endswith("From") else "to"
            base = field.removesuffix("From").removesuffix("To")
            pairs.setdefault(base, {})[suffix] = field
            labels[base] = f"{labels.get(base, '')} {label}".casefold()
        for preferred in ("received", "valid", "validated", "submitted", "application"):
            for base, fields in pairs.items():
                if preferred in labels.get(base, "") and fields.get("from") and fields.get("to"):
                    return (fields["from"], fields["to"])
        for fields in pairs.values():
            if fields.get("from") and fields.get("to"):
                return (fields["from"], fields["to"])
        return ("SDate5From", "SDate5To")

    def _application_from_keyobject(
        self,
        record: dict[str, object],
        page_url: str,
        html_text: str,
        api_url: str,
        ref_type: str,
    ) -> PlanningApplication:
        fields = self._keyobject_fields(record)
        key_number = self._string(record.get("KeyNumber")) or fields.get("keynumber")
        key_text = self._string(record.get("KeyText")) or "Subject"
        reference = fields.get("sdescription") or fields.get("ref_no") or fields.get("reference no") or fields.get("reference number")
        address = (
            fields.get("address")
            or fields.get("applicationaddress")
            or fields.get("application_address")
            or fields.get("location")
            or fields.get("uprndisplay")
            or fields.get("stext9")
        )
        if not address:
            address = clean_text(
                " ".join(
                    value
                    for value in (
                        fields.get("atext1"),
                        fields.get("atext2"),
                        fields.get("atext3"),
                        fields.get("atext4"),
                        fields.get("atext5"),
                    )
                    if value
                )
            )
        url = self._keyobject_viewer_url(page_url, html_text, key_number, key_text, ref_type)
        raw = {
            "portal_family": self.config.family,
            "api": "civica_keyobject",
            "detail_complete": True,
            "civica_api_url": api_url,
            "key_number": key_number,
            "key_text": key_text,
            "ref_type": ref_type,
            "items": record.get("Items") or [],
        }
        received_date = fields.get("sdate5") or fields.get("received_date") or fields.get("received date")
        valid_date = fields.get("sdate1") or fields.get("valid_date") or fields.get("valid date")
        return PlanningApplication(
            authority=self.authority,
            uid=key_number or reference or "",
            url=url,
            reference=reference,
            address=address,
            description=fields.get("proposal") or fields.get("stext10") or self._string(record.get("Description")),
            status=fields.get("status") or fields.get("spicklist2") or fields.get("recommendation"),
            decision=fields.get("decision") or fields.get("decision_notice_type") or fields.get("spicklist2"),
            date_received=parse_council_date(received_date or valid_date),
            date_validated=parse_council_date(valid_date),
            applicant_name=fields.get("applicant name") or fields.get("applicantcontactnoname") or fields.get("stext1"),
            agent_name=fields.get("agent name") or fields.get("agentcontactnoname") or fields.get("stext2"),
            case_officer=fields.get("case officer") or fields.get("case_officer") or fields.get("spicklist3"),
            ward=fields.get("ward") or fields.get("apicklist2"),
            parish=fields.get("parish") or fields.get("apicklist3"),
            postcode=extract_postcode(fields.get("postcode") or fields.get("atext6"), address),
            source_url=self.config.base_url,
            raw=raw,
        )

    def _keyobject_fields(self, record: dict[str, object]) -> dict[str, str]:
        fields: dict[str, str] = {}
        for item in record.get("Items") or []:
            if not isinstance(item, dict):
                continue
            for key in (item.get("FieldName"), item.get("Label")):
                if not key:
                    continue
                value = self._string(item.get("Value"))
                if value is not None:
                    fields[str(key).casefold()] = value
        return fields

    def _extract_civica_api_url(self, html_text: str, page_url: str) -> str | None:
        match = re.search(r"Civica\.APIUrl\s*=\s*[\"']([^\"']+)[\"']", html_text)
        if match:
            return urljoin(page_url, match.group(1))
        return None

    def _extract_planning_ref_type(self, html_text: str) -> str | None:
        match = re.search(r'"PlanningApplicationRefType"\s*:\s*"([^"]+)"', html_text)
        if match:
            return match.group(1)
        match = re.search(r"RefType\s*:\s*[\"']([^\"']+)[\"']", html_text)
        if match:
            return match.group(1)
        return None

    def _keyobject_viewer_url(
        self,
        page_url: str,
        html_text: str,
        key_number: str | None,
        key_text: str | None,
        ref_type: str,
    ) -> str:
        match = re.search(r"Civica\.KeyObjectViewerUrl\s*=\s*[\"']([^\"']+)[\"']", html_text)
        viewer_path = match.group(1) if match else "/my-requests/keyobject-viewer/"
        query = urlencode({"KeyNo": key_number or "", "RefType": ref_type, "KeyText": key_text or "Subject"})
        return f"{urljoin(page_url, viewer_path)}?{query}"

    def _looks_like_keyobject_viewer(self, url: str) -> bool:
        lowered = url.casefold()
        return "keyobject-viewer" in lowered and ("keyno=" in lowered or "keynumber=" in lowered)

    def _api_url_from_keyobject_url(self, url: str) -> str:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}/w2webparts/Resource/Civica/Handler.ashx/"

    def _query_value(self, url: str, name: str) -> str | None:
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        for key, values in query.items():
            if key.casefold() == name.casefold() and values:
                return values[0]
        return None

    def _looks_like_webforms_detail(self, url: str) -> bool:
        lowered = url.lower()
        return "details.html" in lowered and "refval=" in lowered

    def _fetch_webforms_application(self, uid: str, url: str) -> PlanningApplication:
        reference = self._extract_uid(url) or uid
        api_url = self._resolve_webforms_api_url(url)
        response = self.http.post_json(api_url, reference)
        payload = json.loads(response.text)
        planning_data = payload.get("planningData") or {}
        if not planning_data:
            raise ValueError(f"Could not fetch Civica webforms planning data for {reference}")
        return self._parse_webforms_planning_data(planning_data, url, reference)

    def _resolve_webforms_api_url(self, detail_url: str) -> str:
        parsed = urlsplit(detail_url)
        detail_base = detail_url.rsplit("/", 1)[0] + "/"
        config_url = urljoin(detail_base, "config/planningDetails_config_live.js")
        config = self.http.get(config_url).text
        api_root = self._extract_js_string(config, "APIroot")
        planning_api = self._extract_js_string(config, "PlanningAPI")
        planning_data = self._extract_js_string(config, "PlanningData")
        if api_root and planning_api and planning_data:
            return urljoin(api_root, f"{planning_api}{planning_data}")
        return f"{parsed.scheme}://{parsed.netloc}/webapi/api/PlanningAPI/v2/planningdata/"

    def _extract_js_string(self, text: str, name: str) -> str | None:
        match = re.search(rf'var\s+{re.escape(name)}\s*=\s*"([^"]+)"', text)
        return match.group(1) if match else None

    def _parse_webforms_planning_data(
        self,
        data: dict[str, object],
        url: str,
        fallback_uid: str,
    ) -> PlanningApplication:
        reference = self._string(data.get("refval")) or fallback_uid
        address = self._string(data.get("addressline")) or self._string(data.get("address"))
        decision = self._string(data.get("decsn_text")) or self._string(data.get("dectype_text"))
        return PlanningApplication(
            authority=self.authority,
            uid=reference,
            url=url,
            reference=reference,
            address=address,
            description=self._string(data.get("proposal")),
            status=self._string(data.get("dcstat_text")),
            decision=decision,
            date_received=parse_council_date(self._string(data.get("dateaprecv_text"))),
            date_validated=parse_council_date(self._string(data.get("dateapval_text"))),
            applicant_name=self._string(data.get("appname")),
            agent_name=self._string(data.get("agtname")),
            case_officer=self._string(data.get("officer_name")),
            ward=self._string(data.get("ward_text")),
            parish=self._string(data.get("parish_text")),
            postcode=extract_postcode(address),
            source_url=self.config.base_url,
            raw={"portal_family": self.config.family, "api": "webforms_planningdata"},
        )

    def _string(self, value: object) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None


def fetch_civica_documents_from_raw(raw: dict[str, object], *, source_url: str | None = None) -> list[PlanningDocument]:
    api_url = raw.get("civica_api_url")
    key_number = raw.get("key_number")
    key_text = raw.get("key_text") or "Subject"
    ref_type = raw.get("ref_type") or "GFPlanning"
    if not api_url or not key_number:
        return []
    return fetch_civica_documents(str(api_url), str(key_number), str(key_text), str(ref_type), source_url=source_url)


def fetch_civica_documents(
    api_url: str,
    key_number: str,
    key_text: str,
    ref_type: str,
    *,
    source_url: str | None = None,
) -> list[PlanningDocument]:
    http = CouncilHttpClient()
    response = http.post_json(
        urljoin(api_url, "doc/list"),
        {
            "KeyNumb": key_number,
            "KeyText": key_text,
            "RefType": ref_type,
            "OrderBy": "DocDate Desc",
        },
    )
    payload = json.loads(response.text)
    documents: list[PlanningDocument] = []
    for record in payload.get("CompleteDocument") or []:
        if not isinstance(record, dict):
            continue
        doc_no = record.get("DocNo")
        if not doc_no:
            continue
        title = clean_text(str(record.get("Title") or record.get("DocDesc") or f"Document {doc_no}")) or f"Document {doc_no}"
        url = urljoin(api_url, f"Doc/pagestream?{urlencode({'cd': 'download', 'pdf': 'false', 'docno': str(doc_no), 'filename': title})}")
        documents.append(
            PlanningDocument(
                title=title,
                url=url,
                document_type=clean_text(str(record.get("DocDesc") or record.get("TypeCode") or "")),
                date_published=parse_council_date(_date_part(record.get("DocDate"))),
                description=clean_text(str(record.get("DocCategory") or "")),
                source_url=source_url,
            )
        )
    return documents


def _date_part(value: object) -> str | None:
    if value is None:
        return None
    return str(value).split("T", 1)[0]
