from __future__ import annotations

import csv
import json
import re
import ssl
import threading
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, timezone
from email.utils import parsedate_to_datetime
from http.cookiejar import CookieJar
from email.message import Message
from importlib import resources
from pathlib import Path
from time import sleep
from typing import Callable, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPSHandler, HTTPCookieProcessor, Request, build_opener, urlopen

from lxml import html

from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.parsing import clean_text


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

USER_AGENT = "Mozilla/5.0 LeadGeneratorPlanningScraper/0.1 (+responsible planning data collection)"
PLANIT_PAGE_SIZE = 100
SEARCH_WORKER_COUNT = 2
DOCUMENT_DOWNLOAD_DELAY_SECONDS = 0.0
RATE_LIMIT_HTTP_CODES = {429, 503}
MAX_RETRY_AFTER_SECONDS = 60.0


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


@dataclass(slots=True)
class DownloadedFile:
    payload: bytes
    final_url: str
    content_type: str
    filename: str | None = None


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

    pending_targets = deque(targets)
    lock = threading.Lock()

    def next_target(reverse: bool) -> CouncilTarget | None:
        with lock:
            if not pending_targets:
                return None
            return pending_targets.pop() if reverse else pending_targets.popleft()

    def mark_complete() -> None:
        nonlocal completed
        with lock:
            completed += 1
            current = completed
        _progress(progress, current, len(targets))

    def save_row(row: dict[str, str]) -> None:
        with lock:
            rows.append(row)

    def search_worker(name: str, *, reverse: bool = False) -> None:
        while True:
            if should_cancel and should_cancel():
                _log(log, f"{name}: cancelled.")
                return
            target = next_target(reverse)
            if target is None:
                return

            _log(log, f"{name}: searching {target.authority} ({target.portal_family})")
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
                    save_row(
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
                mark_complete()

    worker_count = min(SEARCH_WORKER_COUNT, len(targets))
    workers = [
        threading.Thread(target=search_worker, args=("Forward search",), daemon=True),
    ]
    if worker_count > 1:
        workers.append(threading.Thread(target=search_worker, args=("Reverse search",), kwargs={"reverse": True}, daemon=True))
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

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
    docs_url = application.raw.get("docs_url") if application.raw else None
    if docs_url:
        application.documents = fetch_planit_documents(str(docs_url))
    return application


def fetch_planit_documents(docs_url: str) -> list[PlanningDocument]:
    opener = _build_document_opener()
    text, page_url = _fetch_html_with_portal_session(docs_url, opener, timeout=45)
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
        if not _looks_like_downloadable_document(document):
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


def download_document_file(document: PlanningDocument) -> DownloadedFile:
    return _download_document_file(document)


def download_document_bytes(document: PlanningDocument) -> bytes:
    return download_document_file(document).payload


def _download_document_file(document: PlanningDocument) -> DownloadedFile:
    last_error: Exception | None = None
    opener = _build_document_opener()
    verify_tls = True
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
            except Exception as exc:
                last_error = exc
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
    return list(dict.fromkeys(candidates))


def iter_document_links(document: html.HtmlElement, page_url: str) -> Iterable[tuple[str, str]]:
    yield from iter_public_access_model_links(document, page_url)
    for anchor in document.xpath("//a[@href]"):
        href = anchor.get("href")
        absolute_url = urljoin(page_url, href)
        title = clean_text(" ".join(anchor.itertext())) or document_title_from_url(absolute_url)
        if not _is_document_href(href) and not _is_document_link_text(title, href):
            continue
        if not _is_document_href(href) and _is_application_tab_href(href):
            continue
        yield href, title
    for element in document.xpath("//*[@onclick]"):
        onclick = element.get("onclick") or ""
        for href in re.findall(r"['\"]([^'\"]+(?:document|download|attachment|viewDocument|showDocuments)[^'\"]*)['\"]", onclick, flags=re.IGNORECASE):
            absolute_url = urljoin(page_url, href)
            title = clean_text(" ".join(element.itertext())) or document_title_from_url(absolute_url)
            yield href, title
    for element in document.xpath("//iframe[@src] | //embed[@src] | //object[@data]"):
        href = element.get("src") or element.get("data")
        if not _is_document_href(href):
            continue
        absolute_url = urljoin(page_url, href)
        yield href, document_title_from_url(absolute_url)


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
    return list(dict.fromkeys(normalize_url(link) for link in links))


def _is_document_href(href: str | None) -> bool:
    if not href:
        return False
    lowered = href.strip().lower()
    if lowered.startswith(("javascript:", "mailto:", "tel:", "#")):
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
            "wphappdocs",
            "wchdisplaymedia",
            "displaymedia",
            "showimage",
            ".pdf",
            ".doc",
            ".docx",
            ".xls",
            ".xlsx",
            ".jpg",
            ".jpeg",
            ".png",
            ".tif",
            ".tiff",
            ".rtf",
        )
    )


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
            if opener is not None:
                return opener.open(request, timeout=timeout)
            return urlopen(request, timeout=timeout)
        except HTTPError as exc:
            if exc.code not in RATE_LIMIT_HTTP_CODES or attempt == 3:
                raise
            sleep(_retry_delay_seconds(exc, 5.0 * (attempt + 1)))
    raise RuntimeError(f"Could not fetch {request.full_url}")


def _build_document_opener(*, verify_tls: bool = True):
    handlers = [HTTPCookieProcessor(CookieJar())]
    if not verify_tls:
        handlers.append(HTTPSHandler(context=ssl._create_unverified_context()))
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
    for attempt in range(4):
        request = Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urlopen(request, timeout=45) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            if exc.code not in RATE_LIMIT_HTTP_CODES or attempt == 3:
                raise
            sleep(_retry_delay_seconds(exc, 5.0 * (attempt + 1)))
    raise RuntimeError(f"Could not fetch public planning metadata: {url}")


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
    text = f"{document.title} {urlsplit(document.url).path}".lower()
    return (
        bool(document.url)
        and (
            _is_document_href(document.url)
            or any(extension in text for extension in (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".rtf"))
            or (document.document_type or "").casefold() in {"pdf", "document", "drawing", "plan", "supporting_document"}
        )
    )


def _is_tls_certificate_error(exc: Exception) -> bool:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return True
    if isinstance(exc, URLError):
        reason = exc.reason
        return isinstance(reason, ssl.SSLCertVerificationError) or "certificate" in str(reason).casefold()
    return "certificate" in str(exc).casefold() and "ssl" in str(exc).casefold()


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
    return {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".rtf", ".txt"}


def _extension_from_content_type(content_type: str) -> str | None:
    content_type = content_type.split(";", 1)[0].strip().lower()
    return {
        "application/pdf": ".pdf",
        "application/msword": ".doc",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
        "application/vnd.ms-excel": ".xls",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/tiff": ".tif",
        "application/rtf": ".rtf",
        "text/rtf": ".rtf",
        "text/plain": ".txt",
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
    if _extension_from_signature(payload):
        return True
    if _looks_like_html(payload, content_type):
        return False
    if _extension_from_content_type(content_type):
        return True
    if Path(urlsplit(final_url).path).suffix.lower() in _known_file_extensions():
        return True
    content_type = content_type.split(";", 1)[0].strip().lower()
    return content_type.startswith("application/") or content_type.startswith("image/")


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
