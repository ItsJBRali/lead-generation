from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit

from lead_generator.planning.adapters.generic import (
    GenericCouncilConfig,
    GenericLabelledPlanningScraper,
)
from lead_generator.planning.models import PlanningApplication
from lead_generator.planning.parsing import extract_postcode, parse_council_date


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

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        if url and self._looks_like_webforms_detail(url):
            return self._fetch_webforms_application(uid, url)
        return super().fetch_application(uid, url, include_documents=include_documents)

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
