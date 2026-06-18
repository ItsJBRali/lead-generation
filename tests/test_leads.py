from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from urllib.error import HTTPError
from pathlib import Path
from unittest.mock import patch

from lead_generator.planning.leads import (
    LeadSearchConfig,
    _fetch_json_with_retry,
    application_in_geojson,
    application_matches,
    document_download_candidates,
    download_document_bytes,
    download_pdf_documents,
    load_authority_catalogue,
    parse_keywords,
    point_in_geometry,
    run_lead_search,
    sanitize_path_part,
    select_overlapping_authorities,
)
from lead_generator.planning.models import PlanningApplication, PlanningDocument


def polygon_feature(name: str, xmin: float, ymin: float, xmax: float, ymax: float) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]],
        },
    }


class LeadSearchTest(unittest.TestCase):
    def test_parse_keywords_deduplicates_and_strips_quotes(self) -> None:
        self.assertEqual(
            parse_keywords(' "gates" \n"electric gates"\ngates\n'),
            ["gates", "electric gates"],
        )

    def test_select_overlapping_authorities_uses_app_catalogue_not_user_properties(self) -> None:
        user_geojson = {
            "type": "FeatureCollection",
            "features": [polygon_feature("search area", 0.1, 52.4, 0.2, 52.5)],
        }
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))

        targets = select_overlapping_authorities(user_geojson, catalogue)

        target_by_authority = {target.authority: target for target in targets}
        self.assertIn("Fenland", target_by_authority)
        self.assertNotIn("search area", target_by_authority)
        self.assertEqual(
            target_by_authority["Fenland"].listing_url,
            "https://www.publicaccess.fenland.gov.uk/publicaccess/search.do?action=advanced",
        )

    def test_builtin_catalogue_entries_have_council_names_and_portal_urls(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))

        for feature in catalogue["features"]:
            properties = feature["properties"]
            self.assertTrue(properties["authority"])
            self.assertTrue(properties["council_name"])
            self.assertTrue(properties["listing_url"])

    def test_builtin_catalogue_includes_known_english_gap_authorities(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        authorities = {feature["properties"]["authority"] for feature in catalogue["features"]}

        self.assertTrue(
            {
                "East Suffolk",
                "BCP",
                "North Northamptonshire",
                "West Northamptonshire",
                "Westmorland and Furness",
                "Cumberland",
                "Adur and Worthing",
                "Mid Kent",
                "South West Devon",
                "Babergh Mid Suffolk",
                "Bromsgrove Redditch",
                "Chiltern South Bucks",
                "South Norfolk Broadland",
                "Greater Cambridge",
            }.issubset(authorities)
        )

    def test_point_in_geometry_handles_polygon(self) -> None:
        geometry = polygon_feature("area", 0, 0, 1, 1)["geometry"]

        self.assertTrue(point_in_geometry((0.5, 0.5), geometry))
        self.assertFalse(point_in_geometry((2, 2), geometry))

    def test_application_matches_date_and_keyword(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            description="Installation of gates",
            date_received="2026-06-12",
        )

        self.assertTrue(
            application_matches(
                application,
                date(2026, 6, 1),
                date(2026, 6, 30),
                ["electric gates", "installation of gates"],
            )
        )
        self.assertFalse(
            application_matches(
                application,
                date(2026, 7, 1),
                date(2026, 7, 31),
                ["installation of gates"],
            )
        )

    def test_application_in_geojson_requires_point_inside_user_boundary(self) -> None:
        user_geojson = {
            "type": "FeatureCollection",
            "features": [polygon_feature("search area", 0, 0, 1, 1)],
        }
        inside = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
        )
        outside = PlanningApplication(
            authority="Example",
            uid="2",
            url="https://example.test",
            raw={"location": {"type": "Point", "coordinates": [2, 2]}},
        )

        self.assertTrue(application_in_geojson(inside, user_geojson))
        self.assertFalse(application_in_geojson(outside, user_geojson))

    def test_run_lead_search_writes_only_location_matched_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson = root / "search.geojson"
            user_geojson.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [polygon_feature("search area", 0, 0, 1, 1)],
                    }
                ),
                encoding="utf-8",
            )
            catalogue = root / "catalogue.geojson"
            catalogue.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                **polygon_feature("Example Council", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Example Council",
                                    "portal_family": "idox",
                                    "base_url": "https://planning.example.gov.uk",
                                    "listing_url": "https://planning.example.gov.uk/search",
                                    "link_test_ok": True,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["driveway gates"],
                catalogue_path=catalogue,
            )
            applications = [
                PlanningApplication(
                    authority="Example Council",
                    uid="ABC123",
                    url="https://planning.example.gov.uk/detail/ABC123",
                    reference="24/01234/FUL",
                    description="New driveway gates and boundary wall",
                    date_received="2026-06-10",
                    raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
                ),
                PlanningApplication(
                    authority="Example Council",
                    uid="DEF456",
                    url="https://planning.example.gov.uk/detail/DEF456",
                    reference="24/99999/FUL",
                    description="New driveway gates",
                    date_received="2026-06-10",
                    raw={"location": {"type": "Point", "coordinates": [2, 2]}},
                ),
            ]

            with patch("lead_generator.planning.leads.discover_planit_applications", return_value=applications):
                result = run_lead_search(config)

            self.assertEqual(result.leads_found, 1)
            self.assertTrue(result.csv_path.exists())
            csv_text = result.csv_path.read_text(encoding="utf-8")
            self.assertIn("application link", csv_text)
            self.assertIn("https://planning.example.gov.uk/detail/ABC123", csv_text)
            self.assertIn("24/01234/FUL", csv_text)
            self.assertNotIn("24/99999/FUL", csv_text)
            self.assertTrue((result.output_dir / "Example Council" / "24 01234 FUL").exists())
            self.assertTrue((result.output_dir / "selected_councils.geojson").exists())

    def test_document_download_retries_viewer_url_as_download_url(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if "documentviewer.do" in request.full_url:
                    raise HTTPError(request.full_url, 404, "Not Found", {}, None)
                return FakeResponse()

        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/online-applications/documentviewer.do?keyVal=DOC001",
            source_url="https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=documents",
        )

        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener()
            with (
                patch("lead_generator.planning.leads.build_opener", return_value=opener),
                patch("lead_generator.planning.leads.sleep"),
            ):
                downloaded = download_pdf_documents([document], Path(directory))

            self.assertEqual(downloaded, 1)
            self.assertTrue((Path(directory) / "Proposed plan.pdf").exists())
            self.assertEqual(opener.urls[0], document.url)
            self.assertIn("documentdownload.do", opener.urls[1])

    def test_document_download_candidates_add_idox_module_download_url(self) -> None:
        candidates = document_download_candidates(
            "https://planning.example.gov.uk/online-applications/documentviewer.do?keyVal=DOC001"
        )

        self.assertIn(
            "https://planning.example.gov.uk/online-applications/documentdownload.do?module=planning&keyVal=DOC001",
            candidates,
        )

    def test_fetch_json_waits_and_retries_after_rate_limit(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b'{"records": []}'

        calls = 0

        def fake_urlopen(request, timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "3"}, None)
            return FakeResponse()

        with (
            patch("lead_generator.planning.leads.urlopen", side_effect=fake_urlopen),
            patch("lead_generator.planning.leads.sleep") as sleep_mock,
        ):
            payload = _fetch_json_with_retry("https://www.planit.org.uk/api/applics/json")

        self.assertEqual(payload, {"records": []})
        sleep_mock.assert_called_once_with(3.0)

    def test_document_download_waits_and_retries_after_rate_limit(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)
                return FakeResponse()

        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/online-applications/documentdownload.do?module=planning&keyVal=DOC001",
        )

        opener = FakeOpener()
        with (
            patch("lead_generator.planning.leads.build_opener", return_value=opener),
            patch("lead_generator.planning.leads.sleep") as sleep_mock,
        ):
            payload = download_document_bytes(document)

        self.assertEqual(payload, b"%PDF-1.4")
        self.assertEqual(opener.calls, 2)
        sleep_mock.assert_called_once_with(2.0)

    def test_sanitize_path_part_removes_windows_invalid_characters(self) -> None:
        self.assertEqual(sanitize_path_part("24/01234:FUL*"), "24 01234 FUL")


if __name__ == "__main__":
    unittest.main()
