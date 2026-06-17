from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from time import sleep
from typing import Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import Request, urlopen

from lxml import html

from lead_generator.planning.adapters.agile import AgileCouncilConfig, AgilePlanningScraper
from lead_generator.planning.adapters.civica import CivicaCouncilConfig, CivicaPlanningScraper
from lead_generator.planning.adapters.idox import IdoxCouncilConfig, IdoxPublicAccessScraper
from lead_generator.planning.adapters.northgate import NorthgateCouncilConfig, NorthgatePlanningScraper
from lead_generator.planning.adapters.ocella import OcellaCouncilConfig, OcellaPlanningScraper
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.portals import detect_portal_family


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


COUNCIL_NAME_KEYS = (
    "authority",
    "authority_name",
    "council",
    "council_name",
    "name",
    "area_name",
    "lad_name",
    "LAD23NM",
    "LAD22NM",
    "LAD21NM",
)
PORTAL_FAMILY_KEYS = ("portal_family", "portal", "portal_type", "planning_portal")
BASE_URL_KEYS = ("base_url", "portal_base_url", "planning_base_url", "planning_url", "url")
LISTING_URL_KEYS = ("listing_url", "search_url", "planning_search_url", "weekly_list_url")


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class CouncilTarget:
    authority: str
    portal_family: str
    base_url: str
    listing_url: str | None = None
    source: str = "geojson"


@dataclass(frozen=True, slots=True)
class LeadSearchConfig:
    geojson_path: Path
    output_root: Path
    start_date: date
    end_date: date
    keywords: list[str]


@dataclass(slots=True)
class LeadSearchResult:
    output_dir: Path
    csv_path: Path
    geojson_features: int
    councils_total: int
    councils_completed: int
    leads_found: int


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


def load_council_targets(geojson_path: Path) -> list[CouncilTarget]:
    return load_council_targets_with_stats(geojson_path)[0]


def load_council_targets_with_stats(geojson_path: Path) -> tuple[list[CouncilTarget], int, list[str]]:
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = data.get("features", [])
    targets: list[CouncilTarget] = []
    skipped: list[str] = []
    for feature in features:
        properties = feature.get("properties") or {}
        authority = _first_property(properties, COUNCIL_NAME_KEYS)
        base_url = _first_property(properties, BASE_URL_KEYS)
        listing_url = _first_property(properties, LISTING_URL_KEYS)
        portal_family = _first_property(properties, PORTAL_FAMILY_KEYS)

        if not authority:
            skipped.append("feature without a council name")
            continue
        if not base_url:
            targets.append(
                CouncilTarget(
                    authority=authority,
                    portal_family="planit",
                    base_url="https://www.planit.org.uk",
                    source="public planning metadata",
                )
            )
            continue
        if not portal_family:
            portal_family = detect_portal_family("", f"{base_url} {listing_url or ''}")
        if not portal_family:
            skipped.append(f"{authority}: no portal family could be detected")
            continue

        targets.append(
            CouncilTarget(
                authority=authority,
                portal_family=portal_family.lower(),
                base_url=base_url,
                listing_url=listing_url,
                source="geojson portal metadata",
            )
        )
    return targets, len(features), skipped


def run_lead_search(
    config: LeadSearchConfig,
    *,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> LeadSearchResult:
    targets, feature_count, skipped = load_council_targets_with_stats(config.geojson_path)
    output_dir = config.output_root / date.today().isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "applications.csv"
    rows: list[dict[str, str]] = []
    completed = 0

    _log(log, f"Read {feature_count} GeoJSON features from {config.geojson_path.name}")
    _log(log, f"Prepared {len(targets)} council search targets")
    public_count = sum(1 for target in targets if target.portal_family == "planit")
    if public_count:
        _log(log, f"{public_count} councils will use public planning metadata because no portal URL was supplied")
    for message in skipped[:10]:
        _log(log, f"Skipped {message}")
    if len(skipped) > 10:
        _log(log, f"Skipped {len(skipped) - 10} more features without usable council metadata")
    if not targets:
        raise ValueError(
            "No councils could be searched. The GeoJSON needs council names in feature properties; "
            "portal URLs are optional because the app can fall back to public planning metadata."
        )
    _progress(progress, completed, len(targets))

    for target in targets:
        if should_cancel and should_cancel():
            _log(log, "Run cancelled.")
            break

        _log(log, f"Searching {target.authority} ({target.portal_family})")
        try:
            scraper = build_scraper(target)
            discovery = discover_applications(scraper, target, config.start_date, config.end_date)
            discovery = list(discovery)
            _log(log, f"{target.authority}: found {len(discovery)} received applications in the date range")
            matched_count = 0
            for stub in discovery:
                if should_cancel and should_cancel():
                    break
                try:
                    application = fetch_application(scraper, target, stub)
                except Exception as exc:  # pragma: no cover - live-site resilience
                    _log(log, f"{target.authority}: failed to fetch {stub.reference or stub.uid}: {exc}")
                    continue

                if not application_matches(application, config.start_date, config.end_date, config.keywords):
                    continue

                matched_count += 1
                lead_folder = create_lead_folder(output_dir, target.authority, application)
                download_pdf_documents(application.documents, lead_folder, log=log)
                rows.append(
                    {
                        "Reference": application.reference or application.uid,
                        "proposal": application.description or "",
                        "date received": application.date_received or "",
                        "council": target.authority,
                    }
                )
                _log(log, f"{target.authority}: saved {application.reference or application.uid}")
            _log(log, f"{target.authority}: {matched_count} applications matched the keywords")
        except Exception as exc:  # pragma: no cover - live-site resilience
            _log(log, f"{target.authority}: failed: {exc}")
        finally:
            completed += 1
            _progress(progress, completed, len(targets))

    write_csv(csv_path, rows)
    _log(log, f"Finished. Saved {len(rows)} leads to {csv_path}")
    return LeadSearchResult(
        output_dir=output_dir,
        csv_path=csv_path,
        geojson_features=feature_count,
        councils_total=len(targets),
        councils_completed=completed,
        leads_found=len(rows),
    )


def build_scraper(target: CouncilTarget):
    family = target.portal_family.lower()
    if family == "planit":
        return None
    if family == "idox":
        return IdoxPublicAccessScraper(IdoxCouncilConfig(target.authority, target.base_url))
    if family == "ocella":
        return OcellaPlanningScraper(OcellaCouncilConfig(target.authority, target.base_url))
    if family == "civica":
        return CivicaPlanningScraper(CivicaCouncilConfig(target.authority, target.base_url))
    if family == "agile":
        return AgilePlanningScraper(AgileCouncilConfig(target.authority, target.base_url))
    if family == "northgate":
        return NorthgatePlanningScraper(NorthgateCouncilConfig(target.authority, target.base_url))
    raise ValueError(f"Unsupported portal family: {target.portal_family}")


def discover_applications(scraper, target: CouncilTarget, start_date: date, end_date: date) -> Iterable[PlanningApplication]:
    if target.portal_family == "planit":
        return discover_planit_applications(target.authority, start_date, end_date)
    if target.portal_family == "idox":
        return scraper.discover_ids(
            listing_url=target.listing_url,
            start_date=start_date,
            end_date=end_date,
        ).applications
    if not target.listing_url:
        raise ValueError("listing_url is required for non-Idox councils")
    return scraper.discover_ids(listing_url=target.listing_url).applications


def fetch_application(scraper, target: CouncilTarget, stub: PlanningApplication) -> PlanningApplication:
    if target.portal_family == "planit":
        return enrich_planit_application(stub)
    return scraper.fetch_application(
        stub.uid,
        stub.url,
        include_documents=True,
    )


def discover_planit_applications(authority: str, start_date: date, end_date: date) -> list[PlanningApplication]:
    records: list[dict[str, object]] = []
    page_size = 100
    offset = 0
    while True:
        params = {
            "auth": authority,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "pg_sz": str(page_size),
            "from": str(offset),
        }
        url = f"https://www.planit.org.uk/api/applics/json?{urlencode(params)}"
        payload = _fetch_json_with_retry(url)
        batch = payload.get("records", [])
        records.extend(batch)
        total = int(payload.get("total") or len(records))
        next_offset = int(payload.get("to") or offset) + 1
        if not batch or next_offset >= total:
            break
        offset = next_offset

    return [_application_from_planit_record(authority, record) for record in records]


def _fetch_json_with_retry(url: str) -> dict[str, object]:
    last_error: Exception | None = None
    for attempt in range(4):
        request = Request(url, headers={"User-Agent": "LeadGeneratorPlanningScraper/0.1"})
        try:
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            last_error = exc
            if exc.code != 429 or attempt == 3:
                raise
            sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch public planning metadata: {last_error}")


def enrich_planit_application(application: PlanningApplication) -> PlanningApplication:
    docs_url = application.raw.get("docs_url") if application.raw else None
    if docs_url:
        application.documents = fetch_planit_documents(str(docs_url))
    return application


def fetch_planit_documents(docs_url: str) -> list[PlanningDocument]:
    request = Request(docs_url, headers={"User-Agent": "LeadGeneratorPlanningScraper/0.1"})
    with urlopen(request, timeout=45) as response:
        text = response.read().decode("utf-8", errors="replace")
        page_url = response.geturl()
    document = html.fromstring(text)
    documents: list[PlanningDocument] = []
    seen: set[str] = set()
    for anchor in document.xpath("//a[@href]"):
        href = anchor.get("href")
        absolute_url = urljoin(page_url, href)
        title = " ".join(anchor.itertext()).strip() or absolute_url.rsplit("/", 1)[-1]
        if ".pdf" not in f"{href} {title}".lower():
            continue
        if absolute_url in seen:
            continue
        seen.add(absolute_url)
        documents.append(PlanningDocument(title=title, url=absolute_url))
    return documents


def _application_from_planit_record(authority: str, record: dict[str, object]) -> PlanningApplication:
    other_fields = record.get("other_fields") if isinstance(record.get("other_fields"), dict) else {}
    reference = _record_string(record, "uid") or _record_string(record, "reference") or _record_string(record, "name")
    date_received = _record_string(other_fields, "date_received") or _record_string(record, "start_date")
    raw = {
        "source": "planit",
        "docs_url": _record_string(other_fields, "docs_url"),
        "source_url": _record_string(other_fields, "source_url"),
        "portal_url": _record_string(record, "url"),
    }
    return PlanningApplication(
        authority=authority,
        uid=reference or _record_string(record, "name") or "unknown",
        url=_record_string(record, "url") or _record_string(record, "link") or "",
        reference=reference,
        address=_record_string(record, "address"),
        description=_record_string(record, "description"),
        status=_record_string(other_fields, "status") or _record_string(record, "app_state"),
        date_received=date_received,
        date_validated=_record_string(other_fields, "date_validated"),
        applicant_name=_record_string(other_fields, "applicant_name") or _record_string(other_fields, "applicant_address"),
        agent_name=_record_string(other_fields, "agent_name") or _record_string(other_fields, "agent_address"),
        case_officer=_record_string(other_fields, "case_officer"),
        parish=_record_string(other_fields, "parish"),
        postcode=_record_string(record, "postcode"),
        source_url=_record_string(other_fields, "source_url"),
        raw={key: value for key, value in raw.items() if value},
    )


def application_matches(
    application: PlanningApplication,
    start_date: date,
    end_date: date,
    keywords: list[str],
) -> bool:
    received = _parse_iso_date(application.date_received)
    if received is None or received < start_date or received > end_date:
        return False
    raw_text = " ".join(str(value) for value in application.raw.values()) if application.raw else ""
    haystack = " ".join(
        value
        for value in (
            application.reference,
            application.address,
            application.description,
            raw_text,
        )
        if value
    ).casefold()
    return any(keyword.casefold() in haystack for keyword in keywords)


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
        if not _looks_like_pdf(document):
            continue
        filename = sanitize_path_part(document.title or f"document-{index}.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        path = _unique_path(destination / filename)
        try:
            request = Request(document.url, headers={"User-Agent": "LeadGeneratorPlanningScraper/0.1"})
            with urlopen(request, timeout=30) as response:
                path.write_bytes(response.read())
            downloaded += 1
        except Exception as exc:  # pragma: no cover - network resilience
            _log(log, f"Could not download {document.title}: {exc}")
    return downloaded


def write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["Reference", "proposal", "date received", "council"])
        writer.writeheader()
        writer.writerows(rows)


def sanitize_path_part(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', " ", value)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:120] or "untitled"


def _first_property(properties: dict[str, object], keys: tuple[str, ...]) -> str | None:
    lower_map = {key.lower(): value for key, value in properties.items()}
    for key in keys:
        value = properties.get(key)
        if value is None:
            value = lower_map.get(key.lower())
        if value not in (None, ""):
            return str(value).strip()
    return None


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


def _looks_like_pdf(document: PlanningDocument) -> bool:
    text = f"{document.title} {urlsplit(document.url).path}".lower()
    return ".pdf" in text or document.document_type == "pdf"


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
