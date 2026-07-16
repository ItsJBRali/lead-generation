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

from lead_generator.planning.adapters.generic import GenericLabelledPlanningScraper
from lead_generator.planning.leads import (
    CouncilSearchDegradedError,
    CouncilTarget,
    DownloadedFile,
    LeadSearchConfig,
    _discover_planit_applications_serial,
    _fetch_json_with_retry,
    application_in_geojson,
    application_matches_search_area,
    application_matches,
    application_link,
    document_source_url_from_application_url,
    document_filename,
    document_download_candidates,
    discover_portal_applications,
    discover_portal_applications_with_deadline,
    download_document_bytes,
    download_pdf_documents,
    enrich_planit_application,
    fetch_arcus_public_register_file_list,
    fetch_arcus_files_public_document_list,
    fetch_arcus_salesforce_document_list,
    fetch_publisher_document_list,
    iter_document_links,
    load_authority_catalogue,
    planit_document_source_urls,
    parse_keywords,
    planning_scraper_for_target,
    point_in_geometry,
    run_lead_search,
    sanitize_path_part,
    select_overlapping_authorities,
)
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.http import CouncilHttpClient


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

    def test_builtin_catalogue_uses_nuneatons_current_public_planning_portal(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        properties = next(
            feature["properties"] for feature in catalogue["features"] if feature["properties"]["authority"] == "Nuneaton"
        )

        self.assertEqual(properties["portal_family"], "tascomi")
        self.assertEqual(properties["scraper_type"], "Tascomi")
        self.assertEqual(
            properties["listing_url"],
            "https://idoxcloud.nuneatonandbedworth.gov.uk/planning/index.html?fa=search",
        )

    def test_builtin_catalogue_includes_all_active_non_ni_authority_types(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        authorities = {feature["properties"]["authority"] for feature in catalogue["features"]}
        area_types = [feature["properties"]["area_type"] for feature in catalogue["features"]]

        self.assertEqual(len(catalogue["features"]), 399)
        self.assertEqual(area_types.count("Scottish Council"), 32)
        self.assertEqual(area_types.count("Welsh Principal Area"), 22)
        self.assertNotIn("Northern Ireland District", area_types)
        self.assertTrue(
            {
                "East Suffolk",
                "BCP",
                "North Northamptonshire",
                "West Northamptonshire",
                "Westmorland and Furness",
                "Adur and Worthing",
                "Mid Kent",
                "South West Devon",
                "Babergh Mid Suffolk",
                "Bromsgrove Redditch",
                "Chiltern South Bucks",
                "South Norfolk Broadland",
                "Bath",
                "Carmarthenshire",
                "Colchester",
                "East Dunbartonshire",
                "Telford",
            }.issubset(authorities)
        )

    def test_builtin_catalogue_records_shared_and_current_council_codes(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        properties = [feature["properties"] for feature in catalogue["features"]]
        covered_codes = {
            code
            for item in properties
            for code in ([item.get("gss_code")] if item.get("gss_code") else []) + item.get("covered_gss_codes", [])
        }

        self.assertTrue(
            {
                "E06000063",  # Cumberland's three legacy planning registers
                "E07000044",  # South Hams via South West Devon
                "E07000110",  # Maidstone via Mid Kent
                "E07000223",  # Adur via Adur and Worthing
                "E08000037",  # Gateshead's current code
                "S12000047",  # Fife's current code
                "S12000048",  # Perth and Kinross's current code
                "S12000049",  # Glasgow City's current code
                "S12000050",  # North Lanarkshire's current code
            }.issubset(covered_codes)
        )

    def test_builtin_catalogue_has_a_supported_adapter_for_every_target(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        generic_authorities: list[str] = []
        for feature in catalogue["features"]:
            properties = feature["properties"]
            target = CouncilTarget(
                authority=properties["authority"],
                portal_family=properties["portal_family"],
                scraper_type=properties["scraper_type"],
                base_url=properties["base_url"],
                listing_url=properties["listing_url"],
                geometry=feature["geometry"],
                link_test_ok=properties["link_test_ok"],
            )
            scraper = planning_scraper_for_target(target)
            if type(scraper) is GenericLabelledPlanningScraper:
                generic_authorities.append(target.authority)

        self.assertEqual(generic_authorities, [])

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

    def test_application_matches_excludes_admin_proposal_phrases(self) -> None:
        excluded_proposals = [
            "Variation of condition 2 to allow automated gates",
            "Discharge of condition 4 relating to boundary treatment",
            "Details required by condition 3 for entrance gates",
            "Request for EIA screening opinion for access works",
            "Compliance with condition 5 for gate details",
            "Details of reserved matters including access",
            "Submission of details for new access",
            "Details pursuant to condition 6 for driveway gates",
            "Section 73 application for gates",
            "Application to vary approved access condition",
            "Submission of material samples for gate pillars",
            "Submission of surface water details by front gate",
            "EDC Consultation for new gates",
            "Removal of condition 2 for boundary gates",
            "Partial approval of details for entrance gates",
            "Noise Assessment for new automated gates",
        ]

        for proposal in excluded_proposals:
            with self.subTest(proposal=proposal):
                application = PlanningApplication(
                    authority="Example",
                    uid="1",
                    url="https://example.test",
                    description=proposal,
                    date_received="2026-06-12",
                )

                self.assertFalse(
                    application_matches(
                        application,
                        date(2026, 6, 1),
                        date(2026, 6, 30),
                        ["gates", "access", "boundary"],
                    )
                )

    def test_application_matches_excludes_retrospective_unless_part_retrospective(self) -> None:
        retrospective = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            description="Retrospective installation of automated gates",
            date_received="2026-06-12",
        )
        part_retrospective = PlanningApplication(
            authority="Example",
            uid="2",
            url="https://example.test",
            description="Part retrospective installation of automated gates",
            date_received="2026-06-12",
        )
        apartment = PlanningApplication(
            authority="Example",
            uid="3",
            url="https://example.test",
            description="Retrospective installation of gates to apartment entrance",
            date_received="2026-06-12",
        )

        self.assertFalse(application_matches(retrospective, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))
        self.assertTrue(application_matches(part_retrospective, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))
        self.assertFalse(application_matches(apartment, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))

    def test_application_matches_excludes_proposals_starting_with_t1(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            description="T1 - Oak - install replacement boundary gates",
            date_received="2026-06-12",
        )

        self.assertFalse(application_matches(application, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))

    def test_application_matches_excludes_proposals_starting_with_g1(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="1",
            url="https://example.test",
            description="G1 Mixed trees - install replacement boundary gates",
            date_received="2026-06-12",
        )

        self.assertFalse(application_matches(application, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))

    def test_application_matches_excludes_old_references(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="OLD-2026-001",
            url="https://example.test",
            reference="OLD/2026/001",
            description="Installation of automated gates",
            date_received="2026-06-12",
        )

        self.assertFalse(application_matches(application, date(2026, 6, 1), date(2026, 6, 30), ["gates"]))

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

    def test_discover_portal_applications_uses_planit_first_for_problem_portals(self) -> None:
        target = CouncilTarget(
            authority="Surrey",
            portal_family="unknown",
            scraper_type="Atrium",
            base_url="https://planning.surreycc.gov.uk/",
            listing_url="https://planning.surreycc.gov.uk/planappsearch.aspx",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Surrey",
                uid="PLAN/2026/0498",
                url="https://example.test/app",
                reference="PLAN/2026/0498",
                date_received="2026-06-10",
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target") as scraper_factory,
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback) as planit,
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        scraper_factory.assert_not_called()
        planit.assert_called_once_with("Surrey", date(2026, 6, 8), date(2026, 6, 14))
        self.assertEqual([application.reference for application in applications], ["PLAN/2026/0498"])
        self.assertEqual(applications[0].raw["source"], "planit_fallback")

    def test_discover_portal_applications_marks_blocked_but_responsive_portal_as_degraded(self) -> None:
        class BlockedScraper:
            def discover_ids(self, **kwargs):
                raise RuntimeError("HTTP 403 while fetching https://publicaccess.portsmouth.gov.uk")

        target = CouncilTarget(
            authority="Portsmouth",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://publicaccess.portsmouth.gov.uk",
            listing_url="https://publicaccess.portsmouth.gov.uk/online-applications/search.do?action=advanced",
            geometry={},
        )

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=BlockedScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=[]),
        ):
            with self.assertRaisesRegex(CouncilSearchDegradedError, "HTTP 403"):
                discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

    def test_discover_portal_applications_keeps_server_outage_as_failure(self) -> None:
        class UnavailableScraper:
            def discover_ids(self, **kwargs):
                raise RuntimeError("HTTP 503 while fetching planning search")

        target = CouncilTarget(
            authority="Unavailable",
            portal_family="custom",
            scraper_type="Custom",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=UnavailableScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=[]),
        ):
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

    def test_discover_portal_applications_does_not_treat_local_timeout_as_confirmed_outage(self) -> None:
        class TimedOutScraper:
            def discover_ids(self, **kwargs):
                raise RuntimeError("The read operation timed out")

        target = CouncilTarget(
            authority="Slow Council",
            portal_family="custom",
            scraper_type="Custom",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=TimedOutScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=[]),
        ):
            with self.assertRaisesRegex(CouncilSearchDegradedError, "timed out"):
                discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

    def test_discover_portal_applications_retries_with_a_fresh_session(self) -> None:
        from lead_generator.planning.models import DiscoveryResult

        class BlockedSession:
            def discover_ids(self, **kwargs):
                raise RuntimeError("HTTP 429 while fetching planning search")

        class WorkingSession:
            def discover_ids(self, **kwargs):
                return DiscoveryResult(
                    authority="Example",
                    source_url="https://planning.example.gov.uk/search",
                    applications=[
                        PlanningApplication(
                            authority="Example",
                            uid="26/00001/FUL",
                            url="https://planning.example.gov.uk/application/1",
                            reference="26/00001/FUL",
                            description="New driveway gates",
                            date_received="2026-06-10",
                            raw={"detail_complete": True},
                        )
                    ],
                )

        target = CouncilTarget(
            authority="Example",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )

        with (
            patch(
                "lead_generator.planning.leads.planning_scraper_for_target",
                side_effect=[BlockedSession(), WorkingSession()],
            ) as scraper_factory,
            patch("lead_generator.planning.leads.discover_planit_applications") as planit,
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        self.assertEqual([application.reference for application in applications], ["26/00001/FUL"])
        self.assertEqual(scraper_factory.call_count, 2)
        planit.assert_not_called()

    def test_discover_portal_applications_uses_planit_for_empty_portal_result(self) -> None:
        class EmptyScraper:
            def discover_ids(self, **kwargs):
                from lead_generator.planning.models import DiscoveryResult

                return DiscoveryResult(authority="Example", source_url="https://planning.example.gov.uk", applications=[])

        target = CouncilTarget(
            authority="Example",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/online-applications/search.do?action=advanced",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Example",
                uid="26/01723/NMC",
                url="https://example.test/app",
                reference="26/01723/NMC",
                date_received="2026-06-10",
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=EmptyScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback) as planit,
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        planit.assert_called_once_with("Example", date(2026, 6, 8), date(2026, 6, 14))
        self.assertEqual([application.reference for application in applications], ["26/01723/NMC"])
        self.assertEqual(applications[0].raw["source"], "planit_fallback")

    def test_discover_portal_applications_supplements_failed_details_from_planit(self) -> None:
        from lead_generator.planning.models import DiscoveryResult

        class PartialScraper:
            def discover_ids(self, **kwargs):
                return DiscoveryResult(
                    authority="Example",
                    source_url="https://planning.example.gov.uk/search",
                    applications=[
                        PlanningApplication(
                            authority="Example",
                            uid="APP1",
                            url="https://planning.example.gov.uk/application/APP1",
                            reference="26/00001/FUL",
                            date_received="2026-06-10",
                        )
                    ],
                )

            def fetch_application(self, *args, **kwargs):
                raise RuntimeError("detail page changed")

        target = CouncilTarget(
            authority="Example",
            portal_family="custom",
            scraper_type="Custom",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )
        fallback = [
            PlanningApplication(
                authority="Example",
                uid="PLANIT1",
                url="https://planit.example.test/application/1",
                reference="26/00001/FUL",
                address="1 High Street AB1 2CD",
                description="Install entrance gates",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [-0.1, 51.5]}},
            )
        ]

        with (
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=PartialScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=fallback),
        ):
            applications = discover_portal_applications(target, date(2026, 6, 8), date(2026, 6, 14))

        self.assertEqual(len(applications), 1)
        self.assertEqual(applications[0].url, "https://planning.example.gov.uk/application/APP1")
        self.assertEqual(applications[0].address, "1 High Street AB1 2CD")
        self.assertEqual(applications[0].description, "Install entrance gates")
        self.assertTrue(applications[0].raw["planit_supplemented"])
        self.assertIn("location", applications[0].raw)

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

    def test_run_lead_search_removes_duplicate_exact_references(self) -> None:
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
            applications = [
                PlanningApplication(
                    authority="Example Council",
                    uid="ABC123",
                    url="https://planning.example.gov.uk/detail/ABC123",
                    reference="24/01234/FUL",
                    address="1 Example Street",
                    description="New driveway gates",
                    date_received="2026-06-10",
                ),
                PlanningApplication(
                    authority="Example Council",
                    uid="DEF456",
                    url="https://planning.example.gov.uk/detail/DEF456",
                    reference="24/01234/FUL",
                    address="2 Example Street",
                    description="New driveway gates",
                    date_received="2026-06-10",
                ),
            ]

            captured_counts: list[int] = []
            with patch("lead_generator.planning.leads.discover_portal_applications", return_value=applications):
                result = run_lead_search(config, captured=captured_counts.append)

            self.assertEqual(result.leads_found, 1)
            self.assertEqual(captured_counts, [1])
            with result.csv_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Reference"], "24/01234/FUL")
            self.assertEqual(rows[0]["application link"], "https://planning.example.gov.uk/detail/ABC123")

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

            broken_attempts = 0

            def fake_discover(target, start_date, end_date):
                nonlocal broken_attempts
                if target.authority == "Broken Council":
                    broken_attempts += 1
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
            self.assertEqual(broken_attempts, 2)

    def test_run_lead_search_records_degraded_portal_without_failing_run(self) -> None:
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
                                **polygon_feature("Responsive Council", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Responsive Council",
                                    "portal_family": "idox",
                                    "base_url": "https://responsive.example.gov.uk",
                                    "listing_url": "https://responsive.example.gov.uk/search",
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
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )

            with patch(
                "lead_generator.planning.leads.discover_portal_applications",
                side_effect=CouncilSearchDegradedError("HTTP 403 while fetching portal"),
            ), patch("lead_generator.planning.leads.PLATFORM_BLOCKED_COOLDOWN_SECONDS", 0):
                result = run_lead_search(config)

            self.assertEqual(result.failed_councils, [])
            self.assertEqual(result.no_application_councils, [])
            self.assertEqual(result.completion, "Completed")
            with result.failure_csv_path.open(newline="", encoding="utf-8") as handle:
                failures = list(csv.DictReader(handle))
            self.assertEqual(len(failures), 1)
            self.assertIn("Responsive portal search issue", failures[0]["reason"])

    def test_run_lead_search_appends_persistent_history_row(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson = root / "search.geojson"
            user_geojson.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [polygon_feature("search area", 0, 0, 3, 1)],
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
                                **polygon_feature("Application Council", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Application Council",
                                    "portal_family": "idox",
                                    "base_url": "https://applications.example.gov.uk",
                                    "listing_url": "https://applications.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Empty Council", 1, 0, 2, 1),
                                "properties": {
                                    "authority": "Empty Council",
                                    "portal_family": "idox",
                                    "base_url": "https://empty.example.gov.uk",
                                    "listing_url": "https://empty.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Broken Council", 2, 0, 3, 1),
                                "properties": {
                                    "authority": "Broken Council",
                                    "portal_family": "idox",
                                    "base_url": "https://broken.example.gov.uk",
                                    "listing_url": "https://broken.example.gov.uk/search",
                                },
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            history_path = root / "archive" / "search_history.csv"
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                history_csv_path=history_path,
                worker_count=1,
            )

            def fake_discover(target, start_date, end_date):
                if target.authority == "Broken Council":
                    raise RuntimeError("portal exploded")
                if target.authority == "Empty Council":
                    return []
                return [
                    PlanningApplication(
                        authority=target.authority,
                        uid="ABC123",
                        url="https://applications.example.gov.uk/detail/ABC123",
                        reference="24/01234/FUL",
                        description="Install driveway gates",
                        date_received="2026-06-10",
                    ),
                    PlanningApplication(
                        authority=target.authority,
                        uid="DEF456",
                        url="https://applications.example.gov.uk/detail/DEF456",
                        reference="24/99999/FUL",
                        description="Build rear extension",
                        date_received="2026-06-10",
                    ),
                ]

            def fake_enrich(application):
                application.documents = [
                    PlanningDocument(
                        title="Proposed plan.pdf",
                        url="https://applications.example.gov.uk/document/proposed.pdf",
                    )
                ]
                return application

            captured_counts: list[int] = []
            with (
                patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover),
                patch("lead_generator.planning.leads.enrich_application_documents", side_effect=fake_enrich),
                patch("lead_generator.planning.leads.download_pdf_documents", return_value=1),
            ):
                result = run_lead_search(config, captured=captured_counts.append)

            self.assertEqual(result.total_applications, 2)
            self.assertEqual(result.leads_found, 1)
            self.assertEqual(result.captured_documents, 1)
            self.assertEqual(result.failed_councils, ["Broken Council"])
            self.assertEqual(result.no_application_councils, ["Empty Council"])
            self.assertEqual(result.completion, "Failed")
            self.assertEqual(captured_counts, [1])

            with history_path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["Keyword Set"], "Bespoke")
            self.assertEqual(rows[0]["Total Applications"], "2")
            self.assertEqual(rows[0]["Relevant Captured Applications"], "1")
            self.assertEqual(rows[0]["% Relevant"], "50.00%")
            self.assertEqual(rows[0]["List of failed councils"], "Broken Council")
            self.assertEqual(rows[0]["List of councils with no applications"], "Empty Council")
            self.assertEqual(rows[0]["Completion"], "Failed")
            self.assertEqual(rows[0]["Captured Documents"], "1")

    def test_run_lead_search_round_robins_platform_queues(self) -> None:
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
                                    "portal_family": "arcus",
                                    "scraper_type": "Arcus",
                                    "base_url": "https://c.example.gov.uk",
                                    "listing_url": "https://c.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Council D", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Council D",
                                    "portal_family": "civica",
                                    "scraper_type": "Civica",
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
                worker_count=1,
            )
            started: list[str] = []

            def fake_discover(target, start_date, end_date):
                started.append(target.authority)
                return []

            with patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover):
                result = run_lead_search(config)

            self.assertEqual(result.councils_completed, 4)
            self.assertEqual(started, ["Council A", "Council C", "Council D", "Council B"])

    def test_run_lead_search_retries_rate_limited_council_after_first_pass(self) -> None:
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
            council_specs = [
                ("Rate Limited", "idox"),
                ("Idox Working", "idox"),
                ("Arcus Working", "arcus"),
            ]
            catalogue = root / "catalogue.geojson"
            catalogue.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                **polygon_feature(authority, 0, 0, 1, 1),
                                "properties": {
                                    "authority": authority,
                                    "portal_family": platform,
                                    "scraper_type": platform.title(),
                                    "base_url": f"https://{authority.casefold().replace(' ', '-')}.example.gov.uk",
                                    "listing_url": f"https://{authority.casefold().replace(' ', '-')}.example.gov.uk/search",
                                },
                            }
                            for authority, platform in council_specs
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
                worker_count=1,
            )
            calls: list[str] = []
            rate_limited_attempts = 0

            def fake_discover(target, start_date, end_date):
                nonlocal rate_limited_attempts
                calls.append(target.authority)
                if target.authority == "Rate Limited":
                    rate_limited_attempts += 1
                    if rate_limited_attempts == 1:
                        raise CouncilSearchDegradedError("HTTP 429 while fetching portal")
                return []

            with (
                patch("lead_generator.planning.leads.PLATFORM_RATE_LIMIT_COOLDOWN_SECONDS", 0),
                patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover),
            ):
                result = run_lead_search(config)

            self.assertEqual(calls, ["Rate Limited", "Arcus Working", "Idox Working", "Rate Limited"])
            self.assertEqual(result.councils_completed, 3)
            self.assertEqual(result.failed_councils, [])
            self.assertEqual(result.completion, "Completed")
            with result.failure_csv_path.open(newline="", encoding="utf-8") as handle:
                self.assertEqual(list(csv.DictReader(handle)), [])

    def test_run_lead_search_times_out_stuck_council_and_continues_queue(self) -> None:
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
                                **polygon_feature("Stuck Council", 0, 0, 1, 1),
                                "properties": {
                                    "authority": "Stuck Council",
                                    "portal_family": "idox",
                                    "scraper_type": "Idox",
                                    "base_url": "https://stuck.example.gov.uk",
                                    "listing_url": "https://stuck.example.gov.uk/search",
                                },
                            },
                            {
                                **polygon_feature("Working Council", 1, 0, 2, 1),
                                "properties": {
                                    "authority": "Working Council",
                                    "portal_family": "arcus",
                                    "scraper_type": "Arcus",
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
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            release_stuck_searches = threading.Event()
            calls: list[str] = []
            logs: list[str] = []

            def fake_discover(target, start_date, end_date):
                calls.append(target.authority)
                if target.authority == "Stuck Council":
                    release_stuck_searches.wait(timeout=2)
                return []

            try:
                with (
                    patch("lead_generator.planning.leads.COUNCIL_SEARCH_INACTIVITY_TIMEOUT_SECONDS", 0.08),
                    patch("lead_generator.planning.leads.COUNCIL_SEARCH_HEARTBEAT_SECONDS", 0.02),
                    patch("lead_generator.planning.leads.search_worker_start_delay", return_value=0),
                    patch(
                        "lead_generator.planning.leads.discover_portal_applications",
                        side_effect=fake_discover,
                    ),
                ):
                    result = run_lead_search(config, log=logs.append)
            finally:
                release_stuck_searches.set()

            self.assertEqual(calls, ["Stuck Council", "Working Council", "Stuck Council"])
            self.assertEqual(result.councils_completed, 2)
            self.assertEqual(result.failed_councils, ["Stuck Council"])
            self.assertTrue(any("still searching" in message for message in logs))
            deferred_index = next(
                index for index, message in enumerate(logs) if "Stuck Council: deferred" in message
            )
            working_index = next(
                index for index, message in enumerate(logs) if "searching Working Council" in message
            )
            retry_index = next(
                index for index, message in enumerate(logs) if "final retry for Stuck Council" in message
            )
            self.assertLess(deferred_index, working_index)
            self.assertLess(working_index, retry_index)

    def test_council_deadline_allows_active_requests_past_inactivity_limit(self) -> None:
        class FakeHeaders:
            def get_content_charset(self):
                return "utf-8"

        class FakeResponse:
            headers = FakeHeaders()
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def geturl(self):
                return "https://planning.example.gov.uk/search"

            def read(self):
                return b"<html>active</html>"

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse()

        class ActiveClient(CouncilHttpClient):
            def _opener(self):
                return FakeOpener()

        target = CouncilTarget(
            authority="Active Council",
            portal_family="idox",
            scraper_type="Idox",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )
        client = ActiveClient(min_delay_seconds=0)

        def active_discover(*args, **kwargs):
            for _ in range(5):
                client.get("https://planning.example.gov.uk/search")
                threading.Event().wait(0.03)
            return []

        with patch(
            "lead_generator.planning.leads.discover_portal_applications",
            side_effect=active_discover,
        ):
            applications = discover_portal_applications_with_deadline(
                target,
                date(2026, 7, 6),
                date(2026, 7, 12),
                timeout_seconds=0.05,
                max_elapsed_seconds=1.0,
                heartbeat_seconds=0.02,
            )

        self.assertEqual(applications, [])

    def test_run_lead_search_caps_configured_worker_count_at_eight(self) -> None:
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
            platforms = ["idox"] * 4 + ["custom"] * 3 + ["arcus"] * 3
            catalogue.write_text(
                json.dumps(
                    {
                        "type": "FeatureCollection",
                        "features": [
                            {
                                **polygon_feature(f"Council {index}", 0, 0, 1, 1),
                                "properties": {
                                    "authority": f"Council {index}",
                                    "portal_family": platforms[index],
                                    "scraper_type": platforms[index].title(),
                                    "base_url": f"https://{index}.example.gov.uk",
                                    "listing_url": f"https://{index}.example.gov.uk/search",
                                },
                            }
                            for index in range(10)
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
                worker_count=99,
            )
            started: list[str] = []
            active = 0
            max_active = 0
            lock = threading.Lock()
            first_batch_ready = threading.Event()
            release_workers = threading.Event()

            def fake_discover(target, start_date, end_date):
                nonlocal active, max_active
                with lock:
                    started.append(target.authority)
                    active += 1
                    max_active = max(max_active, active)
                    if active >= 8:
                        first_batch_ready.set()
                release_workers.wait(timeout=1)
                with lock:
                    active -= 1
                return []

            def release_after_first_batch() -> None:
                first_batch_ready.wait(timeout=1)
                release_workers.set()

            releaser = threading.Thread(target=release_after_first_batch, daemon=True)
            releaser.start()
            with (
                patch("lead_generator.planning.leads.search_worker_start_delay", return_value=0),
                patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover),
            ):
                result = run_lead_search(config)
            releaser.join(timeout=1)

            self.assertEqual(result.councils_completed, 10)
            self.assertEqual(max_active, 8)

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

    def test_enrich_application_documents_merges_documents_from_every_source(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url="https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123",
            raw={
                "docs_url": "https://documents.example.gov.uk/PublicAccess_LIVE/SearchResult/RunThirdPartySearch?FileSystemId=PL",
                "portal_url": "https://planning.example.gov.uk/online-applications/applicationDetails.do?activeTab=summary&keyVal=ABC123",
            },
        )

        def fake_fetch(url: str) -> list[PlanningDocument]:
            if "SearchResult" in url:
                return [
                    PlanningDocument(title="Application form.pdf", url="https://documents.example.gov.uk/document/form.pdf"),
                    PlanningDocument(title="Site plan.pdf", url="https://documents.example.gov.uk/document/plan.pdf"),
                ]
            if "activeTab=documents" in url:
                return [
                    PlanningDocument(title="Site plan.pdf", url="https://documents.example.gov.uk/document/plan.pdf"),
                    PlanningDocument(title="Decision notice.pdf", url="https://planning.example.gov.uk/document/decision.pdf"),
                ]
            if "activeTab=summary" in url:
                return [PlanningDocument(title="Proposed elevations.dwg", url="https://planning.example.gov.uk/document/elevations.dwg")]
            return []

        with patch("lead_generator.planning.leads.fetch_planit_documents", side_effect=fake_fetch) as fetch_documents:
            enriched = enrich_planit_application(application)

        self.assertGreaterEqual(fetch_documents.call_count, 3)
        self.assertEqual(
            [document.title for document in enriched.documents],
            ["Application form.pdf", "Site plan.pdf", "Decision notice.pdf", "Proposed elevations.dwg"],
        )

    def test_download_pdf_documents_skips_exe_and_existing_only_files(self) -> None:
        documents = [
            PlanningDocument(title="Existing elevations.pdf", url="https://planning.example.gov.uk/docs/existing-elevations.pdf"),
            PlanningDocument(title="Existing and proposed elevations.pdf", url="https://planning.example.gov.uk/docs/existing-proposed-elevations.pdf"),
            PlanningDocument(title="Viewer.exe", url="https://planning.example.gov.uk/docs/viewer.exe"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lead_generator.planning.leads.download_document_file",
                return_value=DownloadedFile(
                    payload=b"%PDF-1.4",
                    final_url="https://planning.example.gov.uk/docs/existing-proposed-elevations.pdf",
                    content_type="application/pdf",
                ),
            ) as download_file:
                downloaded = download_pdf_documents(documents, Path(directory))

            self.assertEqual(downloaded, 1)
            download_file.assert_called_once_with(documents[1])
            self.assertTrue((Path(directory) / "Existing and proposed elevations.pdf").exists())
            self.assertFalse((Path(directory) / "Existing elevations.pdf").exists())
            self.assertFalse((Path(directory) / "Viewer.exe").exists())

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

    def test_iter_document_links_reads_get_download_forms(self) -> None:
        document = html.fromstring(
            """
            <html><body>
              <form method="get" action="/Document/Download">
                <input type="hidden" name="module" value="PLA">
                <input type="hidden" name="id" value="ABC123">
                <button>Download Proposed plan.pdf</button>
              </form>
            </body></html>
            """
        )

        links = list(iter_document_links(document, "https://planning.example.gov.uk/Planning/Display/ABC123"))

        self.assertEqual(
            links,
            [
                (
                    "https://planning.example.gov.uk/Document/Download?module=PLA&id=ABC123",
                    "Download Proposed plan.pdf",
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

    def test_fetch_arcus_files_public_document_list_reads_wiltshire_rows(self) -> None:
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
                                        "Id": "068WILTS",
                                        "ContentDocumentId": "069WILTS",
                                        "Title": "Proposed site plan",
                                        "arcshared__Category__c": "Plans",
                                        "arcshared__Document_Date__c": "2026-07-10",
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
            "pathPrefix": "/pr",
        }
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        opener = FakeOpener()

        documents = fetch_arcus_files_public_document_list(
            page_html,
            "https://development.wiltshire.gov.uk/pr/s/planning-application/a0iWILTS/pl202600001",
            opener,
        )

        self.assertEqual(len(documents), 1)
        self.assertIn("arcshared.FilesPublicCont.getFiles=1", opener.request_url)
        self.assertIn("FilesPublicCont", opener.request_data)
        self.assertIn("a0iWILTS", opener.request_data)
        self.assertEqual(documents[0].title, "Proposed site plan")
        self.assertEqual(
            documents[0].url,
            "https://development.wiltshire.gov.uk/pr/sfc/servlet.shepherd/version/download/068WILTS",
        )
        self.assertEqual(documents[0].document_type, "Plans")

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

    def test_planit_pagination_rejects_a_repeated_page(self) -> None:
        repeated_page = {
            "records": [{"uid": "24/00001/FUL", "description": "Driveway gates"}],
            "total": 2,
        }

        with patch(
            "lead_generator.planning.leads._fetch_json_with_retry",
            return_value=repeated_page,
        ) as fetch:
            with self.assertRaisesRegex(RuntimeError, "repeated pagination page 2"):
                _discover_planit_applications_serial(
                    "Brighton",
                    date(2026, 6, 1),
                    date(2026, 6, 30),
                )

        self.assertEqual(fetch.call_count, 2)

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

    def test_fetch_json_retries_planit_tls_verification_failure_with_compat_opener(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b'{"records": [{"uid": "26/0001"}]}'

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse()

        tls_error = ssl.SSLCertVerificationError("certificate verify failed")

        with (
            patch("lead_generator.planning.leads.urlopen", side_effect=URLError(tls_error)),
            patch("lead_generator.planning.leads.build_opener", return_value=FakeOpener()) as build_opener_mock,
            patch("lead_generator.planning.leads._throttle_request"),
            patch("lead_generator.planning.leads._skip_next_throttle"),
        ):
            payload = _fetch_json_with_retry("https://www.planit.org.uk/api/applics/json")

        self.assertEqual(payload, {"records": [{"uid": "26/0001"}]})
        build_opener_mock.assert_called_once()

    def test_fetch_json_retries_rate_limit_after_planit_tls_compat_retry(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b'{"records": [{"uid": "26/0002"}]}'

        class FakeOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(request.full_url, 429, "Too Many Requests", {"Retry-After": "2"}, None)
                return FakeResponse()

        tls_error = ssl.SSLCertVerificationError("certificate verify failed")
        opener = FakeOpener()

        with (
            patch("lead_generator.planning.leads.urlopen", side_effect=URLError(tls_error)),
            patch("lead_generator.planning.leads.build_opener", return_value=opener),
            patch("lead_generator.planning.leads._throttle_request"),
            patch("lead_generator.planning.leads._skip_next_throttle"),
            patch("lead_generator.planning.leads.sleep") as sleep_mock,
        ):
            payload = _fetch_json_with_retry("https://www.planit.org.uk/api/applics/json")

        self.assertEqual(payload, {"records": [{"uid": "26/0002"}]})
        self.assertEqual(opener.calls, 2)
        sleep_mock.assert_called_once_with(2.0)

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
