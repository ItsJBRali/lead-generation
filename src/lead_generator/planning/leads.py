from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date
from http.cookiejar import CookieJar
from importlib import resources
from pathlib import Path
from time import sleep
from typing import Callable, Iterable
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener, urlopen

from lxml import html

from lead_generator.planning.models import PlanningApplication, PlanningDocument


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


LogCallback = Callable[[str], None]
ProgressCallback = Callable[[int, int], None]
CancelCallback = Callable[[], bool]


@dataclass(frozen=True, slots=True)
class CouncilTarget:
    authority: str
    portal_family: str
    base_url: str
    listing_url: str | None
    geometry: dict[str, object]
    link_test_ok: bool = False


@dataclass(frozen=True, slots=True)
class LeadSearchConfig:
    geojson_path: Path
    output_root: Path
    start_date: date
    end_date: date
    keywords: list[str]
    catalogue_path: Path | None = None


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


def run_lead_search(
    config: LeadSearchConfig,
    *,
    log: LogCallback | None = None,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCallback | None = None,
) -> LeadSearchResult:
    user_geojson = load_geojson(config.geojson_path)
    catalogue = load_authority_catalogue(config.catalogue_path)
    targets = select_overlapping_authorities(user_geojson, catalogue)
    output_dir = config.output_root / date.today().isoformat()
    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / "applications.csv"
    selected_path = output_dir / "selected_councils.geojson"
    selected_path.write_text(councils_to_geojson(targets), encoding="utf-8")

    rows: list[dict[str, str]] = []
    completed = 0
    feature_count = len(user_geojson.get("features", []))
    _log(log, f"Read {feature_count} user GeoJSON features from {config.geojson_path.name}")
    _log(log, f"Selected {len(targets)} overlapping planning authorities")
    _log(log, f"Saved selected council catalogue to {selected_path}")
    if not targets:
        raise ValueError("No planning authorities overlap the supplied GeoJSON boundary.")
    _progress(progress, completed, len(targets))

    for target in targets:
        if should_cancel and should_cancel():
            _log(log, "Run cancelled.")
            break

        _log(log, f"Searching {target.authority} ({target.portal_family})")
        try:
            applications = discover_planit_applications(target.authority, config.start_date, config.end_date)
            _log(log, f"{target.authority}: found {len(applications)} received applications in the date range")
            matched_count = 0
            for application in applications:
                if should_cancel and should_cancel():
                    break
                if not application_matches(application, config.start_date, config.end_date, config.keywords):
                    continue
                if not application_in_geojson(application, user_geojson):
                    continue
                application = enrich_planit_application(application)
                matched_count += 1
                lead_folder = create_lead_folder(output_dir, target.authority, application)
                download_pdf_documents(application.documents, lead_folder, log=log)
                rows.append(
                    {
                        "Reference": application.reference or application.uid,
                        "application link": application_link(application),
                        "proposal": application.description or "",
                        "date received": application.date_received or "",
                        "council": target.authority,
                    }
                )
                _log(log, f"{target.authority}: saved {application.reference or application.uid}")
            _log(log, f"{target.authority}: {matched_count} applications matched keywords and location")
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


def discover_planit_applications(authority: str, start_date: date, end_date: date) -> list[PlanningApplication]:
    records: list[dict[str, object]] = []
    page_size = 100
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
    docs_url = application.raw.get("docs_url") if application.raw else None
    if docs_url:
        application.documents = fetch_planit_documents(str(docs_url))
    return application


def fetch_planit_documents(docs_url: str) -> list[PlanningDocument]:
    request = Request(docs_url, headers={"User-Agent": "Mozilla/5.0 LeadGeneratorPlanningScraper/0.1"})
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
        documents.append(PlanningDocument(title=title, url=normalize_url(absolute_url), source_url=page_url))
    return documents


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
        for value in (application.reference, application.address, application.description, raw_text)
        if value
    ).casefold()
    return any(keyword.casefold() in haystack for keyword in keywords)


def application_in_geojson(application: PlanningApplication, user_geojson: dict[str, object]) -> bool:
    point = application_point(application)
    if not point:
        return False
    return any(point_in_geometry(point, geometry) for geometry in iter_feature_geometries(user_geojson))


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
        if not _looks_like_pdf(document):
            continue
        filename = sanitize_path_part(document.title or f"document-{index}.pdf")
        if not filename.lower().endswith(".pdf"):
            filename = f"{filename}.pdf"
        path = _unique_path(destination / filename)
        try:
            payload = download_document_bytes(document)
            path.write_bytes(payload)
            downloaded += 1
        except Exception as exc:  # pragma: no cover - network resilience
            _log(log, f"Could not download {document.title}: {exc}")
    return downloaded


def write_csv(csv_path: Path, rows: list[dict[str, str]]) -> None:
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["Reference", "application link", "proposal", "date received", "council"],
        )
        writer.writeheader()
        writer.writerows(rows)


def application_link(application: PlanningApplication) -> str:
    if application.raw:
        for key in ("source_url", "portal_url", "docs_url"):
            value = application.raw.get(key)
            if value:
                return str(value)
    if application.source_url:
        return application.source_url
    if application.url:
        return application.url
    return ""


def download_document_bytes(document: PlanningDocument) -> bytes:
    last_error: Exception | None = None
    opener = build_opener(HTTPCookieProcessor(CookieJar()))
    for url in document_download_candidates(document.url):
        headers = {"User-Agent": "Mozilla/5.0 LeadGeneratorPlanningScraper/0.1"}
        if document.source_url:
            headers["Referer"] = document.source_url
        try:
            request = Request(url, headers=headers)
            with opener.open(request, timeout=30) as response:
                payload = response.read()
                content_type = response.headers.get("Content-Type", "").lower()
                if "html" in content_type and not payload.lstrip().startswith(b"%PDF"):
                    raise ValueError(f"{url} returned HTML instead of a PDF")
                return payload
        except HTTPError as exc:
            last_error = exc
            if exc.code != 404:
                raise
        except Exception as exc:
            last_error = exc
    raise last_error or RuntimeError(f"Could not download {document.url}")


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
            start = lowered.index(old)
            candidate = normalized[:start] + new + normalized[start + len(old) :]
            candidates.append(candidate)
    return list(dict.fromkeys(candidates))


def normalize_url(url: str) -> str:
    parts = urlsplit(url)
    path = quote(parts.path, safe="/%")
    query = quote(parts.query, safe="=&?/%:+,;@")
    return urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


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


def _fetch_json_with_retry(url: str) -> dict[str, object]:
    for attempt in range(4):
        request = Request(url, headers={"User-Agent": "LeadGeneratorPlanningScraper/0.1"})
        try:
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            sleep(2 * (attempt + 1))
    raise RuntimeError(f"Could not fetch public planning metadata: {url}")


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
