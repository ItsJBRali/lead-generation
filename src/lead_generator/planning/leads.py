from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

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
    data = json.loads(geojson_path.read_text(encoding="utf-8"))
    features = data.get("features", [])
    targets: list[CouncilTarget] = []
    for feature in features:
        properties = feature.get("properties") or {}
        authority = _first_property(properties, COUNCIL_NAME_KEYS)
        base_url = _first_property(properties, BASE_URL_KEYS)
        listing_url = _first_property(properties, LISTING_URL_KEYS)
        portal_family = _first_property(properties, PORTAL_FAMILY_KEYS)

        if not authority or not base_url:
            continue
        if not portal_family:
            portal_family = detect_portal_family("", f"{base_url} {listing_url or ''}")
        if not portal_family:
            continue

        targets.append(
            CouncilTarget(
                authority=authority,
                portal_family=portal_family.lower(),
                base_url=base_url,
                listing_url=listing_url,
            )
        )
    return targets


def run_lead_search(
    config: LeadSearchConfig,
    *,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> LeadSearchResult:
    targets = load_council_targets(config.geojson_path)
    output_dir = config.output_root / date.today().isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "applications.csv"
    rows: list[dict[str, str]] = []
    completed = 0

    _log(log, f"Loaded {len(targets)} councils from {config.geojson_path.name}")
    _progress(progress, completed, len(targets))

    for target in targets:
        if should_cancel and should_cancel():
            _log(log, "Run cancelled.")
            break

        _log(log, f"Searching {target.authority} ({target.portal_family})")
        try:
            scraper = build_scraper(target)
            discovery = discover_applications(scraper, target, config.start_date, config.end_date)
            for stub in discovery:
                if should_cancel and should_cancel():
                    break
                try:
                    application = scraper.fetch_application(
                        stub.uid,
                        stub.url,
                        include_documents=True,
                    )
                except Exception as exc:  # pragma: no cover - live-site resilience
                    _log(log, f"{target.authority}: failed to fetch {stub.reference or stub.uid}: {exc}")
                    continue

                if not application_matches(application, config.start_date, config.end_date, config.keywords):
                    continue

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
        councils_total=len(targets),
        councils_completed=completed,
        leads_found=len(rows),
    )


def build_scraper(target: CouncilTarget):
    family = target.portal_family.lower()
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
    if target.portal_family == "idox":
        return scraper.discover_ids(
            listing_url=target.listing_url,
            start_date=start_date,
            end_date=end_date,
        ).applications
    if not target.listing_url:
        raise ValueError("listing_url is required for non-Idox councils")
    return scraper.discover_ids(listing_url=target.listing_url).applications


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
