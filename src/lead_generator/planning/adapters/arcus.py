from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit, urlunsplit

from lead_generator.planning.adapters.base import PlanningScraper
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient
from lead_generator.planning.models import DiscoveryResult, PlanningApplication
from lead_generator.planning.parsing import clean_text, extract_postcode, parse_council_date


@dataclass(frozen=True, slots=True)
class ArcusCouncilConfig:
    authority: str
    base_url: str
    register_name: str = "Arcus_BE_Public_Register"


class ArcusPlanningScraper(PlanningScraper):
    """Scraper for Arcus/Salesforce public registers."""

    def __init__(
        self,
        config: ArcusCouncilConfig,
        *,
        http_client: CouncilHttpClient | None = None,
    ) -> None:
        super().__init__(config.authority)
        self.config = config
        self.http = http_client or CouncilHttpClient(
            verify_tls=False,
            min_delay_seconds=1.25,
            retries=5,
            concurrency_key="portal:arcus",
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
        applications = [self._application_from_record(record, listing_url, fallback_date=start_date) for record in records]
        if limit is not None:
            applications = applications[:limit]
        return DiscoveryResult(authority=self.authority, source_url=listing_url, applications=applications)

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        raise ValueError("Arcus search results are complete enough for lead matching")

    def _search_records(
        self,
        listing_url: str,
        context: dict[str, object],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> list[dict[str, Any]]:
        return self._search_records_window(
            listing_url,
            context,
            start_date=start_date,
            end_date=end_date,
        )

    def _search_records_window(
        self,
        listing_url: str,
        context: dict[str, object],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> list[dict[str, Any]]:
        records, threshold_hit = self._fetch_search_records(
            listing_url,
            context,
            start_date=start_date,
            end_date=end_date,
        )
        if not (threshold_hit and start_date and end_date and start_date < end_date):
            return records

        midpoint = start_date + timedelta(days=(end_date - start_date).days // 2)
        next_start = midpoint + timedelta(days=1)
        left_records, left_threshold = self._fetch_search_records(
            listing_url,
            context,
            start_date=start_date,
            end_date=midpoint,
        )
        right_records, right_threshold = self._fetch_search_records(
            listing_url,
            context,
            start_date=next_start,
            end_date=end_date,
        )
        merged_split_records = self._dedupe_records([*left_records, *right_records])
        if len(merged_split_records) <= len(self._dedupe_records(records)):
            return records

        expanded_records: list[dict[str, Any]] = []
        if left_threshold and start_date < midpoint:
            expanded_records.extend(
                self._search_records_window(
                    listing_url,
                    context,
                    start_date=start_date,
                    end_date=midpoint,
                )
            )
        else:
            expanded_records.extend(left_records)
        if right_threshold and next_start < end_date:
            expanded_records.extend(
                self._search_records_window(
                    listing_url,
                    context,
                    start_date=next_start,
                    end_date=end_date,
                )
            )
        else:
            expanded_records.extend(right_records)
        return self._dedupe_records(expanded_records)

    def _fetch_search_records(
        self,
        listing_url: str,
        context: dict[str, object],
        *,
        start_date: date | None,
        end_date: date | None,
    ) -> tuple[list[dict[str, Any]], bool]:
        endpoint = self._aura_endpoint(listing_url)
        page_uri = self._page_uri(listing_url)
        message = {
            "actions": [
                {
                    "id": "1;a",
                    "descriptor": "aura://ApexActionController/ACTION$execute",
                    "callingDescriptor": "UNKNOWN",
                    "params": {
                        "namespace": "arcuscommunity",
                        "classname": "PR_SearchService",
                        "method": "search",
                        "params": {
                            "request": {
                                "registerName": self._register_name(listing_url),
                                "searchType": "advanced",
                                "searchName": "Planning_Applications",
                                "advancedSearchName": "PA_ADV_All",
                                "searchFilters": self._search_filters(start_date, end_date),
                            }
                        },
                        "cacheable": False,
                        "isContinuation": False,
                    },
                }
            ]
        }
        response = self.http.post_form(
            endpoint,
            {
                "message": json.dumps(message, separators=(",", ":")),
                "aura.context": json.dumps(context, separators=(",", ":")),
                "aura.pageURI": page_uri,
                "aura.token": "null",
            },
        )
        try:
            payload = json.loads(response.text)
        except json.JSONDecodeError as exc:
            raise CouncilFetchError(f"Invalid Arcus search response from {endpoint}") from exc
        records: list[dict[str, Any]] = []
        threshold_hit = False
        for action in payload.get("actions", []):
            if not isinstance(action, dict):
                continue
            return_value = action.get("returnValue")
            if isinstance(return_value, dict):
                inner = return_value.get("returnValue")
                if isinstance(inner, dict) and isinstance(inner.get("records"), list):
                    records.extend(record for record in inner["records"] if isinstance(record, dict))
                    threshold_hit = threshold_hit or bool(inner.get("thresholdHit"))
        return records, threshold_hit

    def _dedupe_records(self, records: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for record in records:
            key = clean_text(str(record.get("Id") or record.get("Name") or ""))
            if not key:
                key = json.dumps(record, sort_keys=True, default=str)
            if key in seen:
                continue
            seen.add(key)
            deduped.append(record)
        return deduped

    def _search_filters(self, start_date: date | None, end_date: date | None) -> list[dict[str, str]]:
        return [
            {"fieldName": "BROM_Location_of_Address__c", "fieldValue": "", "fieldDeveloperName": "PA_ADV_SiteAddress"},
            {"fieldName": "arcusbuiltenv__Proposal__c", "fieldValue": "", "fieldDeveloperName": "PA_ADV_Proposal"},
            {"fieldName": "arcusbuiltenv__Status__c", "fieldValue": "", "fieldDeveloperName": "PA_ADV_ApplicationStatus"},
            {"fieldName": "arcusbuiltenv__Type__c", "fieldValue": "", "fieldDeveloperName": "PA_ADV_ApplicationType"},
            {
                "fieldName": "arcusbuiltenv__Valid_Date__c",
                "fieldValue": start_date.isoformat() if start_date else "",
                "fieldDeveloperName": "PA_ADV_DateValidFrom",
            },
            {
                "fieldName": "arcusbuiltenv__Valid_Date__c",
                "fieldValue": end_date.isoformat() if end_date else "",
                "fieldDeveloperName": "PA_ADV_DateValidTo",
            },
            {
                "fieldName": "arcusbuiltenv__Decision_Notice_Sent_Date_Manual__c",
                "fieldValue": "",
                "fieldDeveloperName": "PA_ADV_DecisionNoticeSentDateFrom",
            },
            {
                "fieldName": "arcusbuiltenv__Decision_Notice_Sent_Date_Manual__c",
                "fieldValue": "",
                "fieldDeveloperName": "PA_ADV_DecisionNoticeSentDateTo",
            },
            {"fieldName": "arcusbuiltenv__Wards__c", "fieldValue": "", "fieldDeveloperName": "PA_ADV_Ward"},
            {"fieldName": "arcusbuiltenv__PS_Type__c", "fieldValue": "", "fieldDeveloperName": "PS_Type"},
        ]

    def _application_from_record(
        self,
        record: dict[str, Any],
        listing_url: str,
        *,
        fallback_date: date | None = None,
    ) -> PlanningApplication:
        uid = clean_text(str(record.get("Id") or ""))
        reference = clean_text(str(record.get("Name") or ""))
        if not uid:
            uid = reference
        if not uid:
            raise ValueError("Could not determine Arcus application uid")
        address = clean_text(str(record.get("arcusbuiltenv__Site_Address__c") or ""))
        received = self._first_record_value(
            record,
            "arcusbuiltenv__Received_Date__c",
            "arcusbuiltenv__Valid_Date__c",
            "Received_Date__c",
            "Valid_Date__c",
            "Date_Received__c",
        )
        parsed_received = parse_council_date(received)
        date_inferred = False
        if not parsed_received and fallback_date:
            parsed_received = fallback_date.isoformat()
            date_inferred = True
        parts = urlsplit(listing_url)
        path_prefix = self._path_prefix(parts.path)
        url = urlunsplit((parts.scheme, parts.netloc, f"{path_prefix}/s/detail/{uid}", "", ""))
        return PlanningApplication(
            authority=self.authority,
            uid=uid,
            url=url,
            reference=reference or uid,
            address=address,
            description=clean_text(str(record.get("arcusbuiltenv__Proposal__c") or "")),
            status=clean_text(str(record.get("arcusbuiltenv__Status__c") or "")),
            date_received=parsed_received,
            postcode=extract_postcode(address),
            source_url=listing_url,
            raw={
                "portal_family": "arcus",
                "api": "arcus_pr_search",
                "detail_complete": True,
                "date_range_filtered": True,
                "date_inferred_from_search_window": date_inferred,
                "record": record,
            },
        )

    def _first_record_value(self, record: dict[str, Any], *keys: str) -> str | None:
        for key in keys:
            value = record.get(key)
            if value is None:
                continue
            cleaned = clean_text(str(value))
            if cleaned:
                return cleaned
        return None

    def _aura_context(self, html_text: str) -> dict[str, object]:
        for match in re.finditer(r"/s/sfsites/l/([^/]+)/(?:inline|bootstrap)\.js", html_text):
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
                    "app": boot.get("app") or "siteforce:communityApp",
                    "loaded": loaded,
                    "dn": [],
                    "globals": {"srcdoc": True},
                    "uad": True,
                }
        raise CouncilFetchError("Could not find Arcus Salesforce Aura context")

    def _aura_endpoint(self, listing_url: str) -> str:
        parts = urlsplit(listing_url)
        path_prefix = self._path_prefix(parts.path)
        return urlunsplit((parts.scheme, parts.netloc, f"{path_prefix}/s/sfsites/aura", urlencode({"r": "1", "aura.ApexAction.execute": "1"}), ""))

    def _page_uri(self, listing_url: str) -> str:
        parts = urlsplit(listing_url)
        return urlunsplit(("", "", parts.path, parts.query, ""))

    def _register_name(self, listing_url: str) -> str:
        query = parse_qs(urlsplit(listing_url).query)
        return unquote((query.get("c__r") or [self.config.register_name])[0])

    def _path_prefix(self, path: str) -> str:
        match = re.match(r"(.+?)/s/", path, flags=re.IGNORECASE)
        return match.group(1) if match else ""
