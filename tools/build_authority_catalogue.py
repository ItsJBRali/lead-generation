from __future__ import annotations

import argparse
import json
import ssl
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from time import sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from lead_generator.planning.portals import detect_portal_family

try:
    import certifi
except ImportError:  # pragma: no cover - depends on local tooling
    certifi = None


AREAS_URL = (
    "https://www.planit.org.uk/api/areas/json?"
    "area_type=active&pg_sz=20&"
    "select=area_id,area_name,long_name,area_type,gss_code,planning_url,scraper_type,min_date,max_date,borders"
)
OUTPUT = Path("src/lead_generator/planning/data/planning_authorities.geojson")
CACHE = Path("build/planning_authority_area_records.json")
LINK_CACHE = Path("build/planning_authority_link_tests.json")
USER_AGENT = "LeadGeneratorPlanningScraper/0.1"


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the bundled planning authority catalogue.")
    parser.add_argument(
        "--cached-only",
        action="store_true",
        help="Write a catalogue from cached area records without fetching more PlanIt pages.",
    )
    args = parser.parse_args()

    records = deduplicate_records(fetch_all_areas(cached_only=args.cached_only))
    print(f"Fetched {len(records)} active planning authorities")
    tested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    tests = test_links({record.get("area_name"): record.get("planning_url") for record in records})

    features = []
    for record in records:
        planning_url = record.get("planning_url") or ""
        scraper_type = (record.get("scraper_type") or "").strip().lower()
        portal_family = normalize_family(scraper_type) or detect_portal_family("", planning_url) or "unknown"
        test = tests.get(record.get("area_name"), {})
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "authority": record.get("area_name"),
                    "council_name": record.get("long_name") or record.get("area_name"),
                    "area_id": record.get("area_id"),
                    "area_type": record.get("area_type"),
                    "gss_code": record.get("gss_code"),
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
            indent=2,
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
            if exc.code != 429 or attempt == 5:
                raise
            retry_after = exc.headers.get("Retry-After")
            delay = int(retry_after) if retry_after and retry_after.isdigit() else 10 * (attempt + 1)
            sleep(delay)
    raise RuntimeError(f"Could not fetch {url}")


def fetch_all_areas(*, cached_only: bool = False) -> list[dict[str, object]]:
    records, total = read_records_cache()
    offset = len(records)
    if cached_only:
        print(f"Using cached area records 0 to {offset - 1} of {total or offset}")
        return records
    if total is not None and offset >= total:
        print(f"Using cached area records 0 to {offset - 1} of {total}")
        return records
    while True:
        payload = fetch_json(f"{AREAS_URL}&index={offset}")
        batch = payload.get("records", [])
        records.extend(batch)
        print(f"Fetched area records {payload.get('from')} to {payload.get('to')} of {payload.get('total')}")
        total = int(payload.get("total") or len(records))
        write_records_cache(records, total)
        next_offset = int(payload.get("to") or offset) + 1
        if not batch or next_offset >= total:
            break
        offset = next_offset
        sleep(2)
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
        "ocella": "ocella",
        "agile": "agile",
        "northgate": "northgate",
        "civica": "civica",
    }
    for key, value in mapping.items():
        if key in scraper_type:
            return value
    return None


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
    lowered = url.lower()
    if family == "idox" and "/online-applications/" in lowered:
        return url[: lowered.index("/online-applications/")]
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
        return [], None
    return records, int(total) if total is not None else None


def write_records_cache(records: list[dict[str, object]], total: int) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps({"total": total, "records": records}, indent=2), encoding="utf-8")


def read_link_cache() -> dict[str | None, dict[str, object]]:
    if not LINK_CACHE.exists():
        return {}
    payload = json.loads(LINK_CACHE.read_text(encoding="utf-8"))
    return payload if isinstance(payload, dict) else {}


def write_link_cache(results: dict[str | None, dict[str, object]]) -> None:
    LINK_CACHE.parent.mkdir(parents=True, exist_ok=True)
    LINK_CACHE.write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
