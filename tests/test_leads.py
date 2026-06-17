from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from lead_generator.planning.leads import (
    LeadSearchConfig,
    application_matches,
    discover_planit_applications,
    load_council_targets,
    parse_keywords,
    run_lead_search,
    sanitize_path_part,
)
from lead_generator.planning.models import DiscoveryResult, PlanningApplication


class FakeScraper:
    def discover_ids(self, **_: object) -> DiscoveryResult:
        return DiscoveryResult(
            authority="Example Council",
            source_url="https://planning.example.gov.uk/search",
            applications=[
                PlanningApplication(
                    authority="Example Council",
                    uid="ABC123",
                    url="https://planning.example.gov.uk/detail/ABC123",
                    reference="24/01234/FUL",
                )
            ],
        )

    def fetch_application(
        self,
        uid: str,
        url: str | None = None,
        *,
        include_documents: bool = False,
    ) -> PlanningApplication:
        return PlanningApplication(
            authority="Example Council",
            uid=uid,
            url=url or "",
            reference="24/01234/FUL",
            description="New driveway gates and boundary wall",
            date_received="2026-06-10",
        )


class LeadSearchTest(unittest.TestCase):
    def test_parse_keywords_deduplicates_and_strips_quotes(self) -> None:
        self.assertEqual(
            parse_keywords(' "gates" \n“electric gates”\ngates\n'),
            ["gates", "electric gates"],
        )

    def test_load_council_targets_from_geojson_properties(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "councils.geojson"
            path.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {
                                    "name": "Example Council",
                                    "portal_family": "idox",
                                    "base_url": "https://planning.example.gov.uk",
                                },
                                "geometry": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            targets = load_council_targets(path)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].authority, "Example Council")
        self.assertEqual(targets[0].portal_family, "idox")

    def test_load_council_targets_uses_public_metadata_fallback_for_name_only_geojson(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "councils.geojson"
            path.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {"LAD23NM": "Arun"},
                                "geometry": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            targets = load_council_targets(path)

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].authority, "Arun")
        self.assertEqual(targets[0].portal_family, "planit")

    @patch("lead_generator.planning.leads.urlopen")
    def test_discover_planit_applications_maps_public_metadata(self, urlopen_mock) -> None:
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "from": 0,
                        "to": 0,
                        "total": 1,
                        "records": [
                            {
                                "uid": "BR/111/24/PL",
                                "url": "https://planning.example.gov.uk/detail",
                                "address": "8 Argyle Road PO21 1DY",
                                "description": "New driveway gates",
                                "start_date": "2024-06-21",
                                "postcode": "PO21 1DY",
                                "other_fields": {
                                    "date_received": "2024-06-21",
                                    "date_validated": "2024-07-10",
                                    "docs_url": "https://planning.example.gov.uk/docs",
                                    "source_url": "https://planning.example.gov.uk/search",
                                },
                            }
                        ],
                    }
                ).encode("utf-8")

        urlopen_mock.return_value = Response()

        applications = discover_planit_applications("Arun", date(2024, 6, 1), date(2024, 6, 30))

        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].reference, "BR/111/24/PL")
        self.assertEqual(applications[0].description, "New driveway gates")
        self.assertEqual(applications[0].date_received, "2024-06-21")
        self.assertEqual(applications[0].raw["docs_url"], "https://planning.example.gov.uk/docs")

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

    def test_run_lead_search_writes_csv_and_reference_folder(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            geojson = root / "councils.geojson"
            geojson.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                "type": "Feature",
                                "properties": {
                                    "name": "Example Council",
                                    "portal_family": "idox",
                                    "base_url": "https://planning.example.gov.uk",
                                },
                                "geometry": None,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            config = LeadSearchConfig(
                geojson_path=geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["driveway gates"],
            )

            with patch("lead_generator.planning.leads.build_scraper", return_value=FakeScraper()):
                result = run_lead_search(config)

            self.assertEqual(result.leads_found, 1)
            self.assertTrue(result.csv_path.exists())
            self.assertIn("24/01234/FUL", result.csv_path.read_text(encoding="utf-8"))
            self.assertTrue((result.output_dir / "Example Council" / "24 01234 FUL").exists())

    def test_sanitize_path_part_removes_windows_invalid_characters(self) -> None:
        self.assertEqual(sanitize_path_part("24/01234:FUL*"), "24 01234 FUL")


if __name__ == "__main__":
    unittest.main()
