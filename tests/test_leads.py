from __future__ import annotations

import csv
import json
import ssl
import threading
import tempfile
import unittest
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from pathlib import Path
from unittest.mock import patch

from lxml import html

from lead_generator.planning.leads import (
    CouncilTarget,
    DownloadedFile,
    LeadSearchConfig,
    _fetch_json_with_retry,
    application_in_geojson,
    application_matches_search_area,
    application_matches,
    application_link,
    document_source_url_from_application_url,
    document_filename,
    document_download_candidates,
    discover_portal_applications,
    download_document_bytes,
    download_pdf_documents,
    enrich_planit_application,
    fetch_arcus_public_register_file_list,
    fetch_arcus_salesforce_document_list,
    fetch_publisher_document_list,
    iter_document_links,
    load_authority_catalogue,
    planit_document_source_urls,
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

    def test_application_matches_uses_validated_date_when_received_date_missing(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            description="Installation of gates",
            date_validated="2026-06-12",
        )

        self.assertTrue(
            application_matches(
                application,
                date(2026, 6, 1),
                date(2026, 6, 30),
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

    def test_application_matches_search_area_allows_portal_records_without_coordinates(self) -> None:
        user_geojson = {
            "type": "FeatureCollection",
            "features": [polygon_feature("search area", 0, 0, 1, 1)],
        }
        application = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
        )

        self.assertTrue(application_matches_search_area(application, user_geojson))

    def test_discover_portal_applications_falls_back_to_planit_after_portal_error(self) -> None:
        class BrokenScraper:
            def discover_ids(self, **kwargs):
                raise RuntimeError("portal unavailable")

        target = CouncilTarget(
            authority="Hampshire",
            portal_family="unknown",
            scraper_type="Custom",
            base_url="https://maps.hants.gov.uk/MwpMapping/",
            listing_url="https://maps.hants.gov.uk/MwpMapping/",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Hampshire",
                uid="26/01274/DDTPO",
                url="https://example.test/app",
                reference="26/01274/DDTPO",
                date_received="2026-06-10",
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=BrokenScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback) as planit,
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        planit.assert_called_once_with("Hampshire", date(2026, 6, 8), date(2026, 6, 14))
        self.assertEqual(applications[0].authority, "Hampshire")
        self.assertEqual(applications[0].raw["source"], "planit_fallback")
        self.assertIn("portal unavailable", applications[0].raw["portal_fetch_error"])

    def test_discover_portal_applications_uses_planit_alias_before_shared_buckinghamshire_portal(self) -> None:
        target = CouncilTarget(
            authority="Wycombe",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://publicaccess.buckinghamshire.gov.uk",
            listing_url="https://publicaccess.buckinghamshire.gov.uk/online-applications/search.do?action=advanced",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Buckinghamshire",
                uid="PL/26/00001/FA",
                url="https://example.test/app",
                reference="PL/26/00001/FA",
                date_received="2026-06-10",
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target") as scraper_factory,
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback) as planit,
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        scraper_factory.assert_not_called()
        planit.assert_called_once_with("Buckinghamshire", date(2026, 6, 8), date(2026, 6, 14))
        self.assertEqual(applications[0].authority, "Wycombe")
        self.assertEqual(applications[0].raw["source"], "planit_fallback")

    def test_discover_portal_applications_uses_planit_for_known_empty_unreliable_portal(self) -> None:
        class EmptyScraper:
            def discover_ids(self, **kwargs):
                from lead_generator.planning.models import DiscoveryResult

                return DiscoveryResult(authority="Lambeth", source_url="https://planning.lambeth.gov.uk", applications=[])

        target = CouncilTarget(
            authority="Lambeth",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://planning.lambeth.gov.uk",
            listing_url="https://planning.lambeth.gov.uk/online-applications/search.do?action=advanced",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Lambeth",
                uid="26/01723/NMC",
                url="https://example.test/app",
                reference="26/01723/NMC",
                date_received="2026-06-10",
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=EmptyScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback),
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        self.assertEqual([application.reference for application in applications], ["26/01723/NMC"])
        self.assertEqual(applications[0].raw["source"], "planit_fallback")

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
                    address="1 Example Street",
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

            with patch("lead_generator.planning.leads.discover_portal_applications", return_value=applications):
                result = run_lead_search(config)

            self.assertEqual(result.leads_found, 1)
            self.assertTrue(result.csv_path.exists())
            csv_text = result.csv_path.read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("Reference,address,application link"))
            self.assertIn("application link", csv_text)
            self.assertIn("1 Example Street", csv_text)
            self.assertIn("https://planning.example.gov.uk/detail/ABC123", csv_text)
            self.assertIn("24/01234/FUL", csv_text)
            self.assertNotIn("24/99999/FUL", csv_text)
            self.assertTrue(result.failure_csv_path.exists())
            self.assertTrue((result.output_dir / "Example Council" / "24 01234 FUL").exists())
            self.assertTrue((result.output_dir / "selected_councils.geojson").exists())

    def test_run_lead_search_can_write_csv_without_downloading_files(self) -> None:
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
                download_application_files=False,
            )
            application = PlanningApplication(
                authority="Example Council",
                uid="ABC123",
                url="https://planning.example.gov.uk/detail/ABC123",
                reference="24/01234/FUL",
                address="1 Example Street",
                description="New driveway gates and boundary wall",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
            )

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=[application]),
                patch("lead_generator.planning.leads.enrich_application_documents", side_effect=AssertionError("Documents should not be enriched")),
                patch("lead_generator.planning.leads.download_pdf_documents", side_effect=AssertionError("Documents should not be downloaded")),
            ):
                result = run_lead_search(config)

            self.assertEqual(result.leads_found, 1)
            csv_text = result.csv_path.read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("Reference,address,application link"))
            self.assertIn("24/01234/FUL,1 Example Street,https://planning.example.gov.uk/detail/ABC123", csv_text)
            self.assertFalse((result.output_dir / "Example Council").exists())

    def test_run_lead_search_updates_output_csv_when_cancelled(self) -> None:
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
                    description="New driveway gates",
                    date_received="2026-06-10",
                ),
                PlanningApplication(
                    authority="Example Council",
                    uid="DEF456",
                    url="https://planning.example.gov.uk/detail/DEF456",
                    reference="24/99999/FUL",
                    description="New driveway gates",
                    date_received="2026-06-11",
                ),
            ]
            cancel_checks = 0

            def should_cancel() -> bool:
                nonlocal cancel_checks
                cancel_checks += 1
                return cancel_checks >= 3

            with patch("lead_generator.planning.leads.discover_portal_applications", return_value=applications):
                result = run_lead_search(config, should_cancel=should_cancel)

            csv_text = result.csv_path.read_text(encoding="utf-8")
            self.assertIn("24/01234/FUL", csv_text)
            self.assertNotIn("24/99999/FUL", csv_text)
            self.assertEqual(result.leads_found, 1)

    def test_run_lead_search_writes_failed_council_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson = root / "search.geojson"
            user_geojson.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [polygon_feature("search area", 0, 0, 2, 1)],
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
                                **polygon_feature("Broken Council", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Broken Council",
                                    "portal_family": "idox",
                                    "scraper_type": "Idox",
                                    "base_url": "https://broken.example.gov.uk",
                                    "listing_url": "https://broken.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Working Council", 1, 0, 2, 1),
                                "properties": {
                                    "authority": "Working Council",
                                    "portal_family": "idox",
                                    "scraper_type": "Idox",
                                    "base_url": "https://working.example.gov.uk",
                                    "listing_url": "https://working.example.gov.uk/search",
                                },
                            },
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

            def fake_discover(target, start_date, end_date):
                if target.authority == "Broken Council":
                    raise RuntimeError("portal exploded")
                return []

            with patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover):
                result = run_lead_search(config)

            with result.failure_csv_path.open(newline="", encoding="utf-8") as handle:
                failures = list(csv.DictReader(handle))
            self.assertEqual(len(failures), 1)
            self.assertEqual(failures[0]["council"], "Broken Council")
            self.assertEqual(failures[0]["portal_family"], "idox")
            self.assertEqual(failures[0]["scraper_type"], "Idox")
            self.assertEqual(failures[0]["listing_url"], "https://broken.example.gov.uk/search")
            self.assertEqual(failures[0]["reason"], "portal exploded")

    def test_run_lead_search_starts_four_workers_from_both_ends(self) -> None:
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
                                **polygon_feature("Council A", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Council A",
                                    "portal_family": "idox",
                                    "base_url": "https://a.example.gov.uk",
                                    "listing_url": "https://a.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Council B", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Council B",
                                    "portal_family": "idox",
                                    "base_url": "https://b.example.gov.uk",
                                    "listing_url": "https://b.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Council C", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Council C",
                                    "portal_family": "idox",
                                    "base_url": "https://c.example.gov.uk",
                                    "listing_url": "https://c.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Council D", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Council D",
                                    "portal_family": "idox",
                                    "base_url": "https://d.example.gov.uk",
                                    "listing_url": "https://d.example.gov.uk/search",
                                },
                            },
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
            started: list[str] = []
            lock = threading.Lock()
            all_started = threading.Event()

            def fake_discover(authority, start_date, end_date):
                target = authority
                with lock:
                    started.append(target.authority)
                    if len(started) >= 4:
                        all_started.set()
                all_started.wait(timeout=1)
                return []

            with patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover):
                result = run_lead_search(config)

            self.assertEqual(result.councils_completed, 4)
            self.assertEqual(set(started[:4]), {"Council A", "Council B", "Council C", "Council D"})

    def test_document_source_url_from_idox_summary_url_uses_documents_tab(self) -> None:
        self.assertEqual(
            document_source_url_from_application_url(
                "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123"
            ),
            "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=documents&keyVal=ABC123",
        )

    def test_planit_document_source_urls_use_portal_url_when_docs_url_missing(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url="https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123",
            source_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            raw={
                "source_url": "https://planning.example.gov.uk/online-applications/search.do?action=advanced",
                "portal_url": "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123",
            },
        )

        self.assertEqual(
            planit_document_source_urls(application),
            [
                "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=documents&keyVal=ABC123",
                "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123",
            ],
        )

    def test_application_link_prefers_application_page_over_search_page(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url="https://planning.example.gov.uk/detail/ABC123",
            source_url="https://planning.example.gov.uk/search",
            raw={
                "source_url": "https://planning.example.gov.uk/search",
                "portal_url": "https://planning.example.gov.uk/detail/ABC123",
            },
        )

        self.assertEqual(application_link(application), "https://planning.example.gov.uk/detail/ABC123")

    def test_enrich_planit_application_falls_back_to_portal_url_when_docs_url_missing(self) -> None:
        application = PlanningApplication(
            authority="BCP",
            uid="P/26/02835/HOU",
            url="https://planning.bcpcouncil.gov.uk/Planning/Display/P/26/02835/HOU",
            raw={"portal_url": "https://planning.bcpcouncil.gov.uk/Planning/Display/P/26/02835/HOU"},
        )
        documents = [PlanningDocument(title="Site Plan", url="https://planning.bcpcouncil.gov.uk/Document/Download?id=1")]

        with patch("lead_generator.planning.leads.fetch_planit_documents", return_value=documents) as fetch_documents:
            enriched = enrich_planit_application(application)

        fetch_documents.assert_called_once_with("https://planning.bcpcouncil.gov.uk/Planning/Display/P/26/02835/HOU")
        self.assertEqual(enriched.documents, documents)

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
                patch("lead_generator.planning.leads._build_document_opener", return_value=opener),
                patch("lead_generator.planning.leads.sleep"),
            ):
                downloaded = download_pdf_documents([document], Path(directory))

            self.assertEqual(downloaded, 1)
            self.assertTrue((Path(directory) / "Proposed plan.pdf").exists())
            self.assertEqual(opener.urls[0], document.source_url)
            self.assertEqual(opener.urls[1], document.url)
            self.assertIn("documentdownload.do", opener.urls[2])

    def test_document_download_warms_source_page_cookie_session(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes, content_type: str = "text/html") -> None:
                self._payload = payload
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return self._payload

        class FakeOpener:
            def __init__(self) -> None:
                self.warmed = False

            def open(self, request, timeout):
                if "activeTab=documents" in request.full_url:
                    self.warmed = True
                    return FakeResponse(b"<html>documents</html>")
                if not self.warmed:
                    raise HTTPError(request.full_url, 404, "Not Found", {}, None)
                return FakeResponse(b"%PDF-1.4", "application/pdf")

        document = PlanningDocument(
            title="Application form.pdf",
            url="https://planning.example.gov.uk/online-applications/files/hash/pdf/application.pdf",
            source_url="https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=documents&keyVal=ABC123",
        )

        with patch("lead_generator.planning.leads._build_document_opener", return_value=FakeOpener()):
            payload = download_document_bytes(document)

        self.assertEqual(payload, b"%PDF-1.4")

    def test_document_download_retries_with_tls_fallback_after_certificate_error(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

        class FailingOpener:
            def open(self, request, timeout):
                raise URLError(ssl.SSLCertVerificationError("certificate has expired"))

        class SuccessOpener:
            def open(self, request, timeout):
                return FakeResponse()

        document = PlanningDocument(
            title="Decision notice.pdf",
            url="https://documents.example.gov.uk/PublicAccess_LIVE/Document/ViewDocument?id=ABC123",
        )

        with patch(
            "lead_generator.planning.leads._build_document_opener",
            side_effect=[FailingOpener(), SuccessOpener()],
        ) as opener_factory:
            payload = download_document_bytes(document)

        self.assertEqual(payload, b"%PDF-1.4")
        self.assertEqual(opener_factory.call_count, 2)

    def test_document_download_candidates_add_idox_module_download_url(self) -> None:
        candidates = document_download_candidates(
            "https://planning.example.gov.uk/online-applications/documentviewer.do?keyVal=DOC001"
        )

        self.assertIn(
            "https://planning.example.gov.uk/online-applications/documentdownload.do?module=planning&keyVal=DOC001",
            candidates,
        )

    def test_iter_document_links_keeps_non_pdf_document_endpoints(self) -> None:
        document = html.fromstring(
            """
            <html><body>
              <a href="/OcellaWeb/viewDocument?file=dv_pl_files%5CAPP%5CApplicationFormRedacted.pdf&module=pl">
                View document
              </a>
              <a href="/online-applications/applicationDetails.do?activeTab=documents&keyVal=APP001">Documents tab</a>
            </body></html>
            """
        )

        links = list(iter_document_links(document, "https://planning.example.gov.uk/OcellaWeb/showDocuments"))

        self.assertEqual(len(links), 1)
        self.assertIn("viewDocument", links[0][0])
        self.assertEqual(links[0][1], "View document")

    def test_iter_document_links_extracts_public_access_model_rows(self) -> None:
        document = html.fromstring(
            """
            <html><body><script>
            var model = {"Rows":[{"Guid":"ABC123","Doc_Type":"Plan","Doc_Ref2":"Site layout.pdf"}],"FileSystemId":"PL"};
            </script></body></html>
            """
        )

        links = list(
            iter_document_links(
                document,
                "https://docs.example.gov.uk/PublicAccess_LIVE/SearchResult/RunThirdPartySearch?FileSystemId=PL",
            )
        )

        self.assertEqual(
            links,
            [("https://docs.example.gov.uk/PublicAccess_LIVE/Document/ViewDocument?id=ABC123", "Site layout.pdf")],
        )

    def test_iter_document_links_ignores_generic_site_documents(self) -> None:
        document = html.fromstring(
            """
            <html><body>
              <a href="https://council.example.gov.uk/Accessibility">Accessibility</a>
              <a href="/Document/Download?fileName=Design%20and%20Access%20Statement.pdf">Design and Access Statement</a>
            </body></html>
            """
        )

        links = list(iter_document_links(document, "https://planning.example.gov.uk/Planning/Display/ABC123"))

        self.assertEqual(
            links,
            [("/Document/Download?fileName=Design%20and%20Access%20Statement.pdf", "Design and Access Statement")],
        )

    def test_iter_document_links_reads_atrium_data_disabled_links(self) -> None:
        document = html.fromstring(
            """
            <html><body>
              <a data-disabled-link="/Document/Download?module=PLA&amp;recordNumber=1&amp;fileName=ApplicationFormRedacted.pdf"
                 class="singledownloadlink"
                 aria-label="Link(Download) ApplicationFormRedacted.pdf">01. Application Form</a>
            </body></html>
            """
        )

        links = list(iter_document_links(document, "https://planning.example.gov.uk/Planning/Display/ABC123"))

        self.assertEqual(
            links,
            [
                (
                    "/Document/Download?module=PLA&recordNumber=1&fileName=ApplicationFormRedacted.pdf",
                    "ApplicationFormRedacted.pdf",
                )
            ],
        )

    def test_fetch_publisher_document_list_reads_ajax_rows(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return (
                    b'{"data":[["Application Form","25/05/2022","","APPLICATION FORM REDACTED",'
                    b'"/docs/A29775F9/Document-A29775F9.pdf",""]],"serviceError":null}'
                )

        class FakeOpener:
            def open(self, request, timeout):
                self.request_url = request.full_url
                return FakeResponse()

        opener = FakeOpener()
        documents = fetch_publisher_document_list(
            '"url": "/publisher/mvc/getDocumentList;jsessionid=abc"',
            "https://app.example.gov.uk/planningdocuments=22%2F001",
            opener,
        )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].title, "APPLICATION FORM REDACTED")
        self.assertEqual(documents[0].url, "https://app.example.gov.uk/docs/A29775F9/Document-A29775F9.pdf")
        self.assertEqual(documents[0].source_url, "https://app.example.gov.uk/planningdocuments=22%2F001")

    def test_fetch_arcus_salesforce_document_list_reads_aura_rows(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "actions": [
                            {
                                "state": "SUCCESS",
                                "returnValue": [
                                    {
                                        "Id": "068ABC",
                                        "Title": "Design and Access Statement",
                                        "FileExtension": "pdf",
                                        "FileType": "PDF",
                                        "ContentSize": 12345,
                                        "Document_Type__c": "Design And Access Statement",
                                        "arcshared__Document_Date__c": "2026-01-13",
                                    }
                                ],
                            }
                        ]
                    }
                ).encode()

        class FakeOpener:
            def open(self, request, timeout):
                self.request_url = request.full_url
                self.request_data = request.data.decode()
                return FakeResponse()

        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "",
        }
        page_html = f'<script src="/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        opener = FakeOpener()

        documents = fetch_arcus_salesforce_document_list(
            page_html,
            "https://planning.example.gov.uk/s/papplication/a1M123/f26100751",
            opener,
        )

        self.assertEqual(len(documents), 1)
        self.assertIn("findContentVersionsForPlanning=1", opener.request_url)
        self.assertIn("recordId%22%3A%22a1M123", opener.request_data)
        self.assertEqual(documents[0].title, "Design and Access Statement.pdf")
        self.assertEqual(
            documents[0].url,
            "https://planning.example.gov.uk/sfc/servlet.shepherd/version/download/068ABC",
        )
        self.assertEqual(documents[0].document_type, "Design And Access Statement")

    def test_fetch_arcus_public_register_file_list_reads_milton_keynes_rows(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "actions": [
                            {
                                "state": "SUCCESS",
                                "returnValue": {
                                    "returnValue": [
                                        {
                                            "Id": "068MK",
                                            "Title": "Proposed Ground Floor Site Plan",
                                            "FileExtension": "pdf",
                                            "FileType": "PDF",
                                            "ContentSize": 383959,
                                            "arcshared__Category__c": "APPPLAN - Plans",
                                            "arcshared__Document_Date__c": "2026-06-17",
                                        }
                                    ]
                                },
                            }
                        ]
                    }
                ).encode()

        class FakeOpener:
            def open(self, request, timeout):
                self.request_url = request.full_url
                self.request_data = request.data.decode()
                return FakeResponse()

        boot = {
            "mode": "PROD",
            "app": "siteforce:communityApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:communityApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        opener = FakeOpener()

        documents = fetch_arcus_public_register_file_list(
            page_html,
            "https://www.be.milton-keynes.gov.uk/pr/s/detail/a0lQH000002K7XF",
            opener,
        )

        self.assertEqual(len(documents), 1)
        self.assertIn("aura.ApexAction.execute=1", opener.request_url)
        self.assertIn("PR_FilesListCont", opener.request_data)
        self.assertIn("a0lQH000002K7XF", opener.request_data)
        self.assertEqual(documents[0].title, "Proposed Ground Floor Site Plan.pdf")
        self.assertEqual(
            documents[0].url,
            "https://www.be.milton-keynes.gov.uk/pr/sfc/servlet.shepherd/version/download/068MK",
        )
        self.assertEqual(documents[0].document_type, "APPPLAN - Plans")

    def test_document_download_follows_html_intermediate_page(self) -> None:
        class FakeResponse:
            def __init__(self, url: str, payload: bytes, content_type: str) -> None:
                self._url = url
                self._payload = payload
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return self._payload

            def geturl(self) -> str:
                return self._url

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if request.full_url.endswith("/viewer"):
                    return FakeResponse(
                        request.full_url,
                        b'<html><body><a href="/download/file.pdf">Download file</a></body></html>',
                        "text/html",
                    )
                return FakeResponse(request.full_url, b"%PDF-1.4", "application/pdf")

        document = PlanningDocument(title="Viewer", url="https://planning.example.gov.uk/viewer")
        opener = FakeOpener()

        with patch("lead_generator.planning.leads._build_document_opener", return_value=opener):
            payload = download_document_bytes(document)

        self.assertEqual(payload, b"%PDF-1.4")
        self.assertEqual(opener.urls, ["https://planning.example.gov.uk/viewer", "https://planning.example.gov.uk/download/file.pdf"])

    def test_document_filename_uses_downloaded_content_type_when_title_has_no_extension(self) -> None:
        filename = document_filename(
            PlanningDocument(title="Planning statement", url="https://planning.example.gov.uk/download?id=1"),
            DownloadedFile(payload=b"%PDF-1.4", final_url="https://planning.example.gov.uk/download?id=1", content_type="application/pdf"),
            fallback="document-1",
        )

        self.assertEqual(filename, "Planning statement.pdf")

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
            patch("lead_generator.planning.leads._build_document_opener", return_value=opener),
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
