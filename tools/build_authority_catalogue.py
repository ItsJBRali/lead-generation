from __future__ import annotations

import argparse
import json
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from lead_generator.planning.portals import detect_portal_family

try:
    import certifi
except ImportError:  # pragma: no cover - depends on local tooling
    certifi = None


AREA_SELECT = "area_id,area_name,long_name,area_type,gss_code,planning_url,scraper_type,min_date,max_date,borders"
AREAS_URL = f"https://www.planit.org.uk/api/areas/json?area_type=active&select={AREA_SELECT}"
DEFAULT_PAGE_SIZE = 20
PAGE_DELAY_SECONDS = 20
OUTPUT = Path("src/lead_generator/planning/data/planning_authorities.geojson")
CACHE = Path("build/planning_authority_area_records.json")
LINK_CACHE = Path("build/planning_authority_link_tests.json")
USER_AGENT = "Mozilla/5.0 LeadGeneratorPlanningScraper/0.1 (+responsible planning data collection)"
EXCLUDED_AREA_TYPES = {"Crown Dependency", "Northern Ireland District", "Other Planning Entity"}
CURRENT_GSS_CODE_OVERRIDES = {
    "Fife": "S12000047",
    "Gateshead": "E08000037",
    "Glasgow": "S12000049",
    "North Lanarkshire": "S12000050",
    "Perth": "S12000048",
}
SHARED_COUNCIL_COVERAGE = {
    "Adur and Worthing": (
        ("Adur", "E07000223"),
        ("Worthing", "E07000229"),
    ),
    "Babergh Mid Suffolk": (
        ("Babergh", "E07000200"),
        ("Mid Suffolk", "E07000203"),
    ),
    "Bromsgrove Redditch": (
        ("Bromsgrove", "E07000234"),
        ("Redditch", "E07000236"),
    ),
    "Mid Kent": (
        ("Maidstone", "E07000110"),
        ("Swale", "E07000113"),
    ),
    "South Norfolk Broadland": (
        ("Broadland", "E07000144"),
        ("South Norfolk", "E07000149"),
    ),
    "South West Devon": (
        ("South Hams", "E07000044"),
        ("West Devon", "E07000047"),
    ),
    "Allerdale": (("Cumberland", "E06000063"),),
    "Carlisle": (("Cumberland", "E06000063"),),
    "Copeland": (("Cumberland", "E06000063"),),
}


class ResponseTooLarge(RuntimeError):
    """Raised when PlanIt refuses a page because the boundary payload is too large."""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the bundled planning authority catalogue.")
    parser.add_argument(
        "--cached-only",
        action="store_true",
        help="Write a catalogue from cached area records without fetching more PlanIt pages.",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch every current PlanIt area instead of extending an older catalogue or cache.",
    )
    parser.add_argument(
        "--page-delay",
        type=float,
        default=PAGE_DELAY_SECONDS,
        help="Seconds to wait between PlanIt area pages (default: 20).",
    )
    args = parser.parse_args()

    records = [
        record
        for record in deduplicate_records(
            fetch_all_areas(
                cached_only=args.cached_only,
                refresh=args.refresh,
                page_delay_seconds=max(args.page_delay, 0.0),
            )
        )
        if include_record(record)
    ]
    print(f"Fetched {len(records)} active planning authorities")
    tested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tests = test_links({record.get("area_name"): record.get("planning_url") for record in records})

    features = []
    for record in records:
        planning_url = record.get("planning_url") or ""
        scraper_type = (record.get("scraper_type") or "").strip().lower()
        portal_family = normalize_family(scraper_type) or detect_portal_family("", planning_url) or "unknown"
        test = tests.get(record.get("area_name"), {})
        authority = str(record.get("area_name") or "")
        coverage = SHARED_COUNCIL_COVERAGE.get(authority, ())
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "authority": authority,
                    "council_name": record.get("long_name") or record.get("area_name"),
                    "area_id": record.get("area_id"),
                    "area_type": record.get("area_type"),
                    "gss_code": CURRENT_GSS_CODE_OVERRIDES.get(authority, record.get("gss_code")),
                    **({"covered_councils": [name for name, _code in coverage]} if coverage else {}),
                    **({"covered_gss_codes": [code for _name, code in coverage]} if coverage else {}),
                    "portal_family": portal_family,
                    "scraper_type": record.get("scraper_type"),
                    "base_url": derive_base_url(planning_url, portal_family),
                    "listing_url": planning_url,
                    "planning_url": planning_url,
                    "source": "planit-active-areas",
                    "min_date": record.get("min_date"),
                    "max_date": record.get("max_date"),
                    "link_tested_at": tested_at,
                    "link_test_ok": test.get("ok", False),
                    "link_test_status": test.get("status"),
                    "link_test_final_url": test.get("final_url"),
                    "link_test_tls_verified": test.get("tls_verified", False),
                },
                "geometry": record.get("borders"),
            }
        )

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "type": "FeatureCollection",
                "name": "planning_authorities_with_tested_portals",
                "generated_at": tested_at,
                "source_url": AREAS_URL,
                "features": features,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    print(f"Wrote {len(features)} authorities to {OUTPUT}")


def fetch_json(url: str) -> dict[str, object]:
    for attempt in range(6):
        try:
            with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=60, context=ssl_context()) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            if exc.code == 400 and "Response content too large" in body:
                raise ResponseTooLarge(body) from exc
            if exc.code != 429 or attempt == 5:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1)
            print(f"Rate limited; waiting {delay}s before retrying")
            sleep(delay)
        except URLError as exc:
            if "CERTIFICATE_VERIFY_FAILED" not in str(exc.reason):
                raise
            try:
                with urlopen(
                    Request(url, headers={"User-Agent": USER_AGENT}),
                    timeout=60,
                    context=ssl._create_unverified_context(),
                ) as response:
                    return json.loads(response.read().decode("utf-8", errors="replace"))
            except HTTPError as unverified_exc:
                body = unverified_exc.read().decode("utf-8", errors="replace")
                if unverified_exc.code == 400 and "Response content too large" in body:
                    raise ResponseTooLarge(body) from unverified_exc
                if unverified_exc.code == 429 and attempt < 5:
                    retry_after = unverified_exc.headers.get("Retry-After")
                    delay = int(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1)
                    print(f"Rate limited; waiting {delay}s before retrying")
                    sleep(delay)
                    continue
                raise
    raise RuntimeError(f"Could not fetch {url}")


def fetch_all_areas(
    *,
    cached_only: bool = False,
    refresh: bool = False,
    page_delay_seconds: float = PAGE_DELAY_SECONDS,
) -> list[dict[str, object]]:
    records, total = ([], None) if refresh else read_records_cache()
    offset = len(records)
    page_size = DEFAULT_PAGE_SIZE
    if cached_only:
        print(f"Using cached area records 0 to {offset - 1} of {total or offset}")
        return records
    if total is not None and offset >= total:
        print(f"Using cached area records 0 to {offset - 1} of {total}")
        return records
    while True:
        try:
            payload = fetch_json(f"{AREAS_URL}&pg_sz={page_size}&index={offset}")
        except ResponseTooLarge as exc:
            if page_size == 1:
                raise
            page_size = max(1, page_size // 2)
            print(f"Area page at index {offset} is too large; retrying with pg_sz={page_size}: {exc}")
            continue
        batch = payload.get("records", [])
        records.extend(batch)
        print(f"Fetched area records {payload.get('from')} to {payload.get('to')} of {payload.get('total')}")
        total = int(payload.get("total") or len(records))
        write_records_cache(records, total)
        next_offset = int(payload.get("to") or offset) + 1
        if not batch or next_offset >= total:
            break
        offset = next_offset
        sleep(page_delay_seconds)
    return records


def test_links(urls: dict[str | None, str | None]) -> dict[str | None, dict[str, object]]:
    results = read_link_cache()
    completed = 0
    pending = {
        name: url
        for name, url in urls.items()
        if not results.get(name, {}).get("ok") or "tls_verified" not in results.get(name, {})
    }
    with ThreadPoolExecutor(max_workers=12) as executor:
        futures = {executor.submit(test_link, url): name for name, url in pending.items()}
        for future in as_completed(futures):
            name = futures[future]
            results[name] = future.result()
            completed += 1
            if completed % 25 == 0:
                print(f"Tested {completed} portal links")
                write_link_cache(results)
    write_link_cache(results)
    return results


def test_link(url: str | None) -> dict[str, object]:
    if not url:
        return {"ok": False, "status": "missing"}
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=8, context=ssl_context()) as response:
            response.read(1024)
            return {
                "ok": 200 <= getattr(response, "status", 200) < 400,
                "status": getattr(response, "status", 200),
                "final_url": response.geturl(),
                "tls_verified": True,
            }
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "final_url": url, "tls_verified": True}
    except URLError as exc:
        if "CERTIFICATE_VERIFY_FAILED" in str(exc.reason):
            return test_link_unverified(url)
        return {"ok": False, "status": str(exc.reason), "final_url": url, "tls_verified": True}
    except Exception as exc:
        return {"ok": False, "status": type(exc).__name__, "final_url": url, "tls_verified": True}


def test_link_unverified(url: str) -> dict[str, object]:
    try:
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with urlopen(request, timeout=8, context=ssl._create_unverified_context()) as response:
            response.read(1024)
            return {
                "ok": 200 <= getattr(response, "status", 200) < 400,
                "status": getattr(response, "status", 200),
                "final_url": response.geturl(),
                "tls_verified": False,
            }
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "final_url": url, "tls_verified": False}
    except URLError as exc:
        return {"ok": False, "status": str(exc.reason), "final_url": url, "tls_verified": False}
    except Exception as exc:
        return {"ok": False, "status": type(exc).__name__, "final_url": url, "tls_verified": False}


def normalize_family(scraper_type: str) -> str | None:
    mapping = {
        "idox": "idox",
        "arcus": "arcus",
        "ocella": "ocella",
        "agile": "agile",
        "achieveforms": "achieveforms",
        "atrium": "atrium",
        "tascomi": "tascomi",
        "appsearchserv": "appsearchserv",
        "planningexplorer": "northgate",
        "northgate": "northgate",
        "civica": "civica",
    }
    for key, value in mapping.items():
        if key in scraper_type:
            return value
    return None


def include_record(record: dict[str, object]) -> bool:
    area_type = str(record.get("area_type") or "")
    gss_code = str(record.get("gss_code") or "")
    return area_type not in EXCLUDED_AREA_TYPES and not gss_code.startswith("N")


def deduplicate_records(records: list[dict[str, object]]) -> list[dict[str, object]]:
    deduplicated: list[dict[str, object]] = []
    seen: set[tuple[object, object, object]] = set()
    for record in records:
        key = (record.get("area_id"), record.get("area_name"), record.get("gss_code"))
        if key in seen:
            continue
        seen.add(key)
        deduplicated.append(record)
    return deduplicated


def derive_base_url(url: str, family: str) -> str:
    if not url:
        return ""
    if family == "idox":
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.netloc}" if parts.scheme and parts.netloc else url
    lowered = url.lower()
    if "/search" in lowered:
        return url[: lowered.rfind("/search")]
    return url.rsplit("/", 1)[0] + "/"


def ssl_context() -> ssl.SSLContext | None:
    if certifi is None:
        return None
    return ssl.create_default_context(cafile=certifi.where())


def read_records_cache() -> tuple[list[dict[str, object]], int | None]:
    if not CACHE.exists():
        return [], None
    payload = json.loads(CACHE.read_text(encoding="utf-8"))
    records = payload.get("records", [])
    total = payload.get("total")
    if not isinstance(records, list):
        records = []
    return deduplicate_records(records), int(total) if total is not None else None


def write_records_cache(records: list[dict[str, object]], total: int) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"total": total, "records": records}, indent=2), encoding="utf-8")


def read_link_cache() -> dict[str | None, dict[str, object]]:
    results = read_link_tests_from_catalogue()
    if LINK_CACHE.exists():
        payload = json.loads(LINK_CACHE.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            results.update(payload)
    return results


def write_link_cache(results: dict[str | None, dict[str, object]]) -> None:
    LINK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LINK_CACHE.write_text(json.dumps(results, indent=2), encoding="utf-8")


def read_link_tests_from_catalogue() -> dict[str | None, dict[str, object]]:
    if not OUTPUT.exists():
        return {}
    catalogue = json.loads(OUTPUT.read_text(encoding="utf-8"))
    results: dict[str | None, dict[str, object]] = {}
    for feature in catalogue.get("features", []):
        properties = feature.get("properties") or {}
        name = properties.get("authority")
        if not name:
            continue
        results[name] = {
            "ok": properties.get("link_test_ok", False),
            "status": properties.get("link_test_status"),
            "final_url": properties.get("link_test_final_url"),
            "tls_verified": properties.get("link_test_tls_verified", False),
        }
    return results


if __name__ == "__main__":
    main()
