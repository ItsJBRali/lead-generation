from __future__ import annotations

import csv
import json
import re
import ssl
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from email.message import Message
from importlib import resources
from pathlib import Path
from time import monotonic, sleep
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener, urlopen

from lxml import html

from lead_generator.planning.adapters import (
    AchieveFormsCouncilConfig,
    AchieveFormsPlanningScraper,
    AgileCouncilConfig,
    AgilePlanningScraper,
    AppSearchServPlanningScraper,
    ArcusCouncilConfig,
    ArcusPlanningScraper,
    AstunPlanningScraper,
    AtriumCouncilConfig,
    AtriumPlanningScraper,
    authority_specific_scraper,
    CcedPlanningScraper,
    CivicaCouncilConfig,
    CivicaPlanningScraper,
    EnterpriseStorePlanningScraper,
    FastwebPlanningScraper,
    HtmlListPlanningScraper,
    IdoxCouncilConfig,
    IdoxPublicAccessScraper,
    LegacyFormsCouncilConfig,
    NorthgateCouncilConfig,
    NorthLincsPlanningScraper,
    NorthgatePlanningScraper,
    OcellaCouncilConfig,
    OcellaPlanningScraper,
    PlanningScraper,
    QueryFormPlanningScraper,
    SocrataPlanningScraper,
    StatMapPlanningScraper,
    TascomiPlanningScraper,
    WebFormsPlanningScraper,
    WiltshireCouncilConfig,
    WiltshirePlanningScraper,
)
from lead_generator.planning.adapters.civica import fetch_civica_documents_from_raw
from lead_generator.planning.adapters.generic import GenericCouncilConfig, GenericLabelledPlanningScraper
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text
from lead_generator.planning.scheduler import (
    PlatformAwareScheduler,
    ScheduledTask,
    SchedulerAdjustment,
)


DEFAULT_KEYWORDS = [
    "gates",
    "driveway gates",
    "entrance gates",
    "electric gates",
    "automated gates",
    "sliding gates",
    "swing gates",
    "metal gates",
    "timber gates",
    "vehicular gates",
    "pedestrian gate",
    "vehicular access",
    "new access",
    "alterations to access",
    "widen existing access",
    "driveway access",
    "access improvements",
    "front boundary",
    "boundary treatment",
    "boundary wall",
    "front wall",
    "boundary enclosure",
    "railings",
    "gate piers",
    "brick piers",
    "entrance piers",
    "pillars",
    "erection of gates",
    "installation of gates",
    "new entrance gates",
    "automated entrance gates",
    "electric driveway gates",
    "boundary wall and gates",
    "front boundary wall and gates",
    "new vehicular access and gates",
    "alterations to front boundary",
    "replacement gates",
    "new gate piers",
    "entrance walls and gates",
]

EXCLUDED_PROPOSAL_PHRASES = (
    "variation of condition",
    "discharge of condition",
    "details required by",
    "request for eia",
    "compliance with",
    "details of reserved matters",
    "submission of details",
    "details pursuant to",
    "section 73",
    "application to vary",
    "submission of material",
    "submission of surface",
    "edc consultation",
    "removal of condition",
    "partial approval of",
    "noise assessment",
)


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
CapturedCallback = Callable[[int], None]
CancelCallback = Callable[[], bool]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/126.0.0.0 Safari/537.36"
)
PLANIT_PAGE_SIZE = 100
DEFAULT_SEARCH_WORKER_COUNT = 4
MAX_SEARCH_WORKER_COUNT = 8
DEFAULT_PLATFORM_CONCURRENCY_LIMIT = 2
PLATFORM_CONCURRENCY_LIMITS = {
    "planit": 1,
    "salesforce": 1,
    "arcus": 2,
    "idox": 3,
    "custom": 3,
}
SEARCH_HOST_CONCURRENCY_LIMIT = 1
PLATFORM_RATE_LIMIT_COOLDOWN_SECONDS = 15.0
PLATFORM_BLOCKED_COOLDOWN_SECONDS = 8.0
PLATFORM_SERVICE_COOLDOWN_SECONDS = 8.0
PLATFORM_RECOVERY_SUCCESS_COUNT = 4
SEARCH_WORKER_STAGGER_BASE_SECONDS = 0.5
SEARCH_WORKER_STAGGER_INCREMENT_SECONDS = 0.15
SEARCH_WORKER_STAGGER_MAX_SECONDS = 1.5
DOCUMENT_DOWNLOAD_DELAY_SECONDS = 0.0
RATE_LIMIT_HTTP_CODES = {429, 503}
MAX_RETRY_AFTER_SECONDS = 20.0
DEFAULT_REQUEST_TIMEOUT_SECONDS = 20.0
REQUEST_THROTTLE_SECONDS = 0.25
PLANIT_REQUEST_THROTTLE_SECONDS = 1.5
APPLICATION_CSV_FIELDS = ["Reference", "address", "application link", "proposal", "date received", "council"]
FAILURE_CSV_FIELDS = ["council", "portal_family", "scraper_type", "listing_url", "reason"]
HISTORY_CSV_FIELDS = [
    "Search Date",
    "Search Time",
    "Keyword Set",
    "Total Applications",
    "Relevant Captured Applications",
    "% Relevant",
    "List of failed councils",
    "List of councils with no applications",
    "Completion",
    "Captured Documents",
]
_REQUEST_THROTTLE_LOCK = threading.Lock()
_LAST_REQUEST_AT: dict[str, float] = {}
_PLANIT_CONCURRENCY_GATE = threading.BoundedSemaphore(1)

PLANIT_AUTHORITY_ALIASES = {
    "Aylesbury Vale": ("Buckinghamshire",),
    "Brighton and Hove": ("Brighton",),
    "Chiltern South Bucks": ("Buckinghamshire",),
    "West Somerset": ("Somerset",),
    "Windsor and Maidenhead": ("Windsor",),
    "Wycombe": ("Buckinghamshire",),
}

PLANIT_FIRST_AUTHORITIES = {
    "Birmingham",
    "South Cambridgeshire",
    "Surrey",
}

FRESH_SESSION_RETRY_TOKENS = (
    "http 403",
    "http 429",
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "blocked by web application firewall",
    "document is empty",
    "empty response",
    "invalid arcus search response",
    "network error",
    "timed out",
    "timeout",
)

RESPONSIVE_PORTAL_ERROR_TOKENS = (
    "http 400",
    "http 401",
    "http 403",
    "http 404",
    "http 405",
    "http 406",
    "http 409",
    "http 410",
    "http 418",
    "http 422",
    "http 429",
    "blocked by web application firewall",
    "forbidden",
    "could not determine",
    "could not find",
    "document is empty",
    "empty response",
    "invalid arcus search response",
    "invalid json",
    "not valid xml",
    "requires a detail url",
)

CONFIRMED_OUTAGE_ERROR_TOKENS = (
    "http 500",
    "http 502",
    "http 503",
    "http 504",
    "connection refused",
    "getaddrinfo failed",
    "name or service not known",
    "no such host",
)

@dataclass(frozen=True, slots=True)
class CouncilTarget:
    authority: str
    portal_family: str
    scraper_type: str
    base_url: str
    listing_url: str | None
    geometry: dict[str, object]
    link_test_ok: bool = False


def council_platform_key(target: CouncilTarget) -> str:
    if target.authority in PLANIT_AUTHORITY_ALIASES or target.authority in PLANIT_FIRST_AUTHORITIES:
        return "planit"
    authority = target.authority.casefold()
    portal = f"{target.scraper_type} {target.portal_family}".casefold()
    if authority in {"eastleigh", "wiltshire"}:
        return "salesforce"
    platform_markers = (
        ("idox", "idox"),
        ("arcus", "arcus"),
        ("achieveforms", "achieveforms"),
        ("achieve forms", "achieveforms"),
        ("atrium", "atrium"),
        ("tascomi", "tascomi"),
        ("enterprisestore", "enterprise-store"),
        ("enterprise store", "enterprise-store"),
        ("appsearchserv", "app-search-serv"),
        ("fastweb", "fastweb"),
        ("cced", "cced"),
        ("astun", "astun"),
        ("socrata", "socrata"),
        ("statmap", "statmap"),
        ("ocella", "ocella"),
        ("agile", "agile"),
        ("civica", "civica"),
        ("northgate", "northgate"),
        ("planningexplorer", "northgate"),
    )
    for marker, platform in platform_markers:
        if marker in portal:
            return platform
    return "custom"


def council_host_key(target: CouncilTarget, platform: str | None = None) -> str:
    platform = platform or council_platform_key(target)
    if platform == "planit":
        return "www.planit.org.uk"
    source_url = target.listing_url or target.base_url
    host = urlsplit(source_url).netloc.casefold()
    return host or target.authority.casefold()


def scheduled_council_task(target: CouncilTarget) -> ScheduledTask[CouncilTarget]:
    platform = council_platform_key(target)
    return ScheduledTask(
        item=target,
        platform=platform,
        host=council_host_key(target, platform),
    )


def search_failure_signal(exc: Exception) -> str:
    text = _portal_error_text(exc)
    if any(token in text for token in ("http 429", "too many requests", "rate limit")):
        return "rate_limited"
    if any(token in text for token in ("http 403", "forbidden", "web application firewall")):
        return "blocked"
    if "http 503" in text:
        return "service_unavailable"
    return "failed"


def search_failure_platform(task: ScheduledTask[CouncilTarget], exc: Exception) -> str:
    text = _portal_error_text(exc)
    if "planit.org.uk" in text:
        return "planit"
    return task.platform


def search_worker_start_delay(worker_index: int) -> float:
    if worker_index <= 0:
        return 0.0
    return min(
        SEARCH_WORKER_STAGGER_BASE_SECONDS
        + ((worker_index - 1) * SEARCH_WORKER_STAGGER_INCREMENT_SECONDS),
        SEARCH_WORKER_STAGGER_MAX_SECONDS,
    )


@dataclass(frozen=True, slots=True)
class LeadSearchConfig:
    geojson_path: Path
    output_root: Path
    start_date: date
    end_date: date
    keywords: list[str]
    catalogue_path: Path | None = None
    download_application_files: bool = True
    worker_count: int = DEFAULT_SEARCH_WORKER_COUNT
    history_csv_path: Path | None = None


@dataclass(slots=True)
class LeadSearchResult:
    output_dir: Path
    csv_path: Path
    failure_csv_path: Path
    geojson_features: int
    councils_total: int
    councils_completed: int
    leads_found: int
    total_applications: int = 0
    captured_documents: int = 0
    failed_councils: list[str] = field(default_factory=list)
    no_application_councils: list[str] = field(default_factory=list)
    completion: str = "Completed"
    history_csv_path: Path | None = None


@dataclass(slots=True)
class DownloadedFile:
    payload: bytes
    final_url: str
    content_type: str
    filename: str | None = None


class CouncilSearchDegradedError(RuntimeError):
    """The council service responded, but its search could not be completed."""


def parse_keywords(text: str) -> list[str]:
    keywords: list[str] = []
    seen: set[str] = set()
    for line in text.splitlines():
        keyword = line.strip().strip("\"'“”")
        if not keyword:
            continue
        key = keyword.casefold()
        if key in seen:
            continue
        seen.add(key)
        keywords.append(keyword)
    return keywords


def run_lead_search(
    config: LeadSearchConfig,
    *,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    captured: CapturedCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> LeadSearchResult:
    started_at = datetime.now()
    user_geojson = load_geojson(config.geojson_path)
    catalogue = load_authority_catalogue(config.catalogue_path)
    targets = select_overlapping_authorities(user_geojson, catalogue)
    output_dir = config.output_root / date.today().isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "applications.csv"
    failure_csv_path = output_dir / "search_failures.csv"
    selected_path = output_dir / "selected_councils.geojson"
    selected_path.write_text(councils_to_geojson(targets), encoding="utf-8")
    initialise_csv(csv_path, APPLICATION_CSV_FIELDS)
    initialise_csv(failure_csv_path, FAILURE_CSV_FIELDS)

    rows: list[dict[str, str]] = []
    saved_references: set[str] = set()
    total_applications = 0
    captured_documents = 0
    failed_councils: list[str] = []
    no_application_councils: list[str] = []
    had_error = False
    cancelled = False
    completed = 0
    feature_count = len(user_geojson.get("features", []))
    _log(log, f"Read {feature_count} user GeoJSON features from {config.geojson_path.name}")
    _log(log, f"Selected {len(targets)} overlapping planning authorities")
    _log(log, f"Saved selected council catalogue to {selected_path}")
    if not targets:
        raise ValueError("No planning authorities overlap the supplied GeoJSON boundary.")
    _progress(progress, completed, len(targets))

    lock = threading.Lock()
    completed_authorities: set[str] = set()
    counted_authorities: set[str] = set()
    captured_document_references: set[str] = set()
    deferred_tasks: list[ScheduledTask[CouncilTarget]] = []
    scheduler = PlatformAwareScheduler[CouncilTarget](
        platform_limits=PLATFORM_CONCURRENCY_LIMITS,
        default_platform_limit=DEFAULT_PLATFORM_CONCURRENCY_LIMIT,
        host_limit=SEARCH_HOST_CONCURRENCY_LIMIT,
        rate_limit_cooldown_seconds=PLATFORM_RATE_LIMIT_COOLDOWN_SECONDS,
        blocked_cooldown_seconds=PLATFORM_BLOCKED_COOLDOWN_SECONDS,
        service_cooldown_seconds=PLATFORM_SERVICE_COOLDOWN_SECONDS,
        recovery_successes=PLATFORM_RECOVERY_SUCCESS_COUNT,
    )

    def mark_complete(target: CouncilTarget) -> None:
        nonlocal completed
        with lock:
            if target.authority in completed_authorities:
                return
            completed_authorities.add(target.authority)
            completed += 1
            current = completed
        _progress(progress, current, len(targets))

    def cancellation_requested() -> bool:
        nonlocal cancelled
        if should_cancel and should_cancel():
            cancelled = True
            return True
        return False

    def reserve_reference(reference: str) -> bool:
        with lock:
            if reference in saved_references:
                return False
            saved_references.add(reference)
        return True

    def save_row(row: dict[str, str]) -> None:
        current = 0
        with lock:
            rows.append(row)
            append_csv_row(csv_path, APPLICATION_CSV_FIELDS, row)
            current = len(rows)
        _captured(captured, current)

    def add_total_applications(target: CouncilTarget, count: int) -> None:
        nonlocal total_applications
        with lock:
            if target.authority in counted_authorities:
                return
            counted_authorities.add(target.authority)
            total_applications += count

    def add_captured_document_application(reference: str) -> None:
        nonlocal captured_documents
        with lock:
            if reference in captured_document_references:
                return
            captured_document_references.add(reference)
            captured_documents += 1

    def add_no_application_council(target: CouncilTarget) -> None:
        with lock:
            if target.authority not in no_application_councils:
                no_application_councils.append(target.authority)

    def save_failure(
        target: CouncilTarget,
        reason: str,
        *,
        no_applications_returned: bool = True,
        fatal: bool = True,
    ) -> None:
        nonlocal had_error
        with lock:
            if fatal:
                had_error = True
            if fatal and no_applications_returned and target.authority not in failed_councils:
                failed_councils.append(target.authority)
            append_csv_row(
                failure_csv_path,
                FAILURE_CSV_FIELDS,
                {
                    "council": target.authority,
                    "portal_family": target.portal_family,
                    "scraper_type": target.scraper_type,
                    "listing_url": target.listing_url or target.base_url,
                    "reason": reason,
                },
            )

    def log_scheduler_adjustment(adjustment: SchedulerAdjustment | None) -> None:
        if adjustment is None:
            return
        if adjustment.cooldown_seconds:
            _log(
                log,
                f"Scheduler: paused new {adjustment.platform} searches for "
                f"{adjustment.cooldown_seconds:.0f}s after {adjustment.reason}; "
                f"concurrency is now {adjustment.limit}",
            )
        else:
            _log(
                log,
                f"Scheduler: {adjustment.platform} concurrency increased to "
                f"{adjustment.limit} after successful searches",
            )

    def search_worker(
        name: str,
        *,
        final_attempt: bool,
        initial_delay: float,
    ) -> None:
        if initial_delay:
            sleep(initial_delay)
        while True:
            task = scheduler.acquire(should_stop=cancellation_requested)
            if task is None:
                if cancelled:
                    _log(log, f"{name}: cancelled.")
                return
            target = task.item

            attempt_label = "final retry for" if final_attempt else "searching"
            _log(
                log,
                f"{name}: {attempt_label} {target.authority} "
                f"({task.platform}, {task.host})",
            )
            applications: list[PlanningApplication] = []
            attempt_error: Exception | None = None
            try:
                applications = discover_portal_applications(target, config.start_date, config.end_date)
                add_total_applications(target, len(applications))
                if not applications:
                    add_no_application_council(target)
                _log(log, f"{target.authority}: found {len(applications)} applications in the date range")
                matched_count = 0
                for application in applications:
                    if cancellation_requested():
                        break
                    if not application_matches(application, config.start_date, config.end_date, config.keywords):
                        continue
                    if not application_matches_search_area(application, user_geojson):
                        continue
                    reference = application.reference or application.uid
                    if not reserve_reference(reference):
                        _log(log, f"{target.authority}: skipped duplicate reference {reference}")
                        continue
                    matched_count += 1
                    if config.download_application_files:
                        application = enrich_application_documents(application)
                        lead_folder = create_lead_folder(output_dir, target.authority, application)
                        downloaded_count = download_pdf_documents(application.documents, lead_folder, log=log)
                    else:
                        downloaded_count = 0
                    if config.download_application_files and application.documents:
                        add_captured_document_application(reference)
                    save_row(
                        {
                            "Reference": reference,
                            "address": application.address or "",
                            "application link": application_link(application),
                            "proposal": application.description or "",
                            "date received": application.date_received or application.date_validated or "",
                            "council": target.authority,
                        }
                    )
                    if config.download_application_files:
                        _log(log, f"{target.authority}: saved {application.reference or application.uid} ({downloaded_count} documents downloaded)")
                    else:
                        _log(log, f"{target.authority}: saved {application.reference or application.uid} (file downloads not requested)")
                _log(log, f"{target.authority}: {matched_count} applications matched keywords and location")
            except Exception as exc:  # pragma: no cover - live-site resilience
                attempt_error = exc

            signal = search_failure_signal(attempt_error) if attempt_error else "success"
            affected_platform = (
                search_failure_platform(task, attempt_error)
                if attempt_error
                else task.platform
            )
            adjustment = scheduler.release(
                task,
                signal=signal,
                affected_platform=affected_platform,
            )
            log_scheduler_adjustment(adjustment)

            if attempt_error is None:
                if final_attempt:
                    _log(log, f"{target.authority}: final retry succeeded")
                mark_complete(target)
                continue

            if not final_attempt:
                with lock:
                    deferred_tasks.append(task)
                _log(
                    log,
                    f"{target.authority}: deferred until the final retry pass: {attempt_error}",
                )
                continue

            if isinstance(attempt_error, CouncilSearchDegradedError):
                _log(
                    log,
                    f"{target.authority}: portal responded but the final retry was unavailable: "
                    f"{attempt_error}",
                )
                save_failure(
                    target,
                    f"Responsive portal search issue after final retry: {attempt_error}",
                    no_applications_returned=False,
                    fatal=False,
                )
            else:
                _log(log, f"{target.authority}: final retry failed: {attempt_error}")
                save_failure(
                    target,
                    str(attempt_error),
                    no_applications_returned=not applications,
                )
            mark_complete(target)

    configured_worker_count = min(max(config.worker_count, 1), MAX_SEARCH_WORKER_COUNT)

    def run_search_phase(
        phase_tasks: list[ScheduledTask[CouncilTarget]],
        *,
        final_attempt: bool,
    ) -> None:
        scheduler.load_phase(phase_tasks)
        worker_count = min(configured_worker_count, len(phase_tasks))
        workers = [
            threading.Thread(
                target=search_worker,
                args=(f"Search worker {index + 1}",),
                kwargs={
                    "final_attempt": final_attempt,
                    "initial_delay": search_worker_start_delay(index),
                },
                daemon=True,
            )
            for index in range(worker_count)
        ]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    run_search_phase(
        [scheduled_council_task(target) for target in targets],
        final_attempt=False,
    )
    if deferred_tasks and not cancelled:
        _log(
            log,
            f"Starting final retry pass for {len(deferred_tasks)} deferred councils after all other searches finished",
        )
        run_search_phase(list(deferred_tasks), final_attempt=True)

    if cancelled:
        completion = "Cancelled"
    elif had_error:
        completion = "Failed"
    else:
        completion = "Completed"
    _log(log, f"Finished. Saved {len(rows)} leads to {csv_path}")
    _log(log, f"Saved failed council log to {failure_csv_path}")
    result = LeadSearchResult(
        output_dir=output_dir,
        csv_path=csv_path,
        failure_csv_path=failure_csv_path,
        geojson_features=feature_count,
        councils_total=len(targets),
        councils_completed=completed,
        leads_found=len(rows),
        total_applications=total_applications,
        captured_documents=captured_documents,
        failed_councils=failed_councils,
        no_application_councils=no_application_councils,
        completion=completion,
        history_csv_path=config.history_csv_path,
    )
    if config.history_csv_path:
        try:
            append_search_history(config.history_csv_path, result, config.keywords, started_at)
            _log(log, f"Saved search history to {config.history_csv_path}")
        except Exception as exc:  # pragma: no cover - history should not break a completed scrape
            _log(log, f"Could not save search history: {exc}")
    return result


def load_geojson(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_authority_catalogue(path: Path | None = None) -> dict[str, object]:
    if path:
        return load_geojson(path)
    data = resources.files("lead_generator.planning.data").joinpath("planning_authorities.geojson")
    return json.loads(data.read_text(encoding="utf-8"))


def select_overlapping_authorities(
    user_geojson: dict[str, object],
    authority_catalogue: dict[str, object],
) -> list[CouncilTarget]:
    user_geometries = list(iter_feature_geometries(user_geojson))
    targets: list[CouncilTarget] = []
    for feature in authority_catalogue.get("features", []):
        geometry = feature.get("geometry")
        if not isinstance(geometry, dict):
            continue
        if not any(geometries_intersect(user_geometry, geometry) for user_geometry in user_geometries):
            continue
        properties = feature.get("properties") or {}
        targets.append(
            CouncilTarget(
                authority=str(properties.get("authority") or properties.get("council_name")),
                portal_family=str(properties.get("portal_family") or "unknown"),
                scraper_type=str(properties.get("scraper_type") or properties.get("portal_family") or "unknown"),
                base_url=str(properties.get("base_url") or ""),
                listing_url=str(properties.get("listing_url") or properties.get("planning_url") or ""),
                geometry=geometry,
                link_test_ok=bool(properties.get("link_test_ok")),
            )
        )
    return targets


def councils_to_geojson(targets: list[CouncilTarget]) -> str:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "authority": target.authority,
                        "portal_family": target.portal_family,
                        "scraper_type": target.scraper_type,
                        "base_url": target.base_url,
                        "listing_url": target.listing_url,
                        "link_test_ok": target.link_test_ok,
                    },
                    "geometry": target.geometry,
                }
                for target in targets
            ],
        },
        indent=2,
    )


GENERIC_UID_QUERY_PARAMS = (
    "id",
    "uid",
    "ref",
    "reference",
    "appNo",
    "appno",
    "caseNo",
    "case",
    "application",
    "keyVal",
    "KEYVAL",
    "REFVAL",
    "PARAM0",
)

GENERIC_DETAIL_MARKERS = (
    "planning",
    "application",
    "details",
    "detail",
    "display",
    "view",
    "case",
    "register",
)


def discover_portal_applications(target: CouncilTarget, start_date: date, end_date: date) -> list[PlanningApplication]:
    if target.authority in PLANIT_AUTHORITY_ALIASES or target.authority in PLANIT_FIRST_AUTHORITIES:
        planit_applications = discover_planit_fallback_applications(target, start_date, end_date)
        if planit_applications:
            return planit_applications

    try:
        discovery, scraper = _discover_portal_listing(target, start_date, end_date)
    except Exception as exc:
        planit_applications = discover_planit_fallback_applications(target, start_date, end_date, portal_error=exc)
        if planit_applications:
            return planit_applications
        if _portal_error_proves_service_responded(exc):
            raise CouncilSearchDegradedError(str(exc)) from exc
        raise
    applications: list[PlanningApplication] = []
    seen: set[str] = set()
    detail_fetch_failed = False
    for stub in discovery.applications:
        application = stub
        if not (stub.raw or {}).get("detail_complete"):
            try:
                application = scraper.fetch_application(stub.uid, stub.url, include_documents=False)
            except Exception as exc:
                detail_fetch_failed = True
                stub.raw = {**(stub.raw or {}), "detail_fetch_error": str(exc)}
        application = with_portal_metadata(application, stub, target, discovery.source_url)
        key = application.reference or application.uid or application.url
        if key in seen:
            continue
        seen.add(key)
        application_date = application.date_received or application.date_validated
        if application_date:
            received = _parse_iso_date(application_date)
            if received is not None and (received < start_date or received > end_date):
                continue
        applications.append(application)
    if not applications or detail_fetch_failed:
        planit_applications = discover_planit_fallback_applications(target, start_date, end_date)
        if planit_applications:
            return _merge_portal_and_planit_applications(applications, planit_applications)
    return applications


def _merge_portal_and_planit_applications(
    portal_applications: list[PlanningApplication],
    planit_applications: list[PlanningApplication],
) -> list[PlanningApplication]:
    by_reference = {
        (application.reference or application.uid).strip(): application
        for application in portal_applications
        if application.reference or application.uid
    }
    merged = list(portal_applications)
    for fallback in planit_applications:
        reference = (fallback.reference or fallback.uid).strip()
        existing = by_reference.get(reference)
        if existing is None:
            by_reference[reference] = fallback
            merged.append(fallback)
            continue
        for field in (
            "address",
            "description",
            "status",
            "decision",
            "date_received",
            "date_validated",
            "postcode",
        ):
            if not getattr(existing, field) and getattr(fallback, field):
                setattr(existing, field, getattr(fallback, field))
        existing.raw = {
            **(fallback.raw or {}),
            **(existing.raw or {}),
            "planit_supplemented": True,
        }
    return merged


def _discover_portal_listing(target: CouncilTarget, start_date: date, end_date: date):
    last_error: Exception | None = None
    for attempt in range(2):
        scraper = planning_scraper_for_target(target)
        listing_url = target.listing_url or None
        if not listing_url and not isinstance(scraper, IdoxPublicAccessScraper):
            raise ValueError("Council has no portal search URL")
        try:
            discovery = scraper.discover_ids(listing_url=listing_url, start_date=start_date, end_date=end_date)
            return discovery, scraper
        except Exception as exc:
            last_error = exc
            if attempt == 0 and _should_retry_portal_with_fresh_session(exc):
                continue
            raise
    raise last_error or RuntimeError(f"Could not search {target.authority}")


def _should_retry_portal_with_fresh_session(exc: Exception) -> bool:
    text = _portal_error_text(exc)
    return any(token in text for token in FRESH_SESSION_RETRY_TOKENS)


def _portal_error_proves_service_responded(exc: Exception) -> bool:
    text = _portal_error_text(exc)
    if any(token in text for token in RESPONSIVE_PORTAL_ERROR_TOKENS):
        return True
    return not any(token in text for token in CONFIRMED_OUTAGE_ERROR_TOKENS)


def _portal_error_text(exc: Exception) -> str:
    parts = [f"{type(exc).__name__}: {exc}"]
    cause = exc.__cause__
    while cause is not None:
        parts.append(f"{type(cause).__name__}: {cause}")
        cause = cause.__cause__
    return " | ".join(parts).casefold()


def discover_planit_fallback_applications(
    target: CouncilTarget,
    start_date: date,
    end_date: date,
    *,
    portal_error: Exception | None = None,
) -> list[PlanningApplication]:
    last_error: Exception | None = None
    for authority in planit_authority_candidates(target.authority):
        try:
            applications = discover_planit_applications(authority, start_date, end_date)
        except Exception as exc:
            last_error = exc
            continue
        if not applications:
            continue
        for application in applications:
            application.authority = target.authority
            application.raw = {
                **(application.raw or {}),
                "portal_family": target.portal_family,
                "scraper_type": target.scraper_type,
                "source": "planit_fallback",
                "planit_authority": authority,
            }
            if portal_error:
                application.raw["portal_fetch_error"] = str(portal_error)
        return applications
    if portal_error and last_error:
        portal_error.add_note(f"Public planning metadata fallback also failed: {last_error}")
    return []


def planit_authority_candidates(authority: str) -> tuple[str, ...]:
    aliases = PLANIT_AUTHORITY_ALIASES.get(authority, ())
    return (*aliases, authority)


def planning_scraper_for_target(target: CouncilTarget) -> PlanningScraper:
    base_url = target_base_url(target)
    scraper_type = (target.scraper_type or "").casefold()
    family = (target.portal_family or "").casefold()
    portal_key = f"{scraper_type} {family}"
    authority_key = target.authority.casefold()

    if authority_key == "wiltshire":
        return WiltshirePlanningScraper(WiltshireCouncilConfig(authority=target.authority, base_url=base_url))
    if authority_scraper := authority_specific_scraper(target.authority, base_url):
        return authority_scraper
    if "arcus" in portal_key:
        return ArcusPlanningScraper(ArcusCouncilConfig(authority=target.authority, base_url=base_url))
    if "achieveforms" in portal_key or "achieve forms" in portal_key:
        return AchieveFormsPlanningScraper(AchieveFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "atrium" in portal_key:
        return AtriumPlanningScraper(AtriumCouncilConfig(authority=target.authority, base_url=base_url))
    listing_key = (target.listing_url or "").casefold()
    if "tascomi" in portal_key and ("tascomi" in listing_key or "/planning/index.html" in listing_key):
        return TascomiPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "enterprisestore" in portal_key or "enterprise store" in portal_key:
        return EnterpriseStorePlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "appsearchserv" in portal_key or "applicationsearchservlet" in portal_key:
        return AppSearchServPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "fastweb" in portal_key:
        return FastwebPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "cced" in portal_key:
        return CcedPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "astun" in portal_key or "developmentcontrol.aspx" in portal_key or "advancedsearchtab.tmplt" in listing_key:
        return AstunPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "socrata" in portal_key or "opendata.camden.gov.uk" in listing_key or authority_key == "camden":
        return SocrataPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "statmap" in portal_key or "horizonext" in listing_key or "horizonext" in listing_key:
        return StatMapPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if authority_key == "eastleigh":
        return ArcusPlanningScraper(ArcusCouncilConfig(authority=target.authority, base_url=base_url))
    if authority_key == "peak district":
        return EnterpriseStorePlanningScraper(
            LegacyFormsCouncilConfig(authority=target.authority, base_url="https://planning.peakdistrict.gov.uk")
        )
    if authority_key == "north lincs":
        return NorthLincsPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if authority_key in {
        "copeland",
        "scilly isles",
        "south derbyshire",
        "amber valley",
        "stratford on avon",
        "cumberland",
    }:
        return HtmlListPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if authority_key in {
        "east sussex",
        "kirklees",
        "nottinghamshire",
        "preston",
        "tandridge",
        "boston",
        "barrow",
        "central bedfordshire",
        "hampshire",
        "herefordshire",
        "ipswich",
        "ribble valley",
        "sedgemoor",
        "taunton deane",
    }:
        return QueryFormPlanningScraper(LegacyFormsCouncilConfig(authority=target.authority, base_url=base_url))
    if "idox" in portal_key:
        return IdoxPublicAccessScraper(
            IdoxCouncilConfig(
                authority=target.authority,
                base_url=base_url,
                application_root=idox_application_root(target.listing_url),
            )
        )
    if "ocella" in portal_key:
        return OcellaPlanningScraper(OcellaCouncilConfig(authority=target.authority, base_url=base_url))
    if "agile" in portal_key:
        return AgilePlanningScraper(AgileCouncilConfig(authority=target.authority, base_url=base_url))
    if "civica" in portal_key:
        return CivicaPlanningScraper(CivicaCouncilConfig(authority=target.authority, base_url=base_url))
    if "northgate" in portal_key or "planningexplorer" in portal_key:
        return NorthgatePlanningScraper(NorthgateCouncilConfig(authority=target.authority, base_url=base_url))

    generic_family = family if family and family != "unknown" else (scraper_type or "generic")
    return GenericLabelledPlanningScraper(
        GenericCouncilConfig(
            authority=target.authority,
            base_url=base_url,
            family=generic_family,
            uid_query_params=GENERIC_UID_QUERY_PARAMS,
            detail_markers=GENERIC_DETAIL_MARKERS,
        )
    )


def target_base_url(target: CouncilTarget) -> str:
    if target.base_url:
        return target.base_url.rstrip("/")
    if target.listing_url:
        parts = urlsplit(target.listing_url)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    raise ValueError("Council has no portal base URL")


def idox_application_root(listing_url: str | None) -> str:
    if not listing_url:
        return "/online-applications/"
    path = urlsplit(listing_url).path
    marker = "search.do"
    if marker not in path:
        return "/online-applications/"
    root = path[: path.index(marker)]
    return root if root.startswith("/") and root.endswith("/") else "/online-applications/"


def with_portal_metadata(
    application: PlanningApplication,
    stub: PlanningApplication,
    target: CouncilTarget,
    source_url: str,
) -> PlanningApplication:
    for field in (
        "reference",
        "address",
        "description",
        "status",
        "decision",
        "date_received",
        "date_validated",
        "applicant_name",
        "agent_name",
        "case_officer",
        "ward",
        "parish",
        "postcode",
    ):
        if not getattr(application, field) and getattr(stub, field):
            setattr(application, field, getattr(stub, field))
    application.raw = {
        **(stub.raw or {}),
        **(application.raw or {}),
        "portal_family": target.portal_family,
        "scraper_type": target.scraper_type,
        "portal_url": application.url or stub.url,
        "source_url": source_url,
    }
    if not application.source_url:
        application.source_url = source_url
    return application


def discover_planit_applications(authority: str, start_date: date, end_date: date) -> list[PlanningApplication]:
    with _PLANIT_CONCURRENCY_GATE:
        return _discover_planit_applications_serial(authority, start_date, end_date)


def _discover_planit_applications_serial(
    authority: str,
    start_date: date,
    end_date: date,
) -> list[PlanningApplication]:
    records: list[dict[str, object]] = []
    page_size = PLANIT_PAGE_SIZE
    page = 1
    while True:
        params = {
            "auth": authority,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "pg_sz": str(page_size),
            "page": str(page),
        }
        url = f"https://www.planit.org.uk/api/applics/json?{urlencode(params)}"
        payload = _fetch_json_with_retry(url)
        batch = payload.get("records", [])
        records.extend(batch)
        total = int(payload.get("total") or len(records))
        if not batch or len(records) >= total:
            break
        page += 1
    return [_application_from_planit_record(authority, record) for record in records]


def enrich_planit_application(application: PlanningApplication) -> PlanningApplication:
    return enrich_application_documents(application)


def enrich_application_documents(application: PlanningApplication) -> PlanningApplication:
    if application.documents:
        return application
    civica_documents = fetch_civica_documents_from_raw(application.raw or {}, source_url=application.url)
    if civica_documents:
        application.documents = civica_documents
        return application
    documents: list[PlanningDocument] = []
    seen: set[str] = set()
    for docs_url in application_document_source_urls(application):
        try:
            fetched = fetch_planit_documents(docs_url)
        except Exception:
            continue
        for document in fetched:
            if document.url in seen:
                continue
            seen.add(document.url)
            documents.append(document)
    if documents:
        application.documents = documents
    return application


def planit_document_source_urls(application: PlanningApplication) -> list[str]:
    return application_document_source_urls(application)


def application_document_source_urls(application: PlanningApplication) -> list[str]:
    candidates: list[str] = []

    def add(value: object, *, allow_listing: bool = False) -> None:
        if not value:
            return
        url = str(value)
        if not allow_listing and _looks_like_listing_url(url):
            return
        if url not in candidates:
            candidates.append(url)

    raw = application.raw or {}
    add(raw.get("docs_url"), allow_listing=True)
    for value in (raw.get("portal_url"), application.url, raw.get("source_url"), application.source_url):
        derived = document_source_url_from_application_url(str(value)) if value else None
        add(derived)
    add(raw.get("portal_url"))
    add(application.url)
    add(raw.get("source_url"))
    add(application.source_url)
    return candidates


def document_source_url_from_application_url(url: str) -> str | None:
    if not url or _looks_like_listing_url(url):
        return None
    parts = urlsplit(url)
    path = parts.path.casefold()
    if "applicationdetails.do" in path:
        query_items = parse_qsl(parts.query, keep_blank_values=True)
        if not any(name.casefold() == "keyval" for name, _value in query_items):
            return url
        updated: list[tuple[str, str]] = []
        replaced = False
        for name, value in query_items:
            if name.casefold() == "activetab":
                updated.append((name, "documents"))
                replaced = True
            else:
                updated.append((name, value))
        if not replaced:
            updated.insert(0, ("activeTab", "documents"))
        return _with_query_items(parts, updated)
    if any(marker in path for marker in ("/planning/display/", "/planning/application/", "/planningapplications/")):
        return url
    return url if _is_document_href(url) else None


def _looks_like_listing_url(url: str) -> bool:
    lowered = url.casefold()
    parts = urlsplit(url)
    path = parts.path.casefold().rstrip("/")
    if "applicationdetails.do" in path:
        return False
    if any(
        marker in lowered
        for marker in (
            "search.do?action=advanced",
            "search.do?action=simple",
            "search/advanced",
            "advancedsearch",
            "advanced-search",
            "public-register",
            "register-view",
            "weekly/monthly",
        )
    ):
        return True
    return path.endswith("/search") or path.endswith("/search/advanced")


def fetch_planit_documents(docs_url: str) -> list[PlanningDocument]:
    text, page_url, opener = _fetch_html_document_page(docs_url, timeout=45)
    document = html.fromstring(text)
    documents: list[PlanningDocument] = []
    seen: set[str] = set()
    for href, title in iter_document_links(document, page_url):
        absolute_url = urljoin(page_url, href)
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        documents.append(PlanningDocument(title=title, url=normalize_url(absolute_url), source_url=page_url))
    for publisher_document in fetch_publisher_document_list(text, page_url, opener):
        if publisher_document.url in seen:
            continue
        seen.add(publisher_document.url)
        documents.append(publisher_document)
    for arcus_document in fetch_arcus_salesforce_document_list(text, page_url, opener):
        if arcus_document.url in seen:
            continue
        seen.add(arcus_document.url)
        documents.append(arcus_document)
    for arcus_document in fetch_arcus_public_register_file_list(text, page_url, opener):
        if arcus_document.url in seen:
            continue
        seen.add(arcus_document.url)
        documents.append(arcus_document)
    for arcus_document in fetch_arcus_files_public_document_list(text, page_url, opener):
        if arcus_document.url in seen:
            continue
        seen.add(arcus_document.url)
        documents.append(arcus_document)
    return documents


def application_matches(
    application: PlanningApplication,
    start_date: date,
    end_date: date,
    keywords: list[str],
) -> bool:
    received = _parse_iso_date(application.date_received or application.date_validated)
    if received is None or received < start_date or received > end_date:
        return False
    if reference_is_excluded(application.reference or application.uid):
        return False
    if proposal_is_excluded(application.description):
        return False
    raw_text = " ".join(str(value) for value in application.raw.values()) if application.raw else ""
    haystack = " ".join(
        value
        for value in (application.reference, application.address, application.description, raw_text)
        if value
    ).casefold()
    return any(keyword.casefold() in haystack for keyword in keywords)


def reference_is_excluded(reference: str | None) -> bool:
    return bool(reference and reference.strip().casefold().startswith("old"))


def proposal_is_excluded(proposal: str | None) -> bool:
    if not proposal:
        return False
    normalized = re.sub(r"\s+", " ", proposal).strip().casefold()
    if normalized.startswith("t1"):
        return True
    if normalized.startswith("g1"):
        return True
    if "retrospective" in normalized and not re.search(r"\bpart\b", normalized):
        return True
    return any(phrase in normalized for phrase in EXCLUDED_PROPOSAL_PHRASES)


def application_in_geojson(application: PlanningApplication, user_geojson: dict[str, object]) -> bool:
    point = application_point(application)
    if not point:
        return False
    return any(point_in_geometry(point, geometry) for geometry in iter_feature_geometries(user_geojson))


def application_matches_search_area(application: PlanningApplication, user_geojson: dict[str, object]) -> bool:
    if not application_point(application):
        return True
    return application_in_geojson(application, user_geojson)


def application_point(application: PlanningApplication) -> tuple[float, float] | None:
    location = application.raw.get("location") if application.raw else None
    if isinstance(location, dict):
        coordinates = location.get("coordinates")
        if isinstance(coordinates, list) and len(coordinates) >= 2:
            return float(coordinates[0]), float(coordinates[1])
    longitude = (application.raw.get("longitude") or application.raw.get("location_x")) if application.raw else None
    latitude = (application.raw.get("latitude") or application.raw.get("location_y")) if application.raw else None
    if longitude is None or latitude is None:
        return None
    try:
        return float(longitude), float(latitude)
    except (TypeError, ValueError):
        return None


def create_lead_folder(output_dir: Path, council: str, application: PlanningApplication) -> Path:
    council_folder = output_dir / sanitize_path_part(council)
    reference = application.reference or application.uid
    lead_folder = council_folder / sanitize_path_part(reference)
    lead_folder.mkdir(parents=True, exist_ok=True)
    return lead_folder


def download_pdf_documents(
    documents: Iterable[PlanningDocument],
    destination: Path,
    *,
    log: LogCallback | None = None,
) -> int:
    downloaded = 0
    for index, document in enumerate(documents, start=1):
        if not _looks_like_downloadable_document(document):
            _log(log, f"Skipped excluded document link: {document.title}")
            continue
        try:
            downloaded_file = download_document_file(document)
            filename = document_filename(document, downloaded_file, fallback=f"document-{index}")
            path = _unique_path(destination / filename)
            path.write_bytes(downloaded_file.payload)
            downloaded += 1
            if DOCUMENT_DOWNLOAD_DELAY_SECONDS:
                sleep(DOCUMENT_DOWNLOAD_DELAY_SECONDS)
        except HTTPError as exc:
            if exc.code == 404:
                _log(log, f"Skipped unavailable document link: {document.title}")
            else:
                _log(log, f"Could not download {document.title}: HTTP {exc.code}")
        except Exception as exc:  # pragma: no cover - network resilience
            _log(log, f"Could not download {document.title}: {exc}")
    return downloaded


def write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    initialise_csv(csv_path, APPLICATION_CSV_FIELDS)
    append_csv_rows(csv_path, APPLICATION_CSV_FIELDS, rows)


def append_search_history(
    history_csv_path: Path,
    result: LeadSearchResult,
    keywords: list[str],
    started_at: datetime,
) -> None:
    history_csv_path.parent.mkdir(parents=True, exist_ok=True)
    relevant_percent = 0 if result.total_applications == 0 else (result.leads_found / result.total_applications) * 100
    row = {
        "Search Date": started_at.strftime("%Y-%m-%d"),
        "Search Time": started_at.strftime("%H:%M:%S"),
        "Keyword Set": keyword_set_name(keywords),
        "Total Applications": str(result.total_applications),
        "Relevant Captured Applications": str(result.leads_found),
        "% Relevant": f"{relevant_percent:.2f}%",
        "List of failed councils": "; ".join(result.failed_councils),
        "List of councils with no applications": "; ".join(result.no_application_councils),
        "Completion": result.completion,
        "Captured Documents": str(result.captured_documents),
    }
    file_exists = history_csv_path.exists() and history_csv_path.stat().st_size > 0
    with history_csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=HISTORY_CSV_FIELDS)
        if not file_exists:
            writer.writeheader()
        writer.writerow(row)


def keyword_set_name(keywords: list[str]) -> str:
    return "Standard" if _normalized_keyword_list(keywords) == _normalized_keyword_list(DEFAULT_KEYWORDS) else "Bespoke"


def _normalized_keyword_list(keywords: list[str]) -> list[str]:
    return [keyword.strip().casefold() for keyword in keywords if keyword.strip()]


def initialise_csv(csv_path: Path, fieldnames: list[str]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()


def append_csv_row(csv_path: Path, fieldnames: list[str], row: dict[str, str]) -> None:
    append_csv_rows(csv_path, fieldnames, [row])


def append_csv_rows(csv_path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    with csv_path.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writerows(rows)


def application_link(application: PlanningApplication) -> str:
    if application.raw:
        for key in ("portal_url", "docs_url", "source_url"):
            value = application.raw.get(key)
            if value:
                return str(value)
    if application.url:
        return application.url
    if application.source_url:
        return application.source_url
    return ""


def download_document_file(document: PlanningDocument) -> DownloadedFile:
    return _download_document_file(document)


def download_document_bytes(document: PlanningDocument) -> bytes:
    return download_document_file(document).payload


def _download_document_file(document: PlanningDocument) -> DownloadedFile:
    last_error: Exception | None = None
    opener = _build_document_opener()
    verify_tls = True
    tls_compat = False
    source_candidates: list[str] = []
    if document.source_url:
        source_candidates = source_document_candidates(document, opener)
    pending = deque([*source_candidates, *document_download_candidates(document.url)])
    seen: set[str] = set()
    while pending:
        url = pending.popleft()
        if url in seen:
            continue
        seen.add(url)
        headers = {"User-Agent": USER_AGENT}
        if document.source_url:
            headers["Referer"] = document.source_url
        request = Request(url, headers=headers)
        for attempt in range(4):
            try:
                _throttle_request(url)
                with opener.open(request, timeout=30) as response:
                    payload = response.read()
                    content_type = response.headers.get("Content-Type", "").lower()
                    final_url = response.geturl() if hasattr(response, "geturl") else url
                    if _is_downloaded_file(payload, content_type, final_url):
                        return DownloadedFile(
                            payload=payload,
                            final_url=final_url,
                            content_type=content_type,
                            filename=_filename_from_headers(response.headers),
                        )
                    if _looks_like_html(payload, content_type):
                        pending.extend(_document_links_from_html(payload, final_url))
                        break
                    raise ValueError(f"{url} did not return a downloadable file")
            except HTTPError as exc:
                last_error = exc
                if exc.code == 404:
                    break
                if exc.code not in RATE_LIMIT_HTTP_CODES or attempt == 3:
                    raise
                sleep(_retry_delay_seconds(exc, 5.0 * (attempt + 1)))
                _skip_next_throttle(url)
            except Exception as exc:
                last_error = exc
                if not tls_compat and _is_tls_compatibility_error(exc):
                    tls_compat = True
                    opener = _build_document_opener(tls_compat=True)
                    if document.source_url:
                        pending.extendleft(reversed(source_document_candidates(document, opener)))
                    seen.discard(url)
                    pending.appendleft(url)
                    break
                if verify_tls and _is_tls_certificate_error(exc):
                    verify_tls = False
                    opener = _build_document_opener(verify_tls=False)
                    if document.source_url:
                        pending.extendleft(reversed(source_document_candidates(document, opener)))
                    seen.discard(url)
                    pending.appendleft(url)
                    break
                break
    raise last_error or RuntimeError(f"Could not download {document.url}")


def source_document_candidates(document: PlanningDocument, opener) -> list[str]:
    if not document.source_url:
        return []
    try:
        text, page_url = _fetch_html_with_portal_session(document.source_url, opener, timeout=30)
    except Exception as exc:
        if not _is_tls_certificate_error(exc):
            return []
        try:
            fallback_opener = _build_document_opener(verify_tls=False)
            text, page_url = _fetch_html_with_portal_session(document.source_url, fallback_opener, timeout=30)
            opener = fallback_opener
        except Exception:
            return []
    candidates: list[PlanningDocument] = []
    try:
        page = html.fromstring(text)
    except Exception:
        page = None
    if page is not None:
        candidates.extend(
            PlanningDocument(title=title, url=normalize_url(urljoin(page_url, href)), source_url=page_url)
            for href, title in iter_document_links(page, page_url)
        )
    candidates.extend(fetch_publisher_document_list(text, page_url, opener))
    candidates.extend(fetch_arcus_salesforce_document_list(text, page_url, opener))
    candidates.extend(fetch_arcus_public_register_file_list(text, page_url, opener))
    candidates.extend(fetch_arcus_files_public_document_list(text, page_url, opener))
    wanted = _comparable_title(document.title)
    matching = [
        candidate.url
        for candidate in candidates
        if not wanted
        or _comparable_title(candidate.title) == wanted
        or wanted in _comparable_title(candidate.title)
        or _comparable_title(candidate.title) in wanted
    ]
    return list(dict.fromkeys(matching))


def document_download_candidates(url: str) -> list[str]:
    normalized = normalize_url(url)
    candidates = [normalized]
    lowered = normalized.lower()
    replacements = {
        "documentviewer.do": "documentdownload.do",
        "documentviewer": "documentdownload",
        "viewdocument": "downloadDocument",
    }
    for old, new in replacements.items():
        if old in lowered:
            candidates.append(re.sub(re.escape(old), new, normalized, flags=re.IGNORECASE))
    parts = urlsplit(normalized)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    has_module = any(name.casefold() == "module" for name, _value in query_items)
    has_keyval = any(name.casefold() == "keyval" for name, _value in query_items)
    if has_keyval and "documentdownload.do" in parts.path.casefold():
        if not has_module:
            candidates.append(_with_query_items(parts, [("module", "planning"), *query_items]))
    if has_keyval and "documentviewer.do" in parts.path.casefold():
        download_path = re.sub("documentviewer\\.do", "documentdownload.do", parts.path, flags=re.IGNORECASE)
        candidates.append(_with_query_items(parts._replace(path=download_path), query_items))
        if not has_module:
            candidates.append(_with_query_items(parts._replace(path=download_path), [("module", "planning"), *query_items]))
    if "download" not in parts.path.casefold():
        query = dict(query_items)
        file_id = query.get("id") or query.get("documentId") or query.get("documentid") or query.get("docid")
        if file_id and any(token in parts.path.casefold() for token in ("viewdocument", "view", "document")):
            candidates.append(urljoin(normalized, f"../DownloadDocument?{urlencode({'id': file_id})}"))
    return list(dict.fromkeys(candidates))


def iter_document_links(document: html.HtmlElement, page_url: str) -> Iterable[tuple[str, str]]:
    yield from iter_public_access_model_links(document, page_url)
    for anchor in document.xpath("//a[@href]"):
        href = anchor.get("href")
        absolute_url = urljoin(page_url, href)
        title = clean_text(" ".join(anchor.itertext())) or document_title_from_url(absolute_url)
        if _is_generic_site_document(href, title):
            continue
        if not _is_document_href(href) and not _is_document_link_text(title, href):
            continue
        if not _is_document_href(href) and _is_application_tab_href(href):
            continue
        yield href, title
    for attr in ("data-disabled-link", "data-link", "data-url", "data-href"):
        for element in document.xpath(f"//*[@{attr}]"):
            href = element.get(attr)
            if not _is_document_href(href):
                continue
            absolute_url = urljoin(page_url, href)
            title = clean_text(element.get("aria-label") or " ".join(element.itertext())) or document_title_from_url(absolute_url)
            title = re.sub(r"^link\s*\(\s*download\s*\)\s*", "", title, flags=re.IGNORECASE).strip()
            if _is_generic_site_document(href, title):
                continue
            yield href, title or document_title_from_url(absolute_url)
    for element in document.xpath("//*[@onclick]"):
        onclick = element.get("onclick") or ""
        for href in re.findall(r"['\"]([^'\"]+(?:document|download|attachment|viewDocument|showDocuments|displaymedia|displaysearchdocument|file)[^'\"]*)['\"]", onclick, flags=re.IGNORECASE):
            absolute_url = urljoin(page_url, href)
            title = clean_text(" ".join(element.itertext())) or document_title_from_url(absolute_url)
            if _is_generic_site_document(href, title):
                continue
            yield href, title
    for form in document.xpath("//form[@action]"):
        action = form.get("action") or ""
        if not _is_document_href(action):
            continue
        method = (form.get("method") or "get").casefold()
        if method != "get":
            continue
        query_items: list[tuple[str, str]] = []
        for input_node in form.xpath(".//input[@name]"):
            input_type = (input_node.get("type") or "hidden").casefold()
            if input_type in {"submit", "button", "image", "reset"}:
                continue
            query_items.append((input_node.get("name") or "", input_node.get("value") or ""))
        parts = urlsplit(urljoin(page_url, action))
        href = _with_query_items(parts, [*parse_qsl(parts.query, keep_blank_values=True), *query_items])
        title = clean_text(" ".join(form.itertext())) or document_title_from_url(href)
        if _is_generic_site_document(href, title):
            continue
        yield href, title
    for element in document.xpath("//iframe[@src] | //embed[@src] | //object[@data]"):
        href = element.get("src") or element.get("data")
        if not _is_document_href(href):
            continue
        absolute_url = urljoin(page_url, href)
        title = document_title_from_url(absolute_url)
        if _is_generic_site_document(href, title):
            continue
        yield href, title


def iter_public_access_model_links(document: html.HtmlElement, page_url: str) -> Iterable[tuple[str, str]]:
    text = html.tostring(document, encoding="unicode")
    match = re.search(r"var\s+model\s*=\s*(\{.*?\})\s*;", text, flags=re.DOTALL)
    if not match:
        return
    try:
        model = json.loads(match.group(1))
    except json.JSONDecodeError:
        return
    rows = model.get("Rows")
    if not isinstance(rows, list):
        return
    for row in rows:
        if not isinstance(row, dict):
            continue
        guid = row.get("Guid")
        if not guid:
            continue
        title = clean_text(str(row.get("Doc_Ref2") or row.get("Doc_Type") or "Document"))
        href = urljoin(page_url, f"../Document/ViewDocument?{urlencode({'id': str(guid)})}")
        yield href, title


def fetch_publisher_document_list(text: str, page_url: str, opener) -> list[PlanningDocument]:
    match = re.search(r'"url"\s*:\s*"([^"]*getDocumentList[^"]*)"', text)
    if not match:
        return []
    context_match = re.search(r"\bvar\s+ctx\s*=\s*['\"]([^'\"]+)['\"]", text)
    context_path = context_match.group(1).rstrip("/") if context_match else ""
    endpoint = urljoin(page_url, match.group(1))
    request = Request(
        endpoint,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": page_url,
            "X-Requested-With": "XMLHttpRequest",
        },
    )
    try:
        with _open_url_with_retry(request, timeout=45, opener=opener) as response:
            payload = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return []
    rows = data.get("data")
    if not isinstance(rows, list):
        return []
    documents: list[PlanningDocument] = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 2:
            continue
        link = next((str(value) for value in reversed(row) if isinstance(value, str) and _is_document_href(value)), None)
        if not link:
            continue
        if context_path and link.startswith("/"):
            link = f"{context_path}{link}"
        title = clean_text(str(row[3] if len(row) > 3 and row[3] else row[0] or "Document"))
        documents.append(
            PlanningDocument(
                title=title,
                url=normalize_url(urljoin(page_url, link)),
                document_type=clean_text(str(row[0])) if row and row[0] else None,
                date_published=clean_text(str(row[1])) if len(row) > 1 and row[1] else None,
                source_url=page_url,
            )
        )
    return documents


def fetch_arcus_salesforce_document_list(text: str, page_url: str, opener) -> list[PlanningDocument]:
    record_id = _salesforce_arcus_record_id(page_url)
    context = _salesforce_aura_context(text)
    if not record_id or not context:
        return []
    parts = urlsplit(page_url)
    path_prefix = str(context.get("pathPrefix") or _salesforce_path_prefix(parts.path)).rstrip("/")
    endpoint = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"{path_prefix}/s/sfsites/aura",
            urlencode({"r": "0", "other.LC_ARC_Files_Viewer.findContentVersionsForPlanning": "1"}),
            "",
        )
    )
    message = {
        "actions": [
            {
                "id": "1;a",
                "descriptor": "apex://LC_ARC_Files_Viewer/ACTION$findContentVersionsForPlanning",
                "callingDescriptor": "markup://c:ARC_Content_Version_Viewer",
                "params": {
                    "recordId": record_id,
                    "isLatest": True,
                    "showSensitveFiles": False,
                    "orderBy": "arcshared__Document_Date__c",
                    "sortType": "DESC",
                },
                "version": None,
            }
        ]
    }
    payload = urlencode(
        {
            "message": json.dumps(message, separators=(",", ":")),
            "aura.context": json.dumps(context, separators=(",", ":")),
            "aura.pageURI": parts.path,
            "aura.token": "null",
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": page_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with _open_url_with_retry(request, timeout=45, opener=opener) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    try:
        payload_data = json.loads(response_text)
    except json.JSONDecodeError:
        return []
    records: list[dict[str, object]] = []
    for action in payload_data.get("actions", []):
        if isinstance(action, dict) and isinstance(action.get("returnValue"), list):
            records.extend(action["returnValue"])
    return _salesforce_content_version_documents(records, page_url)


def fetch_arcus_public_register_file_list(text: str, page_url: str, opener) -> list[PlanningDocument]:
    record_id = _salesforce_arcus_record_id(page_url)
    context = _salesforce_aura_context(text)
    if not record_id or not context:
        return []
    parts = urlsplit(page_url)
    path_prefix = str(context.get("pathPrefix") or _salesforce_path_prefix(parts.path)).rstrip("/")
    endpoint = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"{path_prefix}/s/sfsites/aura",
            urlencode({"r": "0", "aura.ApexAction.execute": "1"}),
            "",
        )
    )
    message = {
        "actions": [
            {
                "id": "1;a",
                "descriptor": "aura://ApexActionController/ACTION$execute",
                "callingDescriptor": "UNKNOWN",
                "params": {
                    "namespace": "arcuscommunity",
                    "classname": "PR_FilesListCont",
                    "method": "getFiles",
                    "params": {"recordId": record_id, "registerName": None},
                    "cacheable": True,
                    "isContinuation": False,
                },
            }
        ]
    }
    payload = urlencode(
        {
            "message": json.dumps(message, separators=(",", ":")),
            "aura.context": json.dumps(context, separators=(",", ":")),
            "aura.pageURI": f"{parts.path}?tabset-ff68f=3",
            "aura.token": "null",
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": page_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with _open_url_with_retry(request, timeout=45, opener=opener) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    try:
        payload_data = json.loads(response_text)
    except json.JSONDecodeError:
        return []
    records: list[dict[str, object]] = []
    for action in payload_data.get("actions", []):
        if not isinstance(action, dict):
            continue
        return_value = action.get("returnValue")
        if isinstance(return_value, dict) and isinstance(return_value.get("returnValue"), list):
            records.extend(item for item in return_value["returnValue"] if isinstance(item, dict))
    return _salesforce_content_version_documents(records, page_url)


def _salesforce_content_version_documents(records: list[dict[str, object]], page_url: str) -> list[PlanningDocument]:
    parts = urlsplit(page_url)
    path_prefix = _salesforce_path_prefix(parts.path).rstrip("/")
    documents: list[PlanningDocument] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        version_id = str(record.get("Id") or "")
        if not version_id:
            continue
        extension = (clean_text(str(record.get("FileExtension") or "")) or "").lower()
        title = clean_text(str(record.get("Title") or "Document"))
        if extension and not title.casefold().endswith(f".{extension}"):
            title = f"{title}.{extension}"
        documents.append(
            PlanningDocument(
                title=title,
                url=normalize_url(f"{parts.scheme}://{parts.netloc}{path_prefix}/sfc/servlet.shepherd/version/download/{version_id}"),
                document_type=clean_text(str(record.get("Document_Type__c") or record.get("arcshared__Category__c") or record.get("FileType") or "")) or None,
                date_published=clean_text(str(record.get("arcshared__Document_Date__c") or "")) or None,
                file_size=str(record.get("ContentSize")) if record.get("ContentSize") else None,
                source_url=page_url,
            )
        )
    return documents


def fetch_arcus_files_public_document_list(text: str, page_url: str, opener) -> list[PlanningDocument]:
    record_id = _salesforce_arcus_record_id(page_url)
    context = _salesforce_aura_context(text)
    if not record_id or not context:
        return []
    parts = urlsplit(page_url)
    path_prefix = str(context.get("pathPrefix") or _salesforce_path_prefix(parts.path)).rstrip("/")
    endpoint = urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            f"{path_prefix}/s/sfsites/aura",
            urlencode({"r": "26", "arcshared.FilesPublicCont.getFiles": "1"}),
            "",
        )
    )
    message = {
        "actions": [
            {
                "id": "1;a",
                "descriptor": "apex://arcshared.FilesPublicCont/ACTION$getFiles",
                "callingDescriptor": "markup://arcshared:FilesPublic",
                "params": {"recordId": record_id, "config": "BE PR File Columns"},
                "version": None,
            }
        ]
    }
    payload = urlencode(
        {
            "message": json.dumps(message, separators=(",", ":")),
            "aura.context": json.dumps(context, separators=(",", ":")),
            "aura.pageURI": parts.path,
            "aura.token": "null",
        }
    ).encode("utf-8")
    request = Request(
        endpoint,
        data=payload,
        headers={
            "User-Agent": USER_AGENT,
            "Referer": page_url,
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest",
        },
        method="POST",
    )
    try:
        with _open_url_with_retry(request, timeout=45, opener=opener) as response:
            response_text = response.read().decode("utf-8", errors="replace")
    except Exception:
        return []
    try:
        payload_data = json.loads(response_text)
    except json.JSONDecodeError:
        return []
    records: list[dict[str, object]] = []
    for action in payload_data.get("actions", []):
        if not isinstance(action, dict):
            continue
        return_value = action.get("returnValue")
        if isinstance(return_value, list):
            records.extend(item for item in return_value if isinstance(item, dict))
    return _salesforce_content_version_documents(records, page_url)


def _salesforce_arcus_record_id(page_url: str) -> str | None:
    match = re.search(
        r"/s/(?:papplication|planning-application|detail)/([^/?#]+)",
        urlsplit(page_url).path,
        flags=re.IGNORECASE,
    )
    return unquote(match.group(1)) if match else None


def _salesforce_path_prefix(path: str) -> str:
    match = re.match(r"(.+?)/s/", path, flags=re.IGNORECASE)
    return match.group(1) if match else ""


def _salesforce_aura_context(text: str) -> dict[str, object] | None:
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
    return None


def document_title_from_url(url: str) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    for key in ("filename", "fileName", "documentName", "docName", "name", "file"):
        value = query.get(key)
        if value:
            return Path(unquote(value.replace("\\", "/"))).name
    return unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]) or "Document"


def document_filename(document: PlanningDocument, downloaded_file: DownloadedFile, *, fallback: str) -> str:
    extension = _downloaded_extension(downloaded_file)
    for value in (downloaded_file.filename, document.title, document_title_from_url(downloaded_file.final_url), fallback):
        if not value:
            continue
        filename = sanitize_path_part(Path(value.replace("\\", "/")).name)
        if filename and Path(filename).suffix:
            return filename
        if filename and extension:
            return f"{filename}{extension}"
        if filename:
            return filename
    return f"{fallback}{extension or '.bin'}"


def _document_links_from_html(payload: bytes, page_url: str) -> list[str]:
    text = payload.decode("utf-8", errors="replace")
    try:
        document = html.fromstring(text)
    except Exception:
        return []
    links = [urljoin(page_url, href) for href, _title in iter_document_links(document, page_url)]
    meta_refresh = document.xpath("//meta[translate(@http-equiv, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz')='refresh']/@content")
    for content in meta_refresh:
        match = re.search(r"url\s*=\s*([^;]+)", content, flags=re.IGNORECASE)
        if match:
            links.append(urljoin(page_url, match.group(1).strip(" '\"")))
    for href in re.findall(r"(?:window\.)?location(?:\.href)?\s*=\s*['\"]([^'\"]+)['\"]", text, flags=re.IGNORECASE):
        links.append(urljoin(page_url, href))
    return list(dict.fromkeys(normalize_url(link) for link in links))


def _is_document_href(href: str | None) -> bool:
    if not href:
        return False
    lowered = href.strip().lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
        return False
    if _is_generic_site_document(href, None):
        return False
    if any(token in lowered for token in ("userguide", "prior1997", "/templates/", "content?id=")):
        return False
    if _is_application_tab_href(href) or "search.do" in lowered:
        return False
    return any(
        marker in lowered
        for marker in (
            "document",
            "download",
            "attachment",
            "docview",
            "doclist",
            "showdocuments",
            "viewdocument",
            "displaysearchdocument",
            "wphappdocs",
            "wchdisplaymedia",
            "displaymedia",
            "showimage",
            "filedownload",
            "getfile",
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".ppt",
            ".pptx",
            ".odt",
            ".ods",
            ".zip",
            ".dwg",
            ".dxf",
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".bmp",
            ".webp",
            ".tif",
            ".tiff",
            ".rtf",
            ".txt",
        )
    )


def _is_generic_site_document(href: str | None, title: str | None) -> bool:
    text = " ".join(value for value in (href, title) if value).casefold()
    if not text:
        return False
    if "design and access" in text or "access statement" in text:
        return False
    generic_tokens = (
        "accessibility",
        "cookie",
        "privacy",
        "terms-and-conditions",
        "terms conditions",
        "terms%20and%20conditions",
        "site-map",
        "sitemap",
        "contact-us",
        "contact us",
        "complaints",
        "modern-slavery",
        "freedom-of-information",
        "foi",
        "userguide",
        "user-guide",
        "adobe",
        "acrobat",
    )
    return any(token in text for token in generic_tokens)


def _is_document_link_text(title: str, href: str | None) -> bool:
    normalized_title = title.casefold().strip()
    lowered_href = (href or "").casefold()
    if normalized_title not in {"documents", "view associated documents", "associated documents"}:
        return False
    return any(token in lowered_href for token in ("searchresult", "publicaccess", "documents", "runthirdpartysearch"))


def _is_application_tab_href(href: str | None) -> bool:
    if not href:
        return False
    lowered = href.strip().lower()
    return "applicationdetails.do" in lowered and "activetab=documents" in lowered


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%")
    query = quote(parts.query, safe="=&?/%:+,;@")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


def _with_query_items(parts, query_items: list[tuple[str, str]]) -> str:
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def iter_feature_geometries(geojson: dict[str, object]) -> Iterable[dict[str, object]]:
    if geojson.get("type") == "FeatureCollection":
        for feature in geojson.get("features", []):
            geometry = feature.get("geometry")
            if isinstance(geometry, dict):
                yield geometry
    elif geojson.get("type") == "Feature":
        geometry = geojson.get("geometry")
        if isinstance(geometry, dict):
            yield geometry
    else:
        yield geojson


def geometries_intersect(a: dict[str, object], b: dict[str, object]) -> bool:
    if not bboxes_intersect(geometry_bbox(a), geometry_bbox(b)):
        return False
    a_polygons = geometry_polygons(a)
    b_polygons = geometry_polygons(b)
    for polygon_a in a_polygons:
        for polygon_b in b_polygons:
            if polygons_intersect(polygon_a, polygon_b):
                return True
    return False


def point_in_geometry(point: tuple[float, float], geometry: dict[str, object]) -> bool:
    return any(point_in_polygon(point, polygon) for polygon in geometry_polygons(geometry))


def geometry_polygons(geometry: dict[str, object]) -> list[list[list[float]]]:
    geometry_type = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if geometry_type == "Polygon":
        return [coordinates] if isinstance(coordinates, list) else []
    if geometry_type == "MultiPolygon" and isinstance(coordinates, list):
        return [polygon for polygon in coordinates if isinstance(polygon, list)]
    return []


def polygons_intersect(a: list[list[float]], b: list[list[float]]) -> bool:
    a_ring = a[0] if a else []
    b_ring = b[0] if b else []
    if not a_ring or not b_ring:
        return False
    if any(point_in_polygon((point[0], point[1]), b) for point in a_ring):
        return True
    if any(point_in_polygon((point[0], point[1]), a) for point in b_ring):
        return True
    return any(segments_intersect(edge_a[0], edge_a[1], edge_b[0], edge_b[1]) for edge_a in ring_edges(a_ring) for edge_b in ring_edges(b_ring))


def point_in_polygon(point: tuple[float, float], polygon: list[list[float]]) -> bool:
    outer = polygon[0] if polygon else []
    if not point_in_ring(point, outer):
        return False
    holes = polygon[1:]
    return not any(point_in_ring(point, hole) for hole in holes)


def point_in_ring(point: tuple[float, float], ring: list[list[float]]) -> bool:
    x, y = point
    inside = False
    if len(ring) < 3:
        return False
    j = len(ring) - 1
    for i, current in enumerate(ring):
        xi, yi = current[0], current[1]
        xj, yj = ring[j][0], ring[j][1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def ring_edges(ring: list[list[float]]):
    for index in range(len(ring)):
        yield ring[index], ring[(index + 1) % len(ring)]


def segments_intersect(a1, a2, b1, b2) -> bool:
    def orient(p, q, r):
        return (q[1] - p[1]) * (r[0] - q[0]) - (q[0] - p[0]) * (r[1] - q[1])

    o1 = orient(a1, a2, b1)
    o2 = orient(a1, a2, b2)
    o3 = orient(b1, b2, a1)
    o4 = orient(b1, b2, a2)
    return (o1 > 0) != (o2 > 0) and (o3 > 0) != (o4 > 0)


def geometry_bbox(geometry: dict[str, object]) -> tuple[float, float, float, float]:
    points: list[list[float]] = []
    for polygon in geometry_polygons(geometry):
        for ring in polygon:
            points.extend(ring)
    if not points:
        return (0, 0, 0, 0)
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def bboxes_intersect(a: tuple[float, float, float, float], b: tuple[float, float, float, float]) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def sanitize_path_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "untitled"


def _application_from_planit_record(authority: str, record: dict[str, object]) -> PlanningApplication:
    other_fields = record.get("other_fields") if isinstance(record.get("other_fields"), dict) else {}
    reference = _record_string(record, "uid") or _record_string(record, "reference") or _record_string(record, "name")
    raw = {
        "source": "planit",
        "docs_url": _record_string(other_fields, "docs_url"),
        "source_url": _record_string(other_fields, "source_url"),
        "portal_url": _record_string(record, "url"),
        "location": record.get("location"),
        "longitude": record.get("location_x"),
        "latitude": record.get("location_y"),
    }
    return PlanningApplication(
        authority=authority,
        uid=reference or _record_string(record, "name") or "unknown",
        url=_record_string(record, "url") or _record_string(record, "link") or "",
        reference=reference,
        address=_record_string(record, "address"),
        description=_record_string(record, "description"),
        status=_record_string(other_fields, "status") or _record_string(record, "app_state"),
        date_received=_record_string(other_fields, "date_received") or _record_string(record, "start_date"),
        date_validated=_record_string(other_fields, "date_validated"),
        applicant_name=_record_string(other_fields, "applicant_name") or _record_string(other_fields, "applicant_address"),
        agent_name=_record_string(other_fields, "agent_name") or _record_string(other_fields, "agent_address"),
        case_officer=_record_string(other_fields, "case_officer"),
        parish=_record_string(other_fields, "parish"),
        postcode=_record_string(record, "postcode"),
        source_url=_record_string(other_fields, "source_url"),
        raw={key: value for key, value in raw.items() if value},
    )


def _open_url_with_retry(request: Request, *, timeout: float, opener=None):
    for attempt in range(4):
        try:
            _throttle_request(request.full_url)
            if opener is not None:
                return opener.open(request, timeout=timeout)
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code not in RATE_LIMIT_HTTP_CODES or attempt == 3:
                raise
            sleep(_retry_delay_seconds(exc, 5.0 * (attempt + 1)))
            _skip_next_throttle(request.full_url)
    raise RuntimeError(f"Could not fetch {request.full_url}")


def _build_document_opener(*, verify_tls: bool = True, tls_compat: bool = False):
    handlers = [HTTPCookieProcessor(CookieJar())]
    if not verify_tls:
        handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
    elif tls_compat:
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT:@SECLEVEL=1")
        handlers.append(HTTPSHandler(context=context))
    return build_opener(*handlers)


def _fetch_html_with_portal_session(url: str, opener, *, timeout: float) -> tuple[str, str]:
    request = Request(url, headers={"User-Agent": USER_AGENT})
    with _open_url_with_retry(request, timeout=timeout, opener=opener) as response:
        text = response.read().decode("utf-8", errors="replace")
        page_url = response.geturl()
    accept_url = _disclaimer_accept_url(text, page_url)
    if accept_url:
        accept_request = Request(accept_url, data=b"", headers={"User-Agent": USER_AGENT}, method="POST")
        with _open_url_with_retry(accept_request, timeout=timeout, opener=opener) as response:
            text = response.read().decode("utf-8", errors="replace")
            page_url = response.geturl()
    return text, page_url


def _fetch_html_document_page(url: str, *, timeout: float):
    opener = _build_document_opener()
    try:
        text, page_url = _fetch_html_with_portal_session(url, opener, timeout=timeout)
        return text, page_url, opener
    except Exception as exc:
        if _is_tls_compatibility_error(exc):
            opener = _build_document_opener(tls_compat=True)
            text, page_url = _fetch_html_with_portal_session(url, opener, timeout=timeout)
            return text, page_url, opener
        if not _is_tls_certificate_error(exc):
            raise
    opener = _build_document_opener(verify_tls=False)
    text, page_url = _fetch_html_with_portal_session(url, opener, timeout=timeout)
    return text, page_url, opener


def _disclaimer_accept_url(text: str, page_url: str) -> str | None:
    try:
        document = html.fromstring(text)
    except Exception:
        return None
    for form in document.xpath("//form[@action]"):
        action = form.get("action") or ""
        if "/disclaimer/accept" in action.casefold():
            return urljoin(page_url, action)
    return None


def _fetch_json_with_retry(url: str) -> dict[str, object]:
    attempts = 4
    tls_compat_opener = None
    for attempt in range(attempts):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            _throttle_request(url)
            if tls_compat_opener is not None:
                response_context = tls_compat_opener.open(request, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
            else:
                response_context = urlopen(request, timeout=DEFAULT_REQUEST_TIMEOUT_SECONDS)
            with response_context as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            if exc.code not in RATE_LIMIT_HTTP_CODES or attempt == attempts - 1:
                raise
            sleep(min(_retry_delay_seconds(exc, 2.0 * (attempt + 1)), 8.0))
            _skip_next_throttle(url)
        except URLError as exc:
            if not _should_retry_json_without_tls_verification(url, exc) or attempt == attempts - 1:
                raise
            _skip_next_throttle(url)
            tls_compat_opener = build_opener(HTTPSHandler(context=ssl._create_unverified_context()))
    raise RuntimeError(f"Could not fetch public planning metadata: {url}")


def _should_retry_json_without_tls_verification(url: str, exc: URLError) -> bool:
    if not urlsplit(url).netloc.casefold().endswith("planit.org.uk"):
        return False
    reason = getattr(exc, "reason", None)
    return isinstance(reason, ssl.SSLCertVerificationError)


def _throttle_request(url: str) -> None:
    netloc = urlsplit(url).netloc.casefold()
    if not netloc:
        return
    delay = PLANIT_REQUEST_THROTTLE_SECONDS if netloc.endswith("planit.org.uk") else REQUEST_THROTTLE_SECONDS
    with _REQUEST_THROTTLE_LOCK:
        now = monotonic()
        wait = delay - (now - _LAST_REQUEST_AT.get(netloc, 0.0))
        if wait > 0:
            sleep(wait)
            now = monotonic()
        _LAST_REQUEST_AT[netloc] = now


def _skip_next_throttle(url: str) -> None:
    netloc = urlsplit(url).netloc.casefold()
    if not netloc:
        return
    delay = PLANIT_REQUEST_THROTTLE_SECONDS if netloc.endswith("planit.org.uk") else REQUEST_THROTTLE_SECONDS
    with _REQUEST_THROTTLE_LOCK:
        _LAST_REQUEST_AT[netloc] = monotonic() - delay


def _retry_delay_seconds(exc: HTTPError, fallback_seconds: float) -> float:
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            return min(max(float(retry_after), 0.0), MAX_RETRY_AFTER_SECONDS)
        except ValueError:
            try:
                retry_after_date = parsedate_to_datetime(retry_after)
            except (TypeError, ValueError):
                pass
            else:
                if retry_after_date.tzinfo is None:
                    retry_after_date = retry_after_date.replace(tzinfo=timezone.utc)
                delay = (retry_after_date - datetime.now(timezone.utc)).total_seconds()
                return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)
    return fallback_seconds


def _record_string(record: object, key: str) -> str | None:
    if not isinstance(record, dict):
        return None
    value = record.get(key)
    if value in (None, ""):
        return None
    return str(value).strip()


def _parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _looks_like_downloadable_document(document: PlanningDocument) -> bool:
    if _is_excluded_document(document):
        return False
    text = _document_filter_text(document)
    return (
        bool(document.url)
        and (
            _is_document_href(document.url)
            or any(extension in text for extension in _known_file_extensions())
            or (document.document_type or "").casefold() in {"pdf", "document", "drawing", "plan", "supporting_document", "image"}
        )
    )


def _is_excluded_document(document: PlanningDocument) -> bool:
    text = _document_filter_text(document)
    if ".exe" in text or _path_suffix(document.url) == ".exe":
        return True
    return "existing" in text and "proposed" not in text


def _document_filter_text(document: PlanningDocument) -> str:
    values = [
        document.title,
        document.url,
        document.description,
        document.document_type,
        document.source_url,
        document_title_from_url(document.url) if document.url else "",
    ]
    return " ".join(unquote(str(value).replace("+", " ")) for value in values if value).casefold()


def _path_suffix(url: str | None) -> str:
    if not url:
        return ""
    return Path(unquote(urlsplit(url).path).replace("\\", "/")).suffix.casefold()


def _is_tls_certificate_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        return isinstance(reason, ssl.SSLCertVerificationError) or "certificate" in str(reason).casefold()
    return "certificate" in str(exc).casefold() and "ssl" in str(exc).casefold()


def _is_tls_compatibility_error(exc: Exception) -> bool:
    text = str(exc).casefold()
    if isinstance(exc, URLError):
        text = f"{exc.reason} {exc}".casefold()
    return "forcibly closed" in text or "winerror 10054" in text or "sslv3 alert handshake failure" in text


def _comparable_title(value: str | None) -> str:
    if not value:
        return ""
    path = Path(value.replace("\\", "/"))
    text = path.stem if path.suffix.lower() in _known_file_extensions() else value
    text = re.sub(r"\.[a-z0-9]{2,5}$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"[^a-z0-9]+", " ", text.casefold())
    return re.sub(r"\s+", " ", text).strip()


def _filename_from_headers(headers: Message) -> str | None:
    filename = headers.get_filename() if hasattr(headers, "get_filename") else None
    if filename:
        return Path(unquote(filename.replace("\\", "/"))).name
    disposition = headers.get("Content-Disposition", "")
    match = re.search(r"filename\*?=(?:UTF-8''|\"?)([^\";]+)", disposition, flags=re.IGNORECASE)
    if match:
        return Path(unquote(match.group(1).strip().strip('"').replace("\\", "/"))).name
    return None


def _downloaded_extension(downloaded_file: DownloadedFile) -> str:
    filename = downloaded_file.filename or document_title_from_url(downloaded_file.final_url)
    suffix = Path(filename.replace("\\", "/")).suffix.lower()
    if suffix in _known_file_extensions():
        return suffix
    path_suffix = Path(urlsplit(downloaded_file.final_url).path).suffix.lower()
    if path_suffix in _known_file_extensions():
        return path_suffix
    return _extension_from_content_type(downloaded_file.content_type) or _extension_from_signature(downloaded_file.payload) or ".bin"


def _known_file_extensions() -> set[str]:
    return {
        ".pdf",
        ".doc",
        ".docx",
        ".xls",
        ".xlsx",
        ".ppt",
        ".pptx",
        ".odt",
        ".ods",
        ".rtf",
        ".txt",
        ".csv",
        ".zip",
        ".dwg",
        ".dxf",
        ".jpg",
        ".jpeg",
        ".png",
        ".gif",
        ".bmp",
        ".webp",
        ".tif",
        ".tiff",
        ".heic",
    }


def _extension_from_content_type(content_type: str) -> str | None:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "application/vnd.ms-powerpoint": ".ppt",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
        "application/vnd.oasis.opendocument.text": ".odt",
        "application/vnd.oasis.opendocument.spreadsheet": ".ods",
        "application/zip": ".zip",
        "application/x-zip-compressed": ".zip",
        "application/acad": ".dwg",
        "application/x-acad": ".dwg",
        "application/autocad_dwg": ".dwg",
        "image/vnd.dwg": ".dwg",
        "image/x-dwg": ".dwg",
        "application/dxf": ".dxf",
        "application/x-dxf": ".dxf",
        "image/vnd.dxf": ".dxf",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/bmp": ".bmp",
        "image/webp": ".webp",
        "image/tiff": ".tif",
        "image/heic": ".heic",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
        "text/plain": ".txt",
        "text/csv": ".csv",
    }.get(content_type)


def _extension_from_signature(payload: bytes) -> str | None:
    start = payload[:16]
    if start.startswith(b"%PDF"):
        return ".pdf"
    if start.startswith(b"PK\x03\x04"):
        return ".docx"
    if start.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if start.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if start[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if start[:4] in (b"II*\x00", b"MM\x00*"):
        return ".tif"
    if start.startswith(b"{\\rtf"):
        return ".rtf"
    if start.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return ".doc"
    return None


def _is_downloaded_file(payload: bytes, content_type: str, final_url: str) -> bool:
    if not payload:
        return False
    if payload[:2] == b"MZ" or _path_suffix(final_url) == ".exe":
        return False
    normalized_content_type = content_type.split(";", 1)[0].strip().lower()
    if normalized_content_type in {
        "application/json",
        "text/json",
        "application/javascript",
        "text/javascript",
        "application/x-msdownload",
        "application/vnd.microsoft.portable-executable",
    }:
        return False
    if _extension_from_signature(payload):
        return True
    if _looks_like_html(payload, content_type):
        return False
    if _extension_from_content_type(content_type):
        return True
    if Path(urlsplit(final_url).path).suffix.lower() in _known_file_extensions():
        return True
    return normalized_content_type.startswith("application/") or normalized_content_type.startswith("image/")


def _looks_like_html(payload: bytes, content_type: str) -> bool:
    if "html" in content_type.lower():
        return True
    stripped = payload.lstrip().lower()
    return stripped.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    stem = path.stem
    suffix = path.suffix
    for index in range(2, 1000):
        candidate = path.with_name(f"{stem}-{index}{suffix}")
        if not candidate.exists():
            return candidate
    raise FileExistsError(path)


def _log(callback: LogCallback | None, message: str) -> None:
    if callback:
        callback(message)


def _progress(callback: ProgressCallback | None, completed: int, total: int) -> None:
    if callback:
        callback(completed, total)


def _captured(callback: CapturedCallback | None, count: int) -> None:
    if callback:
        callback(count)
