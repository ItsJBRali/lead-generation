from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import unquote, urlencode, urlsplit, urlunsplit

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class WiltshireCouncilConfig:
    authority: str
    base_url: str


class WiltshirePlanningScraper(PlanningScraper):
    """Scraper for Wiltshire's custom Salesforce public register."""

    def __init__(
        self,
        config: WiltshireCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient(
            verify_tls=False,
            min_delay_seconds=1.25,
            retries=5,
            concurrency_key="portal:salesforce-custom",
        )

    def discover_ids(
        self,
        *,
        listing_url: str,
        start_date: date | None = None,
        end_date: date | None = None,
        limit: int | None = None,
        **_: object,
    ) -> DiscoveryResult:
        page = self.http.get(listing_url)
        context = self._aura_context(page.text)
        records = self._search_records(
            listing_url,
            context,
            start_date=start_date,
            end_date=end_date,
        )
        applications = [
            self._application_from_record(record, listing_url, fallback_date=start_date)
            for record in records
        ]
        if limit is not None:
            applications = applications[:limit]
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
        raise ValueError("Wiltshire search results are complete enough for lead matching")

    def _search_records(
        self,
        listing_url: str,
        context: dict[str, object],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> list[dict[str, Any]]:
        criteria: dict[str, str] = {}
        if start_date:
            criteria["arcusbuiltenv__Valid_Date__c:from"] = start_date.isoformat()
        if end_date:
            criteria["arcusbuiltenv__Valid_Date__c:to"] = end_date.isoformat()
        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "apex://PR_SearchCont/ACTION$query",
                    "callingDescriptor": "markup://c:PR_Search",
                    "params": {
                        "searchable_resources": "regserv_searchables,be_searchables,dev_searchables",
                        "resource_name": (
                            "be_adv_categories,be_adv_categories_Building_Control,"
                            "be_adv_categories_Enforcements"
                        ),
                        "category_name": "PApplication",
                        "search_criteria": criteria,
                    },
                    "version": None,
                }
            ]
        }
        response = self.http.post_form(
            self._aura_endpoint(listing_url),
            {
                "message": json.dumps(message, separators=(",", ":")),
                "aura.context": json.dumps(context, separators=(",", ":")),
                "aura.pageURI": self._page_uri(listing_url),
                "aura.token": "null",
            },
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError("Invalid Wiltshire planning search response") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("actions"), list):
            raise CouncilFetchError("Invalid Wiltshire planning search payload")

        found_search_action = False
        for action in payload["actions"]:
            if not isinstance(action, dict):
                continue
            state = str(action.get("state") or "").upper()
            if state == "ERROR":
                errors = action.get("error") or action.get("errors") or "unknown Salesforce error"
                raise CouncilFetchError(f"Wiltshire planning search failed: {errors}")
            return_value = action.get("returnValue")
            if return_value is None:
                continue
            found_search_action = True
            if isinstance(return_value, str):
                try:
                    return_value = json.loads(return_value)
                except json.JSONDecodeError as exc:
                    raise CouncilFetchError("Invalid Wiltshire planning search result data") from exc
            if isinstance(return_value, dict):
                records = return_value.get("records")
                if isinstance(records, list):
                    return [record for record in records if isinstance(record, dict)]
        if found_search_action:
            raise CouncilFetchError("Wiltshire planning search result did not contain records")
        raise CouncilFetchError("Wiltshire planning search action was missing from the response")

    def _application_from_record(
        self,
        record: dict[str, Any],
        listing_url: str,
        *,
        fallback_date: date | None,
    ) -> PlanningApplication:
        uid = clean_text(str(record.get("Id") or ""))
        reference = clean_text(str(record.get("Name") or ""))
        if not uid:
            uid = reference
        if not uid:
            raise CouncilFetchError("Wiltshire returned an application without an identifier")
        address = clean_text(
            str(
                record.get("arcusbuiltenv__Site_Address__c")
                or record.get("Hidden_Address__c")
                or ""
            )
        )
        validated = parse_council_date(
            clean_text(str(record.get("arcusbuiltenv__Valid_Date__c") or ""))
        )
        date_inferred = False
        if not validated and fallback_date:
            validated = fallback_date.isoformat()
            date_inferred = True
        parts = urlsplit(listing_url)
        path_prefix = self._path_prefix(parts.path)
        reference_slug = re.sub(r"[^a-z0-9]+", "", (reference or uid).casefold())
        detail_url = urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                f"{path_prefix}/s/planning-application/{uid}/{reference_slug}",
                "",
                "",
            )
        )
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=detail_url,
            reference=reference or uid,
            address=address,
            description=clean_text(
                str(
                    record.get("Hidden_Proposal__c")
                    or record.get("arcusbuiltenv__Proposal__c")
                    or ""
                )
            ),
            status=clean_text(str(record.get("arcusbuiltenv__Status__c") or "")),
            date_validated=validated,
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": "wiltshire_salesforce",
                "api": "PR_SearchCont.query",
                "detail_complete": True,
                "date_range_filtered": True,
                "date_inferred_from_search_window": date_inferred,
                "record": record,
            },
        )

    def _aura_context(self, text: str) -> dict[str, object]:
        for match in re.finditer(r"/s/sfsites/l/([^/]+)/(?:inline|bootstrap)\.js", text):
            try:
                boot = json.loads(unquote(match.group(1)))
            except json.JSONDecodeError:
                continue
            fwuid = boot.get("fwuid")
            loaded = boot.get("loaded")
            if fwuid and isinstance(loaded, dict):
                return {
                    "mode": boot.get("mode") or "PROD",
                    "fwuid": fwuid,
                    "app": boot.get("app") or "siteforce:napiliApp",
                    "loaded": loaded,
                    "dn": [],
                    "globals": {},
                    "uad": True,
                    "pathPrefix": boot.get("pathPrefix") or "",
                }
        raise CouncilFetchError("Could not find Wiltshire Salesforce Aura context")

    def _aura_endpoint(self, listing_url: str) -> str:
        parts = urlsplit(listing_url)
        path_prefix = self._path_prefix(parts.path)
        return urlunsplit(
            (
                parts.scheme,
                parts.netloc,
                f"{path_prefix}/s/sfsites/aura",
                urlencode({"r": "10", "other.PR_SearchCont.query": "1"}),
                "",
            )
        )

    def _page_uri(self, listing_url: str) -> str:
        parts = urlsplit(listing_url)
        return urlunsplit(("", "", parts.path, parts.query, ""))

    def _path_prefix(self, path: str) -> str:
        match = re.match(r"(.+?)/s/", path, flags=re.IGNORECASE)
        return match.group(1) if match else ""
