from __future__ import annotations

import csv
import json
import ssl
import threading
import tempfile
import unittest
from datetime import date
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request
from pathlib import Path
from unittest.mock import patch

from lxml import html

import lead_generator.planning.leads as leads_module
from lead_generator.planning.adapters.generic import GenericLabelledPlanningScraper
from lead_generator.planning.enrichment import ContactEnrichment
from lead_generator.planning.drawing_sources import is_existing_only_drawing_metadata
from lead_generator.planning.leads import (
    CouncilSearchCancelledError,
    CouncilSearchDegradedError,
    CouncilTarget,
    DocumentDownloadBatchResult,
    DocumentDownloadCancelledError,
    DocumentDiscoveryResult,
    DocumentDiscoveryTransientError,
    DocumentSourceFailure,
    DownloadedFile,
    LeadSearchConfig,
    _discover_planit_applications_serial,
    _download_document_file,
    _download_pdf_documents_once,
    _open_url_with_retry,
    _throttle_request,
    _is_document_link_text,
    _looks_like_listing_url,
    _fetch_json_with_retry,
    _associated_document_source_urls,
    application_in_geojson,
    application_matches_search_area,
    application_matches,
    application_link,
    document_source_url_from_application_url,
    document_filename,
    document_download_candidates,
    discover_application_documents,
    discover_portal_applications,
    discover_portal_applications_with_deadline,
    download_document_bytes,
    download_document_file,
    download_pdf_documents,
    enrich_planit_application,
    fetch_arcus_public_register_file_list,
    fetch_arcus_files_public_document_list,
    fetch_arcus_salesforce_document_list,
    fetch_atrium_document_list,
    fetch_enterprise_document_list,
    fetch_planit_documents,
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
    source_document_candidates,
)
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.http import CouncilFetchError, CouncilHttpClient


def polygon_feature(name: str, xmin: float, ymin: float, xmax: float, ymax: float) -> dict[str, object]:
    return {
        "type": "Feature",
        "properties": {"name": name},
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[xmin, ymin], [xmax, ymin], [xmax, ymax], [xmin, ymax], [xmin, ymin]]],
        },
    }


def write_search_fixture(root: Path, authorities: list[str]) -> tuple[Path, Path]:
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
                        **polygon_feature(authority, 0, 0, 1, 1),
                        "properties": {
                            "authority": authority,
                            "portal_family": "idox",
                            "scraper_type": "Idox",
                            "base_url": f"https://{index}.planning.example.gov.uk",
                            "listing_url": f"https://{index}.planning.example.gov.uk/search",
                            "link_test_ok": True,
                        },
                    }
                    for index, authority in enumerate(authorities, start=1)
                ],
            }
        ),
        encoding="utf-8",
    )
    return user_geojson, catalogue


class LeadSearchTest(unittest.TestCase):
    def setUp(self) -> None:
        with leads_module._REQUEST_THROTTLE_LOCK:
            leads_module._LAST_REQUEST_AT.clear()
            leads_module._REQUEST_COOLDOWN_UNTIL.clear()

    def test_existing_only_drawing_metadata(self) -> None:
        assert is_existing_only_drawing_metadata("Existing elevations.pdf")
        assert not is_existing_only_drawing_metadata("Existing survey report.pdf")
        assert not is_existing_only_drawing_metadata("Existing planning statement.pdf")

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

    def test_builtin_catalogue_uses_east_hampshires_new_public_planning_portal(self) -> None:
        catalogue = load_authority_catalogue(Path("src/lead_generator/planning/data/planning_authorities.geojson"))
        properties = next(
            feature["properties"]
            for feature in catalogue["features"]
            if feature["properties"]["authority"] == "East Hampshire"
        )

        self.assertEqual(properties["portal_family"], "tascomi")
        self.assertEqual(properties["scraper_type"], "Tascomi")
        self.assertEqual(
            properties["listing_url"],
            "https://publicaccess.easthants.gov.uk/planning/index.html?fa=search",
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
            "Works to Holly - install replacement boundary gates",
            "Please note this is not a planning application for new gates",
            "Application for approval of details for the access gates",
            "Details submitted to satisfy the approved boundary treatment",
            "Display for temporary period beside the entrance gates",
            "Adjoining consultation for a gated development",
            "Pending decision for the proposed access gates",
            "Fell 1 x apple beside the entrance gates",
            "Consultation by the adjoining authority for new gates",
            "Consultation request regarding boundary gates",
            "Details of tree works beside the proposed gates",
            "Installation of gates pursuant to Condition 17",
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

    def test_application_matches_excludes_new_start_only_proposal_phrases(self) -> None:
        excluded_proposals = [
            "Details of condition approval for replacement gates",
            "Detail of condition submission for boundary gates",
            "Removal condition application for entrance gates",
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
                        ["gates"],
                    )
                )

        non_prefixed = PlanningApplication(
            authority="Example",
            uid="2",
            url="https://example.test",
            description="Replacement gates following details of condition approval",
            date_received="2026-06-12",
        )
        self.assertTrue(
            application_matches(
                non_prefixed,
                date(2026, 6, 1),
                date(2026, 6, 30),
                ["gates"],
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

    def test_portal_filtered_weekly_result_is_not_removed_by_received_date(self) -> None:
        from lead_generator.planning.models import DiscoveryResult

        class WeeklyScraper:
            def discover_ids(self, **kwargs):
                return DiscoveryResult(
                    authority="Example",
                    source_url="https://planning.example.gov.uk/weekly",
                    applications=[
                        PlanningApplication(
                            authority="Example",
                            uid="APP1",
                            url="https://planning.example.gov.uk/application/APP1",
                            reference="26/00001/FUL",
                            date_received="2026-06-30",
                            date_validated="2026-07-14",
                            raw={"detail_complete": True, "date_range_filtered": True},
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
            patch("lead_generator.planning.leads.planning_scraper_for_target", return_value=WeeklyScraper()),
            patch("lead_generator.planning.leads.discover_planit_applications", return_value=[]),
        ):
            applications = discover_portal_applications(target, date(2026, 7, 13), date(2026, 7, 19))

        self.assertEqual([application.reference for application in applications], ["26/00001/FUL"])

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

            enrichment_counts: list[tuple[int, int]] = []
            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=applications),
                patch(
                    "lead_generator.planning.leads.enrich_application_folder",
                    return_value=ContactEnrichment(
                        architect_company_names=["Example Architects Ltd"],
                        email_addresses=["studio@example-architects.co.uk"],
                    ),
                ),
            ):
                result = run_lead_search(
                    config,
                    enrichment_progress=lambda completed, total: enrichment_counts.append(
                        (completed, total)
                    ),
                )

            self.assertEqual(result.leads_found, 1)
            self.assertTrue(result.csv_path.exists())
            csv_text = result.csv_path.read_text(encoding="utf-8")
            self.assertTrue(csv_text.startswith("Reference,address,application link"))
            self.assertIn("application link", csv_text)
            self.assertIn("1 Example Street", csv_text)
            self.assertIn("https://planning.example.gov.uk/detail/ABC123", csv_text)
            self.assertIn("24/01234/FUL", csv_text)
            self.assertNotIn("24/99999/FUL", csv_text)
            self.assertIn("Architect / Company Name", csv_text)
            self.assertIn("Example Architects Ltd", csv_text)
            self.assertIn("studio@example-architects.co.uk", csv_text)
            self.assertEqual(enrichment_counts, [(0, 1), (1, 1)])
            self.assertTrue(result.failure_csv_path.exists())
            self.assertTrue((result.output_dir / "Example Council" / "24 01234 FUL").exists())
            self.assertTrue((result.output_dir / "selected_councils.geojson").exists())

    def test_run_lead_search_searches_all_councils_before_downloading_documents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Council A", "Council B"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            events: list[str] = []

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
                events.append(f"search:{target.authority}")
                suffix = target.authority[-1]
                return [
                    PlanningApplication(
                        authority=target.authority,
                        uid=f"APP-{suffix}",
                        url=f"https://planning.example.gov.uk/{suffix}",
                        reference=f"REF-{suffix}",
                        address="1 Example Street",
                        description="Install driveway gates",
                        date_received="2026-06-10",
                        raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
                    )
                ]

            def fake_document_discovery(
                application,
                *,
                should_cancel=None,
                defer_rate_limit=False,
            ):
                return DocumentDiscoveryResult(documents=[
                    PlanningDocument(
                        title=f"{application.reference}.pdf",
                        url=f"https://documents.example.gov.uk/{application.uid}.pdf",
                    )
                ])

            def fake_download(documents, destination, **kwargs):
                document = list(documents)[0]
                events.append(f"download:{Path(document.title).stem}")
                return DocumentDownloadBatchResult(downloaded_count=1)

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    side_effect=fake_document_discovery,
                ),
                patch("lead_generator.planning.leads._download_pdf_documents_once", side_effect=fake_download),
                patch("lead_generator.planning.leads.MAX_CONCURRENT_DOCUMENT_BATCHES", 1),
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                result = run_lead_search(config)

        self.assertEqual(result.leads_found, 2)
        self.assertEqual(
            events,
            ["search:Council A", "search:Council B", "download:REF-A", "download:REF-B"],
        )

    def test_run_lead_search_retries_temporary_404_at_final_queue_pass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Example Council"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            applications = [
                PlanningApplication(
                    authority="Example Council",
                    uid=f"APP-{index}",
                    url=f"https://planning.example.gov.uk/{index}",
                    reference=f"REF-{index}",
                    address=f"{index} Example Street",
                    description="Install driveway gates",
                    date_received="2026-06-10",
                    raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
                )
                for index in (1, 2)
            ]
            events: list[str] = []

            def fake_document_discovery(
                application,
                *,
                should_cancel=None,
                defer_rate_limit=False,
            ):
                return DocumentDiscoveryResult(documents=[
                    PlanningDocument(
                        title=f"{application.reference}.pdf",
                        url=f"https://documents.example.gov.uk/{application.uid}.pdf",
                    )
                ])

            attempts: dict[str, int] = {}

            def fake_download(document, **kwargs):
                reference = Path(document.title).stem
                attempts[reference] = attempts.get(reference, 0) + 1
                events.append(
                    f"{'first' if attempts[reference] == 1 else 'retry'}:{reference}"
                )
                if reference == "REF-1" and attempts[reference] == 1:
                    raise HTTPError(document.url, 404, "Not Found", {}, None)
                return DownloadedFile(
                    payload=b"%PDF-1.4",
                    final_url=document.url,
                    content_type="application/pdf",
                )

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=applications),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    side_effect=fake_document_discovery,
                ),
                patch("lead_generator.planning.leads.download_document_file", side_effect=fake_download),
                patch("lead_generator.planning.leads._wait_for_document_retry_cooldown", return_value=True),
                patch("lead_generator.planning.leads.MAX_CONCURRENT_DOCUMENT_BATCHES", 1),
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                result = run_lead_search(config)

        self.assertEqual(events, ["first:REF-1", "first:REF-2", "retry:REF-1"])
        self.assertEqual(result.captured_documents, 2)

    def test_run_lead_search_rediscovers_partial_document_jobs_at_queue_end(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Example Council"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            application = PlanningApplication(
                authority="Example Council",
                uid="APP-1",
                url="https://planning.example.test/APP-1",
                reference="REF-1",
                address="1 Example Street",
                description="Install driveway gates",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
            )
            first = PlanningDocument(title="Proposed plan.pdf", url="https://docs.test/one.pdf")
            second = PlanningDocument(title="Proposed elevations.pdf", url="https://docs.test/two.pdf")
            discoveries = [
                DocumentDiscoveryResult(
                    documents=[first],
                    successful_sources=["https://docs.test/a"],
                    failed_sources=[DocumentSourceFailure("https://docs.test/b", "HTTP 503")],
                ),
                DocumentDiscoveryResult(
                    documents=[first, second],
                    successful_sources=["https://docs.test/a", "https://docs.test/b"],
                ),
            ]
            download_batches: list[list[str]] = []
            progress: list[tuple[int, int]] = []

            def fake_download(documents, destination, **kwargs):
                batch = list(documents)
                download_batches.append([document.url for document in batch])
                return DocumentDownloadBatchResult(downloaded_count=len(batch))

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=[application]),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    side_effect=discoveries,
                ) as discover_documents,
                patch("lead_generator.planning.leads._download_pdf_documents_once", side_effect=fake_download),
                patch("lead_generator.planning.leads._wait_for_document_retry_cooldown", return_value=True),
                patch("lead_generator.planning.leads.MAX_CONCURRENT_DOCUMENT_BATCHES", 1),
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                result = run_lead_search(
                    config,
                    document_progress=lambda done, total: progress.append((done, total)),
                )

        self.assertEqual(download_batches, [[first.url], [second.url]])
        self.assertEqual(discover_documents.call_count, 2)
        self.assertEqual(
            [
                call.kwargs["defer_rate_limit"]
                for call in discover_documents.call_args_list
            ],
            [True, False],
        )
        self.assertEqual(progress, [(0, 1), (1, 1)])
        self.assertEqual(result.captured_documents, 1)

    def test_run_lead_search_does_not_retry_confirmed_empty_document_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Example Council"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            application = PlanningApplication(
                authority="Example Council",
                uid="APP-1",
                url="https://planning.example.test/APP-1",
                reference="REF-1",
                address="1 Example Street",
                description="Install driveway gates",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
            )
            confirmed_empty = DocumentDiscoveryResult(
                successful_sources=["https://planning.example.test/APP-1"]
            )

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=[application]),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    return_value=confirmed_empty,
                ) as discover_documents,
                patch("lead_generator.planning.leads._download_pdf_documents_once") as download_documents,
                patch("lead_generator.planning.leads._wait_for_document_retry_cooldown") as wait_for_retry,
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                result = run_lead_search(config)

        discover_documents.assert_called_once()
        download_documents.assert_not_called()
        wait_for_retry.assert_not_called()
        self.assertEqual(result.failed_councils, [])

    def test_run_lead_search_reports_document_progress(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Example Council"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            application = PlanningApplication(
                authority="Example Council",
                uid="APP-1",
                url="https://planning.example.gov.uk/1",
                reference="REF-1",
                address="1 Example Street",
                description="Install driveway gates",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
            )
            document_progress: list[tuple[int, int]] = []

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=[application]),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    return_value=DocumentDiscoveryResult(),
                ),
                patch(
                    "lead_generator.planning.leads._download_pdf_documents_once",
                    return_value=DocumentDownloadBatchResult(),
                ),
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                run_lead_search(config, document_progress=lambda complete, total: document_progress.append((complete, total)))

        self.assertEqual(document_progress, [(0, 1), (1, 1)])

    def test_document_discovery_failure_does_not_discard_lead_or_fail_council(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            user_geojson, catalogue = write_search_fixture(root, ["Example Council"])
            config = LeadSearchConfig(
                geojson_path=user_geojson,
                output_root=root,
                start_date=date(2026, 6, 1),
                end_date=date(2026, 6, 30),
                keywords=["gates"],
                catalogue_path=catalogue,
                worker_count=1,
            )
            application = PlanningApplication(
                authority="Example Council",
                uid="APP-1",
                url="https://planning.example.gov.uk/1",
                reference="REF-1",
                address="1 Example Street",
                description="Install driveway gates",
                date_received="2026-06-10",
                raw={"location": {"type": "Point", "coordinates": [0.5, 0.5]}},
            )

            with (
                patch("lead_generator.planning.leads.discover_portal_applications", return_value=[application]),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    return_value=DocumentDiscoveryResult(
                        failed_sources=[
                            DocumentSourceFailure(
                                application.url,
                                "document portal timed out",
                            )
                        ]
                    ),
                ),
                patch("lead_generator.planning.leads._wait_for_document_retry_cooldown", return_value=True),
                patch("lead_generator.planning.leads.enrich_application_folder", return_value=ContactEnrichment()),
            ):
                result = run_lead_search(config)

        self.assertEqual(result.leads_found, 1)
        self.assertEqual(result.failed_councils, [])
        self.assertEqual(result.completion, "Completed")

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
                patch("lead_generator.planning.leads.discover_application_documents", side_effect=AssertionError("Documents should not be discovered")),
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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

            def fake_document_discovery(
                application,
                *,
                should_cancel=None,
                defer_rate_limit=False,
            ):
                return DocumentDiscoveryResult(documents=[
                    PlanningDocument(
                        title="Proposed plan.pdf",
                        url="https://applications.example.gov.uk/document/proposed.pdf",
                    )
                ])

            captured_counts: list[int] = []
            with (
                patch("lead_generator.planning.leads.discover_portal_applications", side_effect=fake_discover),
                patch(
                    "lead_generator.planning.leads.discover_application_documents",
                    side_effect=fake_document_discovery,
                ),
                patch(
                    "lead_generator.planning.leads._download_pdf_documents_once",
                    return_value=DocumentDownloadBatchResult(downloaded_count=1),
                ),
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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

    def test_council_deadline_propagates_attempt_cancellation_to_discovery(self) -> None:
        target = CouncilTarget(
            authority="Cancelable Council",
            portal_family="custom",
            scraper_type="Custom",
            base_url="https://planning.example.gov.uk",
            listing_url="https://planning.example.gov.uk/search",
            geometry={},
        )
        received_callbacks = []

        def discover(*args, should_cancel=None, **kwargs):
            received_callbacks.append(should_cancel)
            return []

        with patch(
            "lead_generator.planning.leads.discover_portal_applications",
            side_effect=discover,
        ):
            applications = discover_portal_applications_with_deadline(
                target,
                date(2026, 7, 6),
                date(2026, 7, 12),
            )

        self.assertEqual(applications, [])
        self.assertEqual(len(received_callbacks), 1)
        self.assertTrue(callable(received_callbacks[0]))

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

            def fake_discover(target, start_date, end_date, *, should_cancel=None):
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

        fetch_documents.assert_called_once()
        self.assertEqual(
            fetch_documents.call_args.args[0],
            "https://planning.bcpcouncil.gov.uk/Planning/Display/P/26/02835/HOU",
        )
        self.assertEqual(enriched.documents, documents)

    def test_camden_document_source_uses_camdocs_reference_lookup(self) -> None:
        application = PlanningApplication(
            authority="Camden",
            uid="2026/2898/P",
            reference="2026/2898/P",
            url="https://opendata.camden.gov.uk/resource/2eiu-s2cw.json",
        )
        expected = "https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec?" + urlencode(
            {"q": 'recContainer:"2026/2898/P"'}
        )

        self.assertIn(expected, planit_document_source_urls(application))

    def test_exeter_document_source_uses_related_documents_lookup(self) -> None:
        application = PlanningApplication(
            authority="Exeter",
            uid="26/1049/FUL",
            reference="26/1049/FUL",
            url="https://exeter.gov.uk/planning-services/permissions-and-applications/",
        )
        expected = (
            "https://exeter.gov.uk/planning-services/permissions-and-applications/"
            "related-documents?" + urlencode({"appref": "26/1049/FUL"})
        )

        self.assertIn(expected, planit_document_source_urls(application))

    def test_bath_document_source_uses_active_application_host(self) -> None:
        application = PlanningApplication(
            authority="Bath and North East Somerset",
            uid="26/1049/FUL",
            reference="26/1049/FUL",
            url="https://planning.bathnes.gov.uk/application/26-1049-FUL",
            raw={"portal_family": "bath_planning_api"},
        )

        self.assertIn(
            "https://planning.bathnes.gov.uk/planningdocuments=26%2F1049%2FFUL",
            planit_document_source_urls(application),
        )

    def test_iter_document_links_skips_missing_derived_title_and_keeps_valid_link(self) -> None:
        page_url = "https://planning.example.gov.uk/application/ABC123"
        markup = html.fromstring(
            """
            <html><body>
              <a href="/documents/download?id=missing-title"></a>
              <a href="/documents/download?id=proposed-plan">Proposed Site Plan</a>
            </body></html>
            """
        )

        with patch(
            "lead_generator.planning.leads.document_title_from_url",
            side_effect=[None, "Proposed Site Plan.pdf"],
        ):
            documents = list(iter_document_links(markup, page_url))

        self.assertEqual(
            documents,
            [("/documents/download?id=proposed-plan", "Proposed Site Plan")],
        )
        self.assertFalse(_is_document_link_text(None, "/documents"))

    def test_associated_document_source_reads_wandsworth_link_once(self) -> None:
        page_url = "https://planning2.wandsworth.gov.uk/planningcase/CaseDetails.aspx?case=2026/2589"
        markup = """
            <html><body>
              <a href="/planningcase/comments.aspx?case=2026/2589">
                View Associated Application Documents
              </a>
              <a href="/planningcase/comments.aspx?case=2026/2589">
                View Associated Application Documents
              </a>
            </body></html>
        """

        self.assertEqual(
            _associated_document_source_urls(markup, page_url),
            ["https://planning2.wandsworth.gov.uk/planningcase/comments.aspx?case=2026/2589"],
        )

    def test_associated_document_source_skips_empty_label_and_keeps_later_link(self) -> None:
        page_url = "https://planning.example.test/application/ABC123"
        markup = """
            <html><body>
              <a href="/documents/empty"></a>
              <a href="/documents/related">View Related Documents</a>
            </body></html>
        """

        self.assertEqual(
            _associated_document_source_urls(markup, page_url),
            ["https://planning.example.test/documents/related"],
        )

    def test_associated_document_labels_are_source_only(self) -> None:
        page_url = "https://planning.example.test/application/ABC123"
        for label in (
            "Click to view associated documents",
            "View associated documents",
            "Associated documents",
        ):
            with self.subTest(label=label):
                markup = f"""
                    <html><body>
                      <a href="/planning/planning-documents?reference=ABC123">{label}</a>
                      <a href="/files/proposed-elevations.pdf">Proposed elevations</a>
                    </body></html>
                """

                self.assertEqual(
                    _associated_document_source_urls(markup, page_url),
                    [
                        "https://planning.example.test/planning/"
                        "planning-documents?reference=ABC123"
                    ],
                )
                self.assertEqual(
                    list(iter_document_links(html.fromstring(markup), page_url)),
                    [("/files/proposed-elevations.pdf", "Proposed elevations")],
                )

    def test_suffixed_associated_document_label_is_source_only(self) -> None:
        page_url = "https://planning.example.test/application/ABC123"
        markup = """
            <html><body>
              <a href="/planning/planning-documents?reference=ABC123">
                Click to view associated documents (opens in new window)
              </a>
              <a href="/files/proposed-elevations.pdf">Proposed elevations</a>
            </body></html>
        """

        self.assertEqual(
            _associated_document_source_urls(markup, page_url),
            [
                "https://planning.example.test/planning/"
                "planning-documents?reference=ABC123"
            ],
        )
        self.assertEqual(
            list(iter_document_links(html.fromstring(markup), page_url)),
            [("/files/proposed-elevations.pdf", "Proposed elevations")],
        )

    def test_associated_document_sources_ignore_page_chrome(self) -> None:
        page_url = "https://planning.example.test/application/ABC123"
        markup = """
            <html><body>
              <nav><a href="/navigation/documents">Plans &amp; Documents</a></nav>
              <main><a href="/application/documents">Plans &amp; Documents</a></main>
            </body></html>
        """

        self.assertEqual(
            _associated_document_source_urls(markup, page_url),
            ["https://planning.example.test/application/documents"],
        )

    def test_document_links_exclude_breadcrumb_tab_navigation_and_footer_pdf(self) -> None:
        page_url = (
            "https://planning.example.gov.uk/online-applications/"
            "applicationDetails.do?keyVal=ABC123"
        )
        markup = html.fromstring(
            """
            <html><body>
              <div class="breadcrumb">
                <a href="/related-documents/Related%20Documents">Related Documents</a>
              </div>
              <nav>
                <a href="applicationDetails.do?activeTab=externalDocuments&amp;keyVal=ABC123">
                  Plans &amp; Documents
                </a>
              </nav>
              <main>
                <a href="/files/proposed-elevations.pdf">Proposed elevations</a>
              </main>
              <footer>
                <a href="https://www.westminster.gov.uk/pay-gap.pdf">Pay Gap PDF</a>
              </footer>
            </body></html>
            """
        )

        self.assertEqual(
            list(iter_document_links(markup, page_url)),
            [("/files/proposed-elevations.pdf", "Proposed elevations")],
        )

    def test_document_links_filter_resolved_exeter_navigation_url(self) -> None:
        page_url = (
            "https://exeter.gov.uk/planning-services/permissions-and-applications/"
            "related-documents/?appref=26%2F1049%2FFUL"
        )
        markup = html.fromstring(
            """
            <html><body>
              <a href="Related Documents">MyExeter</a>
              <a href="26_1049_FUL-Proposed_Plan.pdf">Proposed plan</a>
            </body></html>
            """
        )

        self.assertEqual(
            list(iter_document_links(markup, page_url)),
            [("26_1049_FUL-Proposed_Plan.pdf", "Proposed plan")],
        )

    def test_associated_document_chain_is_bounded_and_does_not_loop(self) -> None:
        root_url = "https://planning.example.gov.uk/summary/ABC123"
        plans_url = (
            "https://planning.example.gov.uk/online-applications/"
            "applicationDetails.do?activeTab=externalDocuments&keyVal=ABC123"
        )
        associated_url = "https://planning.example.gov.uk/associated/ABC123"
        pages = {
            root_url: (
                '<a href="/online-applications/applicationDetails.do?'
                'activeTab=externalDocuments&amp;keyVal=ABC123">Plans &amp; Documents</a>'
            ),
            plans_url: '<a href="/associated/ABC123">view associated documents</a>',
            associated_url: (
                '<a href="/files/proposed-plan.pdf">Proposed plan</a>'
                '<a href="/summary/ABC123">Plans &amp; Documents</a>'
            ),
        }
        visits: list[str] = []

        def fake_fetch(
            url: str,
            *,
            timeout: float,
            defer_rate_limit: bool = False,
        ):
            visits.append(url)
            return pages[url], url, object()

        with patch(
            "lead_generator.planning.leads._fetch_html_document_page",
            side_effect=fake_fetch,
        ):
            documents = fetch_planit_documents(root_url)

        self.assertEqual(
            [(document.title, document.url) for document in documents],
            [("Proposed plan", "https://planning.example.gov.uk/files/proposed-plan.pdf")],
        )
        self.assertEqual(visits, [root_url, plans_url, associated_url])

    def test_tascomi_search_url_is_treated_as_listing_page(self) -> None:
        self.assertTrue(
            _looks_like_listing_url(
                "https://planning.example.test/planning/index.html?fa=search"
            )
        )

    def test_tascomi_browser_documents_reconcile_same_source_http_failure(self) -> None:
        application = PlanningApplication(
            authority="Waltham Forest",
            uid="261479",
            url="https://planning.example.test/planning/application details?id=261479",
            raw={"portal_family": "tascomi"},
        )
        normalized_source = "https://planning.example.test/planning/application%20details?id=261479"
        unrelated_source = "https://documents.example.test/secondary"
        document = PlanningDocument(
            title="Proposed elevations.pdf",
            url="https://planning.example.test/documents/proposed-elevations.pdf",
        )

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[normalized_source, unrelated_source],
            ),
            patch(
                "lead_generator.planning.leads.fetch_planit_documents",
                side_effect=[
                    HTTPError(normalized_source, 503, "Unavailable", {}, None),
                    HTTPError(unrelated_source, 503, "Unavailable", {}, None),
                ],
            ),
            patch(
                "lead_generator.planning.leads.fetch_browser_document_list",
                return_value=[document],
            ),
        ):
            result = discover_application_documents(application)

        self.assertEqual(result.documents, [document])
        self.assertEqual(
            [failure.source_url for failure in result.failed_sources],
            [unrelated_source],
        )
        self.assertIn(application.url, result.successful_sources)

    def test_tascomi_confirmed_empty_browser_reconciles_same_source_http_failure(self) -> None:
        application = PlanningApplication(
            authority="Rother",
            uid="RR/2026/0814/FULL",
            url="https://planning.example.test/planning/index.html?fa=getApplication&id=123",
            raw={"portal_family": "tascomi"},
        )

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[application.url],
            ),
            patch(
                "lead_generator.planning.leads.fetch_planit_documents",
                side_effect=HTTPError(application.url, 503, "Unavailable", {}, None),
            ),
            patch(
                "lead_generator.planning.leads.fetch_browser_document_list",
                return_value=[],
            ),
        ):
            result = discover_application_documents(application)

        self.assertEqual(result.documents, [])
        self.assertEqual(result.failed_sources, [])
        self.assertIn(application.url, result.successful_sources)

    def test_tascomi_rate_limit_deferral_cannot_become_successful_empty(self) -> None:
        application = PlanningApplication(
            authority="Rother",
            uid="RR/2026/0814/FULL",
            url="https://planning.example.test/planning/index.html?fa=getApplication&id=123",
            raw={"portal_family": "tascomi"},
        )

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[application.url],
            ),
            patch(
                "lead_generator.planning.leads.fetch_planit_documents",
                side_effect=leads_module._HostRateLimitDeferredError(
                    application.url
                ),
            ),
            patch(
                "lead_generator.planning.leads._request_cooldown_remaining_seconds",
                return_value=60.0,
            ),
            patch(
                "lead_generator.planning.leads.fetch_browser_document_list",
                return_value=[],
            ) as browser_fallback,
        ):
            result = discover_application_documents(
                application,
                defer_rate_limit=True,
            )

        browser_fallback.assert_not_called()
        self.assertEqual(result.documents, [])
        self.assertEqual(result.successful_sources, [])
        self.assertEqual(len(result.failed_sources), 1)
        self.assertEqual(result.failed_sources[0].source_url, application.url)

    def test_tascomi_browser_failure_retains_same_source_failure(self) -> None:
        application = PlanningApplication(
            authority="Rother",
            uid="RR/2026/0802/LBC",
            url="https://planning.example.test/planning/index.html?fa=getApplication&id=456",
            raw={"portal_family": "tascomi"},
        )

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[application.url],
            ),
            patch(
                "lead_generator.planning.leads.fetch_planit_documents",
                side_effect=HTTPError(application.url, 503, "Unavailable", {}, None),
            ),
            patch(
                "lead_generator.planning.leads.fetch_browser_document_list",
                side_effect=HTTPError(application.url, 503, "Unavailable", {}, None),
            ),
        ):
            result = discover_application_documents(application)

        self.assertGreaterEqual(len(result.failed_sources), 1)
        self.assertTrue(
            all(failure.source_url == application.url for failure in result.failed_sources)
        )
        self.assertEqual(result.successful_sources, [])

    def test_tascomi_browser_failure_is_retained_after_successful_http_empty(self) -> None:
        application = PlanningApplication(
            authority="Rother",
            uid="RR/2026/0802/LBC",
            url="https://planning.example.test/planning/index.html?fa=getApplication&id=456",
            raw={"portal_family": "tascomi"},
        )

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[application.url],
            ),
            patch("lead_generator.planning.leads.fetch_planit_documents", return_value=[]),
            patch(
                "lead_generator.planning.leads.fetch_browser_document_list",
                side_effect=HTTPError(application.url, 503, "Unavailable", {}, None),
            ),
        ):
            result = discover_application_documents(application)

        self.assertEqual(len(result.failed_sources), 1)
        self.assertEqual(result.failed_sources[0].source_url, application.url)

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

        def fake_fetch(url: str, **kwargs) -> list[PlanningDocument]:
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

    def test_discover_application_documents_keeps_partial_results(self) -> None:
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url="https://example.test/application/ABC123",
        )
        source_a = "https://example.test/documents-a"
        source_b = "https://example.test/documents-b"
        proposed_plan = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://example.test/proposed-plan.pdf",
        )
        unavailable = HTTPError(source_b, 503, "Unavailable", {}, None)

        with (
            patch(
                "lead_generator.planning.leads.application_document_source_urls",
                return_value=[source_a, source_b],
            ),
            patch(
                "lead_generator.planning.leads.fetch_planit_documents",
                side_effect=[[proposed_plan], unavailable],
            ),
        ):
            result = discover_application_documents(application)

        self.assertEqual([document.title for document in result.documents], ["Proposed plan.pdf"])
        self.assertEqual(result.successful_sources, [source_a])
        self.assertEqual(result.failed_sources[0].source_url, source_b)
        self.assertIn("503", result.failed_sources[0].reason)

    def test_download_pdf_documents_allows_existing_only_drawings(self) -> None:
        documents = [
            PlanningDocument(title="Existing elevations.pdf", url="https://example.test/existing-elevations.pdf"),
            PlanningDocument(title="Existing survey report.pdf", url="https://example.test/existing-survey.pdf"),
            PlanningDocument(title="Existing and proposed elevations.pdf", url="https://example.test/combined.pdf"),
            PlanningDocument(title="Viewer.exe", url="https://example.test/viewer.exe"),
        ]

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with patch(
                "lead_generator.planning.leads.download_document_file",
                return_value=DownloadedFile(
                    payload=b"%PDF-1.4",
                    final_url="https://example.test/file.pdf",
                    content_type="application/pdf",
                ),
            ) as download_file:
                downloaded = download_pdf_documents(documents, destination)

            self.assertEqual(downloaded, 2)
            self.assertEqual(
                [call.args[0].title for call in download_file.call_args_list],
                ["Existing elevations.pdf", "Existing and proposed elevations.pdf"],
            )
            self.assertTrue((destination / "Existing elevations.pdf").exists())
            self.assertFalse((destination / "Existing survey report.pdf").exists())
            self.assertFalse((destination / "Viewer.exe").exists())

    def test_download_pdf_documents_retries_temporary_404_once(self) -> None:
        document = PlanningDocument(
            title="Removed plan.pdf",
            url="https://planning.example.gov.uk/docs/removed-plan.pdf",
        )
        error = HTTPError(document.url, 404, "Not Found", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("lead_generator.planning.leads.download_document_file", side_effect=error) as download_file,
                patch("lead_generator.planning.leads.sleep") as wait,
            ):
                downloaded = download_pdf_documents([document], Path(directory))

        self.assertEqual(downloaded, 0)
        self.assertEqual(download_file.call_count, 2)
        self.assertTrue(all(call.args[0] is document for call in download_file.call_args_list))
        wait.assert_called_once()

    def test_document_batch_defers_first_pass_404_without_blocking_same_host(self) -> None:
        missing = PlanningDocument(
            title="Temporarily missing plan.pdf",
            url="https://planning.example.gov.uk/docs/missing-plan.pdf",
        )
        sibling = PlanningDocument(
            title="Available elevations.pdf",
            url="https://planning.example.gov.uk/docs/available-elevations.pdf",
        )

        def fake_download(document, **kwargs):
            if document is missing:
                raise HTTPError(document.url, 404, "Not Found", {}, None)
            return DownloadedFile(
                payload=b"%PDF-1.4",
                final_url=document.url,
                content_type="application/pdf",
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=fake_download,
            ) as download_file:
                result = _download_pdf_documents_once(
                    [missing, sibling],
                    Path(directory),
                )

        self.assertEqual(download_file.call_count, 2)
        self.assertEqual(result.downloaded_count, 1)
        self.assertEqual(result.transient_documents, [missing])
        self.assertEqual(result.failures, [])

    def test_download_pdf_documents_skip_source_page_when_direct_urls_work(self) -> None:
        source_url = (
            "https://planning.example.gov.uk/online-applications/"
            "applicationDetails.do?activeTab=documents&keyVal=ABC123"
        )
        documents = [
            PlanningDocument(
                title="Proposed plan.pdf",
                url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
                source_url=source_url,
            ),
            PlanningDocument(
                title="Proposed elevations.pdf",
                url="https://planning.example.gov.uk/docs/proposed-elevations.pdf",
                source_url=source_url,
            ),
        ]

        class FakeResponse:
            def __init__(self, url: str, payload: bytes, content_type: str) -> None:
                self.url = url
                self.payload = payload
                self.headers = {"Content-Type": content_type}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return self.payload

            def geturl(self) -> str:
                return self.url

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                url = request.full_url
                self.urls.append(url)
                if "activeTab=documents" in url:
                    return FakeResponse(
                        url,
                        b"""
                        <html><body>
                          <a href="/docs/proposed-plan.pdf">Proposed plan.pdf</a>
                          <a href="/docs/proposed-elevations.pdf">Proposed elevations.pdf</a>
                        </body></html>
                        """,
                        "text/html",
                    )
                return FakeResponse(url, b"%PDF-1.4", "application/pdf")

        with tempfile.TemporaryDirectory() as directory:
            opener = FakeOpener()
            with (
                patch("lead_generator.planning.leads._build_document_opener", return_value=opener),
                patch("lead_generator.planning.leads.sleep"),
            ):
                downloaded = download_pdf_documents(documents, Path(directory))

        self.assertEqual(downloaded, 2)
        self.assertEqual(opener.urls, [document.url for document in documents])

    def test_document_batch_defers_remaining_same_host_after_rate_limit(self) -> None:
        documents = [
            PlanningDocument(
                title=f"Plan {index}.pdf",
                url=f"https://planning.example.gov.uk/docs/plan-{index}.pdf",
            )
            for index in range(1, 4)
        ]
        rate_limit = HTTPError(documents[0].url, 503, "Unavailable", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=rate_limit,
            ) as download_file:
                result = _download_pdf_documents_once(documents, Path(directory))

        self.assertEqual(download_file.call_count, 1)
        self.assertEqual(result.downloaded_count, 0)
        self.assertEqual(result.transient_documents, documents)

    def test_large_rate_limited_host_batch_does_not_block_another_host(self) -> None:
        rate_limited = [
            PlanningDocument(
                title=f"Plan {index}.pdf",
                url=f"https://limited.example.gov.uk/docs/plan-{index}.pdf",
            )
            for index in range(1, 13)
        ]
        available = PlanningDocument(
            title="Available plan.pdf",
            url="https://available.example.gov.uk/docs/available-plan.pdf",
        )

        def download(document, **kwargs):
            if "limited.example.gov.uk" in document.url:
                raise HTTPError(document.url, 429, "Too Many Requests", {}, None)
            return DownloadedFile(
                payload=b"%PDF-available",
                final_url=document.url,
                content_type="application/pdf",
            )

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=download,
            ) as download_file:
                result = _download_pdf_documents_once(
                    [*rate_limited, available],
                    Path(directory),
                )

        self.assertEqual(download_file.call_count, 2)
        self.assertEqual(result.transient_documents, rate_limited)
        self.assertEqual(result.downloaded_count, 1)

    def test_real_first_pass_429_releases_slot_for_unrelated_host(self) -> None:
        limited_first = PlanningDocument(
            title="First limited plan.pdf",
            url="https://limited.example.gov.uk/docs/first.pdf",
        )
        limited_second = PlanningDocument(
            title="Second limited plan.pdf",
            url="https://limited.example.gov.uk/docs/second.pdf",
        )
        available = PlanningDocument(
            title="Available plan.pdf",
            url="https://available.example.gov.uk/docs/available.pdf",
        )
        cooldown_recorded = threading.Event()
        second_waiting = threading.Event()
        release_waits = threading.Event()
        available_started = threading.Event()
        results: dict[str, DocumentDownloadBatchResult] = {}

        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-available"

            def geturl(self) -> str:
                return self.url

        class HostAwareOpener:
            def open(self, request, timeout):
                if "limited.example.gov.uk" in request.full_url:
                    raise HTTPError(
                        request.full_url,
                        429,
                        "Too Many Requests",
                        {},
                        None,
                    )
                available_started.set()
                return FakeResponse(request.full_url)

        original_set_cooldown = leads_module._set_request_cooldown

        def record_cooldown(url: str, seconds: float) -> None:
            original_set_cooldown(url, seconds)
            cooldown_recorded.set()

        def controlled_wait(seconds: float, should_cancel) -> bool:
            if threading.current_thread().name == "limited-second":
                second_waiting.set()
            return release_waits.wait(timeout=2.0)

        def run_batch(name: str, document: PlanningDocument, destination: Path) -> None:
            results[name] = _download_pdf_documents_once(
                [document],
                destination,
                defer_transient=True,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destinations = {
                name: root / name
                for name in ("limited-first", "limited-second", "available")
            }
            for destination in destinations.values():
                destination.mkdir()
            with (
                patch(
                    "lead_generator.planning.leads._build_document_opener",
                    return_value=HostAwareOpener(),
                ),
                patch(
                    "lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL",
                    {},
                ),
                patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
                patch(
                    "lead_generator.planning.leads._set_request_cooldown",
                    side_effect=record_cooldown,
                ),
                patch(
                    "lead_generator.planning.leads._wait_for_cancelable_delay",
                    side_effect=controlled_wait,
                ),
            ):
                first = threading.Thread(
                    target=run_batch,
                    args=("limited-first", limited_first, destinations["limited-first"]),
                    name="limited-first",
                )
                second = threading.Thread(
                    target=run_batch,
                    args=("limited-second", limited_second, destinations["limited-second"]),
                    name="limited-second",
                )
                third = threading.Thread(
                    target=run_batch,
                    args=("available", available, destinations["available"]),
                    name="available",
                )
                first.start()
                self.assertTrue(cooldown_recorded.wait(timeout=1.0))
                second.start()
                second.join(timeout=1.0)
                self.assertFalse(second.is_alive())
                third.start()
                try:
                    self.assertTrue(available_started.wait(timeout=0.5))
                finally:
                    release_waits.set()
                    for worker in (first, second, third):
                        worker.join(timeout=2.0)

        self.assertEqual(results["limited-first"].transient_documents, [limited_first])
        self.assertEqual(results["limited-second"].transient_documents, [limited_second])
        self.assertEqual(results["available"].downloaded_count, 1)
        self.assertFalse(second_waiting.is_set())

    def test_two_precooled_same_host_jobs_do_not_occupy_both_batch_slots(self) -> None:
        limited_documents = [
            PlanningDocument(
                title=f"Limited plan {index}.pdf",
                url=f"https://limited.example.gov.uk/docs/plan-{index}.pdf",
            )
            for index in range(1, 3)
        ]
        available = PlanningDocument(
            title="Available plan.pdf",
            url="https://available.example.gov.uk/docs/available.pdf",
        )
        release_waits = threading.Event()
        available_started = threading.Event()
        ready_for_available = threading.Event()
        limited_finished = 0
        state_lock = threading.Lock()
        results: dict[str, DocumentDownloadBatchResult] = {}

        class TrackingGate:
            def __init__(self) -> None:
                self._semaphore = threading.BoundedSemaphore(2)
                self._lock = threading.Lock()
                self._active = 0

            def __enter__(self):
                self._semaphore.acquire()
                with self._lock:
                    self._active += 1
                    if self._active == 2:
                        ready_for_available.set()
                return self

            def __exit__(self, *args):
                with self._lock:
                    self._active -= 1
                self._semaphore.release()

        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-available"

            def geturl(self) -> str:
                return self.url

        class HostAwareOpener:
            def open(self, request, timeout):
                if "limited.example.gov.uk" in request.full_url:
                    raise HTTPError(
                        request.full_url,
                        429,
                        "Too Many Requests",
                        {},
                        None,
                    )
                available_started.set()
                return FakeResponse(request.full_url)

        def controlled_wait(seconds: float, should_cancel) -> bool:
            return release_waits.wait(timeout=2.0)

        def run_limited(name: str, document: PlanningDocument, destination: Path) -> None:
            nonlocal limited_finished
            results[name] = _download_pdf_documents_once(
                [document],
                destination,
                defer_transient=True,
            )
            with state_lock:
                limited_finished += 1
                if limited_finished == 2:
                    ready_for_available.set()

        def run_available(destination: Path) -> None:
            results["available"] = _download_pdf_documents_once(
                [available],
                destination,
                defer_transient=True,
            )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destinations = {
                name: root / name
                for name in ("limited-1", "limited-2", "available")
            }
            for destination in destinations.values():
                destination.mkdir()
            cooldowns = {
                "limited.example.gov.uk": leads_module.monotonic() + 60.0,
            }
            with (
                patch(
                    "lead_generator.planning.leads._build_document_opener",
                    return_value=HostAwareOpener(),
                ),
                patch(
                    "lead_generator.planning.leads._DOCUMENT_DOWNLOAD_GATE",
                    TrackingGate(),
                ),
                patch(
                    "lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL",
                    cooldowns,
                ),
                patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
                patch(
                    "lead_generator.planning.leads._wait_for_cancelable_delay",
                    side_effect=controlled_wait,
                ),
            ):
                limited_workers = [
                    threading.Thread(
                        target=run_limited,
                        args=(
                            f"limited-{index}",
                            document,
                            destinations[f"limited-{index}"],
                        ),
                    )
                    for index, document in enumerate(limited_documents, start=1)
                ]
                for worker in limited_workers:
                    worker.start()
                self.assertTrue(ready_for_available.wait(timeout=1.0))
                available_worker = threading.Thread(
                    target=run_available,
                    args=(destinations["available"],),
                )
                available_worker.start()
                try:
                    self.assertTrue(available_started.wait(timeout=0.25))
                finally:
                    release_waits.set()
                    for worker in (*limited_workers, available_worker):
                        worker.join(timeout=2.0)

        self.assertEqual(
            results["limited-1"].transient_documents,
            [limited_documents[0]],
        )
        self.assertEqual(
            results["limited-2"].transient_documents,
            [limited_documents[1]],
        )
        self.assertEqual(results["available"].downloaded_count, 1)

    def test_document_batch_reuses_identical_payload_in_same_folder(self) -> None:
        documents = [
            PlanningDocument(
                title="Proposed plan.pdf",
                url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
            ),
            PlanningDocument(
                title="Proposed plan duplicate.pdf",
                url="https://planning.example.gov.uk/docs/proposed-plan-copy.pdf",
            ),
        ]

        def download(document, **kwargs):
            return DownloadedFile(
                payload=b"%PDF-identical",
                final_url=document.url,
                content_type="application/pdf",
            )

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=download,
            ):
                result = _download_pdf_documents_once(documents, destination)

            self.assertEqual(result.downloaded_count, 2)
            files = list(destination.iterdir())
            self.assertEqual(len(files), 1)
            self.assertEqual(files[0].name, "Proposed plan.pdf")
            self.assertEqual(files[0].read_bytes(), b"%PDF-identical")

    def test_document_batch_counts_preexisting_identical_payload_without_copying(self) -> None:
        document = PlanningDocument(
            title="Renamed proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            existing = destination / "Previously captured.pdf"
            existing.write_bytes(b"%PDF-identical")
            downloaded = DownloadedFile(
                payload=b"%PDF-identical",
                final_url=document.url,
                content_type="application/pdf",
            )
            with patch(
                "lead_generator.planning.leads.download_document_file",
                return_value=downloaded,
            ):
                result = _download_pdf_documents_once([document], destination)

            self.assertEqual(result.downloaded_count, 1)
            self.assertEqual(list(destination.iterdir()), [existing])

    def test_identical_payloads_are_preserved_in_different_application_folders(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )
        downloaded = DownloadedFile(
            payload=b"%PDF-identical",
            final_url=document.url,
            content_type="application/pdf",
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = root / "REF-1"
            second = root / "REF-2"
            first.mkdir()
            second.mkdir()
            with patch(
                "lead_generator.planning.leads.download_document_file",
                return_value=downloaded,
            ):
                first_result = _download_pdf_documents_once([document], first)
                second_result = _download_pdf_documents_once([document], second)

            self.assertEqual(first_result.downloaded_count, 1)
            self.assertEqual(second_result.downloaded_count, 1)
            self.assertEqual(len(list(first.iterdir())), 1)
            self.assertEqual(len(list(second.iterdir())), 1)

    def test_document_batch_reports_permanent_and_final_transient_failures(self) -> None:
        documents = [
            PlanningDocument(
                title="Missing plan.pdf",
                url="https://documents.example.gov.uk/missing.pdf",
            ),
            PlanningDocument(
                title="Rate limited plan.pdf",
                url="https://planning.example.gov.uk/docs/rate-limited.pdf",
            ),
            PlanningDocument(
                title="Blocked sibling plan.pdf",
                url="https://planning.example.gov.uk/docs/blocked-sibling.pdf",
            ),
        ]

        def fail_download(document, **kwargs):
            code = 404 if "missing" in document.url else 503
            raise HTTPError(document.url, code, "Unavailable", {}, None)

        with tempfile.TemporaryDirectory() as directory:
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=fail_download,
            ) as download_file:
                result = _download_pdf_documents_once(
                    documents,
                    Path(directory),
                    defer_transient=False,
                )

        self.assertEqual(download_file.call_count, 2)
        self.assertEqual(result.transient_documents, [])
        self.assertEqual(
            [(failure.document, failure.reason) for failure in result.failures],
            [
                (documents[0], "HTTP 404"),
                (documents[1], "HTTP 503"),
                (documents[2], "portal remained unavailable"),
            ],
        )

    def test_document_batch_does_not_block_host_after_optional_source_failure(self) -> None:
        source_url = "https://planning.example.gov.uk/pr/s/detail/a0iABC"
        documents = [
            PlanningDocument(
                title="Stale plan.pdf",
                url="https://planning.example.gov.uk/docs/stale-plan.pdf",
                source_url=source_url,
            ),
            PlanningDocument(
                title="Current elevations.pdf",
                url="https://planning.example.gov.uk/docs/current-elevations.pdf",
                source_url=source_url,
            ),
        ]

        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

            def geturl(self) -> str:
                return self.url

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if request.full_url == documents[0].url:
                    not_found = HTTPError(request.full_url, 404, "Not Found", {}, None)
                    not_found.close()
                    raise not_found
                return FakeResponse(request.full_url)

        opener = FakeOpener()
        with tempfile.TemporaryDirectory() as directory:
            with (
                patch("lead_generator.planning.leads._build_document_opener", return_value=opener),
                patch(
                    "lead_generator.planning.leads._fetch_html_with_portal_session",
                    return_value=("<html></html>", source_url),
                ),
                patch(
                    "lead_generator.planning.leads.fetch_publisher_document_list",
                    side_effect=RuntimeError("optional source failed"),
                ),
                patch("lead_generator.planning.leads.fetch_enterprise_document_list", return_value=[]),
                patch("lead_generator.planning.leads.fetch_arcus_salesforce_document_list", return_value=[]),
                patch("lead_generator.planning.leads.fetch_arcus_public_register_file_list", return_value=[]),
                patch("lead_generator.planning.leads.fetch_arcus_files_public_document_list", return_value=[]),
            ):
                result = _download_pdf_documents_once(
                    documents,
                    Path(directory),
                    defer_transient=False,
                )

        self.assertEqual(result.downloaded_count, 1)
        self.assertEqual(
            [(failure.document, failure.reason) for failure in result.failures],
            [(documents[0], "HTTP 404")],
        )
        self.assertIn(documents[1].url, opener.urls)

    def test_document_rate_limit_backoff_stops_when_cancelled(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )

        class RateLimitedOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, 503, "Unavailable", {}, None)

        cancel_checks = 0

        def should_cancel() -> bool:
            nonlocal cancel_checks
            cancel_checks += 1
            return cancel_checks >= 2

        opener = RateLimitedOpener()
        with (
            patch("lead_generator.planning.leads._throttle_request"),
            patch("lead_generator.planning.leads.sleep") as wait,
        ):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                download_document_file(
                    document,
                    opener=opener,
                    should_cancel=should_cancel,
                )

        self.assertEqual(opener.calls, 1)
        wait.assert_not_called()

    def test_document_429_uses_conservative_default_and_retains_final_host_cooldown(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )

        class RateLimitedOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        opener = RateLimitedOpener()
        with (
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                return_value=True,
            ) as wait,
            patch("lead_generator.planning.leads._set_request_cooldown") as set_cooldown,
        ):
            with self.assertRaises(HTTPError):
                download_document_file(document, opener=opener)

        self.assertEqual(opener.calls, 2)
        self.assertEqual(
            [entry.args for entry in set_cooldown.call_args_list],
            [(document.url, 60.0), (document.url, 60.0)],
        )
        wait.assert_called_once_with(60.0, None)

    def test_source_page_429_uses_rate_limit_policy_and_keeps_final_cooldown(self) -> None:
        url = "https://planning.example.gov.uk/documents/ABC123"
        request = Request(url)

        class RateLimitedOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

        opener = RateLimitedOpener()
        with (
            patch("lead_generator.planning.leads.sleep"),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                return_value=True,
            ) as wait,
            patch("lead_generator.planning.leads._set_request_cooldown") as set_cooldown,
        ):
            with self.assertRaises(HTTPError):
                _open_url_with_retry(request, timeout=30, opener=opener)

        self.assertEqual(opener.calls, 4)
        self.assertEqual(
            [entry.args for entry in set_cooldown.call_args_list],
            [(url, 60.0)] * 4,
        )
        self.assertEqual(
            [entry.args for entry in wait.call_args_list],
            [(60.0, None)] * 3,
        )

    def test_first_pass_source_page_429_defers_without_waiting(self) -> None:
        source_url = "https://planning.example.gov.uk/documents/ABC123"
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url=source_url,
            reference="ABC123",
            raw={"docs_url": source_url},
        )

        class RateLimitedOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "120"},
                    None,
                )

        opener = RateLimitedOpener()
        with (
            patch(
                "lead_generator.planning.leads._build_document_opener",
                return_value=opener,
            ),
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", {}),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
            ) as wait,
        ):
            discovery = discover_application_documents(
                application,
                defer_rate_limit=True,
            )

        self.assertEqual(opener.calls, 1)
        self.assertEqual(len(discovery.failed_sources), 1)
        self.assertIn("HTTP Error 429", discovery.failed_sources[0].reason)
        wait.assert_not_called()

    def test_document_discovery_rate_limit_wait_propagates_cancellation(self) -> None:
        source_url = "https://planning.example.gov.uk/documents/ABC123"
        application = PlanningApplication(
            authority="Example",
            uid="ABC123",
            url=source_url,
            reference="ABC123",
            raw={"docs_url": source_url},
        )
        cancel_checks = 0

        def should_cancel() -> bool:
            nonlocal cancel_checks
            cancel_checks += 1
            return cancel_checks >= 2

        class RateLimitedOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "120"},
                    None,
                )

        with (
            patch(
                "lead_generator.planning.leads._build_document_opener",
                return_value=RateLimitedOpener(),
            ),
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", {}),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                wraps=leads_module._wait_for_cancelable_delay,
            ) as wait,
        ):
            with self.assertRaises(DocumentDownloadCancelledError):
                discover_application_documents(
                    application,
                    should_cancel=should_cancel,
                )

        wait.assert_called_once_with(120.0, should_cancel)

    def test_document_429_retry_after_is_bounded_at_120_seconds(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )

        class EventuallyAvailableOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(
                        request.full_url,
                        429,
                        "Too Many Requests",
                        {"Retry-After": "999"},
                        None,
                    )
                return type(
                    "Response",
                    (),
                    {
                        "headers": {"Content-Type": "application/pdf"},
                        "__enter__": lambda self: self,
                        "__exit__": lambda self, *args: None,
                        "read": lambda self: b"%PDF-1.4",
                    },
                )()

        opener = EventuallyAvailableOpener()
        with (
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                return_value=True,
            ) as wait,
            patch("lead_generator.planning.leads._set_request_cooldown"),
        ):
            payload = download_document_file(document, opener=opener).payload

        self.assertEqual(payload, b"%PDF-1.4")
        wait.assert_called_once_with(120.0, None)

    def test_final_pass_retained_cooldown_wait_is_bounded_and_cancelable(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )

        class RateLimitedOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "999"},
                    None,
                )

        opener = RateLimitedOpener()
        cancel_checks = 0

        def should_cancel() -> bool:
            nonlocal cancel_checks
            cancel_checks += 1
            return cancel_checks >= 3
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "lead_generator.planning.leads._build_document_opener",
                return_value=opener,
            ),
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", {}),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                wraps=leads_module._wait_for_cancelable_delay,
            ) as wait,
        ):
            first_pass = _download_pdf_documents_once(
                [document],
                Path(directory),
                defer_transient=True,
            )
            final_pass = _download_pdf_documents_once(
                first_pass.transient_documents,
                Path(directory),
                should_cancel=should_cancel,
                defer_transient=False,
            )

        self.assertEqual(opener.calls, 1)
        self.assertEqual(first_pass.transient_documents, [document])
        self.assertEqual(final_pass.downloaded_count, 0)
        self.assertEqual(wait.call_count, 1)
        waited_seconds, waited_callback = wait.call_args.args
        self.assertLessEqual(waited_seconds, 120.0)
        self.assertIs(waited_callback, should_cancel)

    def test_overlapping_retry_cannot_remove_newer_host_cooldown(self) -> None:
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
        )
        older_waiting = threading.Event()
        release_older = threading.Event()
        older_result: list[DownloadedFile] = []
        older_errors: list[Exception] = []

        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

        class OlderOpener:
            def __init__(self) -> None:
                self.calls = 0

            def open(self, request, timeout):
                self.calls += 1
                if self.calls == 1:
                    raise HTTPError(
                        request.full_url,
                        429,
                        "Too Many Requests",
                        {"Retry-After": "60"},
                        None,
                    )
                return FakeResponse()

        class NewerOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "120"},
                    None,
                )

        def hold_older_wait(seconds: float, should_cancel) -> bool:
            older_waiting.set()
            return release_older.wait(timeout=2.0)

        def run_older() -> None:
            try:
                older_result.append(
                    _download_document_file(
                        document,
                        opener=OlderOpener(),
                        defer_rate_limit=False,
                    )
                )
            except Exception as exc:
                older_errors.append(exc)

        cooldowns: dict[str, float] = {}
        with (
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", cooldowns),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                side_effect=hold_older_wait,
            ),
            patch(
                "lead_generator.planning.leads.monotonic",
                side_effect=[100.0, 101.0, 102.0],
            ),
        ):
            older = threading.Thread(target=run_older)
            older.start()
            self.assertTrue(older_waiting.wait(timeout=1.0))
            with self.assertRaises(HTTPError):
                _download_document_file(
                    document,
                    opener=NewerOpener(),
                    defer_rate_limit=True,
                )
            release_older.set()
            older.join(timeout=2.0)

        self.assertEqual(older_errors, [])
        self.assertEqual(len(older_result), 1)
        self.assertEqual(cooldowns["planning.example.gov.uk"], 221.0)

    def test_browser_rate_limits_defer_host_without_first_pass_wait(self) -> None:
        documents = [
            PlanningDocument(
                title=f"Browser plan {index}.pdf",
                url=f"https://planning.example.gov.uk/index.html?fa=downloaddocument&id={index}",
                source_url="https://planning.example.gov.uk/index.html?fa=getApplication&id=1",
            )
            for index in range(1, 3)
        ]

        for status in (429, 503):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                with leads_module._REQUEST_THROTTLE_LOCK:
                    leads_module._LAST_REQUEST_AT.clear()
                    leads_module._REQUEST_COOLDOWN_UNTIL.clear()

                class RateLimitedBrowser:
                    def __init__(self, timeout_seconds):
                        pass

                    def get(self, url):
                        return object()

                    def get_bytes(self, url):
                        raise CouncilFetchError(f"HTTP {status} while fetching {url}")

                    def close(self):
                        pass

                with (
                    patch(
                        "lead_generator.planning.leads.CouncilBrowserClient",
                        RateLimitedBrowser,
                    ),
                    patch(
                        "lead_generator.planning.leads._set_request_cooldown",
                    ) as set_cooldown,
                    patch(
                        "lead_generator.planning.leads._wait_for_cancelable_delay",
                    ) as wait,
                ):
                    result = _download_pdf_documents_once(
                        documents,
                        Path(directory),
                        defer_transient=True,
                    )

                self.assertEqual(result.transient_documents, documents)
                wait.assert_not_called()
                if status == 429:
                    set_cooldown.assert_called_once_with(documents[0].url, 60.0)
                else:
                    set_cooldown.assert_not_called()

    def test_final_browser_retry_waits_for_retained_cooldown_cancelably(self) -> None:
        document = PlanningDocument(
            title="Browser plan.pdf",
            url="https://planning.example.gov.uk/index.html?fa=downloaddocument&id=1",
            source_url="https://planning.example.gov.uk/index.html?fa=getApplication&id=1",
        )
        browser_created = False

        class UnexpectedBrowser:
            def __init__(self, timeout_seconds):
                nonlocal browser_created
                browser_created = True

            def close(self):
                pass

        should_cancel = lambda: False
        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "lead_generator.planning.leads.CouncilBrowserClient",
                UnexpectedBrowser,
            ),
            patch(
                "lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL",
                {"planning.example.gov.uk": leads_module.monotonic() + 120.0},
            ),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                return_value=False,
            ) as wait,
        ):
            result = _download_pdf_documents_once(
                [document],
                Path(directory),
                should_cancel=should_cancel,
                defer_transient=False,
            )

        self.assertFalse(browser_created)
        self.assertEqual(result.downloaded_count, 0)
        wait.assert_called_once()
        waited_seconds, waited_callback = wait.call_args.args
        self.assertLessEqual(waited_seconds, 120.0)
        self.assertIs(waited_callback, should_cancel)

    def test_throttle_rechecks_cooldown_installed_during_spacing_wait(self) -> None:
        url = "https://planning.example.gov.uk/docs/proposed-plan.pdf"
        cooldowns: dict[str, float] = {}

        def install_cooldown(seconds: float, should_cancel) -> bool:
            cooldowns["planning.example.gov.uk"] = 200.0
            return True

        with (
            patch(
                "lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL",
                cooldowns,
            ),
            patch(
                "lead_generator.planning.leads._LAST_REQUEST_AT",
                {"planning.example.gov.uk": 0.0},
            ),
            patch(
                "lead_generator.planning.leads.monotonic",
                side_effect=[0.0, 0.25],
            ),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                side_effect=install_cooldown,
            ),
        ):
            with self.assertRaises(leads_module._HostRateLimitDeferredError):
                _throttle_request(url, defer_rate_limit=True)

    def test_browser_source_rate_limit_cools_source_host(self) -> None:
        document = PlanningDocument(
            title="Browser plan.pdf",
            url="https://files.example-cdn.test/index.html?fa=downloaddocument&id=1",
            source_url="https://planning.example.gov.uk/index.html?fa=getApplication&id=1",
        )

        class SourceLimitedBrowser:
            def __init__(self, timeout_seconds):
                pass

            def get(self, url):
                raise CouncilFetchError(f"HTTP 429 while fetching {url}")

            def close(self):
                pass

        with (
            tempfile.TemporaryDirectory() as directory,
            patch(
                "lead_generator.planning.leads.CouncilBrowserClient",
                SourceLimitedBrowser,
            ),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._set_request_cooldown",
            ) as set_cooldown,
        ):
            result = _download_pdf_documents_once(
                [document],
                Path(directory),
                defer_transient=True,
            )

        self.assertEqual(result.transient_documents, [document])
        set_cooldown.assert_called_once_with(document.source_url, 60.0)

    def test_host_cooldown_wait_is_cancelable(self) -> None:
        url = "https://planning.example.gov.uk/docs/proposed-plan.pdf"
        with (
            patch(
                "lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL",
                {"planning.example.gov.uk": 100.0},
            ),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch("lead_generator.planning.leads.monotonic", return_value=0.0),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                return_value=False,
            ) as wait,
        ):
            with self.assertRaises(DocumentDownloadCancelledError):
                _throttle_request(url, should_cancel=lambda: True)

        wait.assert_called_once_with(100.0, unittest.mock.ANY)

    def test_fetch_atrium_document_list_builds_binary_document_requests(self) -> None:
        page = """
        <table><tr data-module="PLA" data-recordnumber="25895"
          data-planid="370930.0000" data-imageid="5.0000"
          data-storedindatabase="False"
          data-filename="\\\\server\\docs\\ApplicationFormRedacted.pdf">
          <td><button class="viewDocument">View</button></td>
        </tr></table>
        """

        documents = fetch_atrium_document_list(
            page,
            "https://planning.example.gov.uk/Planning/Display?applicationNumber=26/00001/FUL",
        )

        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0].title, "ApplicationFormRedacted.pdf")
        self.assertIn("/Document/GetFileBinary?", documents[0].url)
        self.assertIn("planID=370930", documents[0].url)
        self.assertIn("isPlan=false", documents[0].url)

    def test_publisher_document_list_uses_stable_name_before_session_bound_link(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(
                    {
                        "data": [
                            [
                                "21/07/2026",
                                "Drawing",
                                "Proposed entrance gates",
                                "/docs/SESSION1/Document-SESSION1.pdf",
                                "",
                            ]
                        ]
                    }
                ).encode()

        class FakeOpener:
            def open(self, request, timeout):
                return FakeResponse()

        page = """
        <script>
          var ctx = "/publisher";
          var table = {"url": "/publisher/mvc/getDocumentList;jsessionid=ABC"};
        </script>
        """

        documents = fetch_publisher_document_list(
            page,
            "https://planning.example.gov.uk/publisher/mvc/listDocuments",
            FakeOpener(),
        )

        self.assertEqual(documents[0].title, "Proposed entrance gates")
        self.assertEqual(
            documents[0].url,
            "https://planning.example.gov.uk/publisher/docs/SESSION1/Document-SESSION1.pdf",
        )

    def test_session_bound_publisher_candidates_refresh_for_each_opener_session(self) -> None:
        source_url = "https://planning.example.gov.uk/publisher/mvc/listDocuments"
        documents = [
            PlanningDocument(
                title=f"Proposed drawing {index}",
                url=f"https://planning.example.gov.uk/publisher/docs/OLD/Document-{index}.pdf",
                source_url=source_url,
            )
            for index in (1, 2)
        ]
        source_cache: dict[str, list[PlanningDocument]] = {}

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
            def __init__(self, session: str) -> None:
                self.session = session
                self.urls: list[str] = []

            def open(self, request, timeout):
                url = request.full_url
                self.urls.append(url)
                if url == source_url:
                    return FakeResponse(
                        url,
                        b'<script>var ctx = "/publisher"; var table = '
                        b'{"url": "/publisher/mvc/getDocumentList"};</script>',
                        "text/html",
                    )
                if "getDocumentList" in url:
                    rows = [
                        [
                            "21/07/2026",
                            "Drawing",
                            f"Proposed drawing {index}",
                            f"/docs/{self.session}/Document-{index}.pdf",
                            "",
                        ]
                        for index in (1, 2)
                    ]
                    return FakeResponse(
                        url,
                        json.dumps({"data": rows}).encode(),
                        "application/json",
                    )
                if f"/publisher/docs/{self.session}/" in url:
                    return FakeResponse(url, b"%PDF-1.4", "application/pdf")
                raise HTTPError(url, 404, "Expired session", {}, None)

        downloaded = [
            download_document_file(
                document,
                opener=FakeOpener(f"SESSION-{index}"),
                source_cache=source_cache,
            )
            for index, document in enumerate(documents, start=1)
        ]

        self.assertEqual(
            [result.final_url for result in downloaded],
            [
                "https://planning.example.gov.uk/publisher/docs/SESSION-1/Document-1.pdf",
                "https://planning.example.gov.uk/publisher/docs/SESSION-2/Document-2.pdf",
            ],
        )

    def test_session_bound_tls_fallback_rediscovers_with_replacement_opener(self) -> None:
        source_url = "https://planning.example.gov.uk/application/ABC123"
        session_urls = (
            (
                "https://planning.example.gov.uk/publisher/docs/OLD/Document.pdf",
                "https://planning.example.gov.uk/publisher/docs/VERIFIED/Document.pdf",
                "https://planning.example.gov.uk/publisher/docs/UNVERIFIED/Document.pdf",
            ),
            (
                "https://planning.example.gov.uk/Document/Download?fileName=SitePlan.pdf&token=old",
                "https://planning.example.gov.uk/Document/Download?fileName=SitePlan.pdf&token=verified",
                "https://planning.example.gov.uk/Document/Download?fileName=SitePlan.pdf&token=unverified",
            ),
        )

        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self._url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

            def geturl(self) -> str:
                return self._url

        for original_url, verified_url, unverified_url in session_urls:
            with self.subTest(original_url=original_url):
                opened: list[tuple[str, str]] = []
                discoveries: list[str] = []

                class VerifiedOpener:
                    def open(self, request, timeout):
                        opened.append(("verified", request.full_url))
                        raise URLError(
                            ssl.SSLCertVerificationError(
                                "Missing Authority Key Identifier"
                            )
                        )

                class UnverifiedOpener:
                    def open(self, request, timeout):
                        opened.append(("unverified", request.full_url))
                        if request.full_url == unverified_url:
                            return FakeResponse(unverified_url)
                        raise HTTPError(request.full_url, 404, "Expired session", {}, None)

                verified_opener = VerifiedOpener()
                unverified_opener = UnverifiedOpener()

                def fake_source_candidates(
                    document,
                    opener,
                    *,
                    cache=None,
                    defer_rate_limit=False,
                ):
                    if opener is verified_opener:
                        discoveries.append("verified")
                        return [verified_url]
                    if opener is unverified_opener:
                        discoveries.append("unverified")
                        return [unverified_url]
                    raise AssertionError("unexpected opener")

                document = PlanningDocument(
                    title="Plan",
                    url=original_url,
                    source_url=source_url,
                )
                with (
                    patch(
                        "lead_generator.planning.leads.source_document_candidates",
                        side_effect=fake_source_candidates,
                    ),
                    patch(
                        "lead_generator.planning.leads._build_document_opener",
                        return_value=unverified_opener,
                    ),
                ):
                    downloaded = download_document_file(document, opener=verified_opener)

                self.assertEqual(downloaded.final_url, unverified_url)
                self.assertEqual(discoveries, ["verified", "unverified"])
                self.assertNotIn(("unverified", verified_url), opened)

    def test_source_document_candidate_wait_propagates_cancellation(self) -> None:
        source_url = "https://planning.example.gov.uk/application/ABC123"
        document = PlanningDocument(
            title="Proposed plan.pdf",
            url="https://planning.example.gov.uk/docs/proposed-plan.pdf",
            source_url=source_url,
        )
        cancel_checks = 0

        def should_cancel() -> bool:
            nonlocal cancel_checks
            cancel_checks += 1
            return cancel_checks >= 2

        class RateLimitedOpener:
            def open(self, request, timeout):
                raise HTTPError(
                    request.full_url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "120"},
                    None,
                )

        with (
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", {}),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                wraps=leads_module._wait_for_cancelable_delay,
            ) as wait,
        ):
            with self.assertRaises(DocumentDownloadCancelledError):
                source_document_candidates(
                    document,
                    RateLimitedOpener(),
                    should_cancel=should_cancel,
                )

        wait.assert_called_once_with(120.0, should_cancel)

    def test_nested_enterprise_tls_failure_restarts_source_discovery_with_replacement_opener(self) -> None:
        source_url = (
            "https://planning.example.gov.uk/application/details?applicationNumber=ABC123"
        )
        list_path = "/documents/list"
        stale_url = (
            "https://planning.example.gov.uk/Document/Download?"
            "session=stale&fileName=ApplicationForm.pdf"
        )
        fresh_url = (
            "https://planning.example.gov.uk/OnlinePlanning/DisplaySearchDocument?"
            "session=fresh&fileName=ApplicationForm.pdf"
        )
        source_markup = (
            f'<div id="divDisplayDocumentsUrl" data-url="{list_path}"></div>'
        ).encode()
        events: list[tuple[str, str]] = []

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
            def __init__(self, name: str) -> None:
                self.name = name

            def open(self, request, timeout):
                url = request.full_url
                events.append((self.name, url))
                if url == source_url:
                    return FakeResponse(url, source_markup, "text/html")
                if url.startswith("https://planning.example.gov.uk/documents/list?"):
                    if self.name == "verified":
                        raise URLError(
                            ssl.SSLCertVerificationError(
                                "Missing Authority Key Identifier"
                            )
                        )
                    return FakeResponse(
                        url,
                        (
                            '<a href="/OnlinePlanning/DisplaySearchDocument?session=fresh&amp;'
                            'fileName=ApplicationForm.pdf">Application Form</a>'
                        ).encode(),
                        "text/html",
                    )
                if self.name == "replacement" and url == fresh_url:
                    return FakeResponse(url, b"%PDF-1.4", "application/pdf")
                raise HTTPError(url, 404, "Expired session", {}, None)

        verified_opener = FakeOpener("verified")
        replacement_opener = FakeOpener("replacement")
        document = PlanningDocument(
            title="Application Form",
            url=stale_url,
            source_url=source_url,
        )

        with patch(
            "lead_generator.planning.leads._build_document_opener",
            return_value=replacement_opener,
        ) as opener_factory:
            downloaded = download_document_file(document, opener=verified_opener)

        self.assertEqual(downloaded.final_url, fresh_url)
        self.assertEqual(
            [name for name, url in events if url == source_url],
            ["verified", "replacement"],
        )
        self.assertEqual(
            [name for name, url in events if url.startswith("https://planning.example.gov.uk/documents/list?")],
            ["verified", "replacement"],
        )
        self.assertNotIn(("replacement", stale_url), events)
        opener_factory.assert_called_once_with(verify_tls=False, tls_compat=False)

    def test_generic_plan_documents_use_distinct_url_filenames(self) -> None:
        documents = [
            PlanningDocument(
                title="Plan",
                url=f"https://planning.example.gov.uk/Document/Download?fileName={filename}",
            )
            for filename in ("SitePlan.pdf", "FloorPlan.pdf")
        ]

        def fake_download(document, **kwargs):
            return DownloadedFile(
                payload=f"%PDF-{document.url}".encode(),
                final_url=document.url,
                content_type="application/pdf",
            )

        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory)
            with patch(
                "lead_generator.planning.leads.download_document_file",
                side_effect=fake_download,
            ):
                result = _download_pdf_documents_once(documents, destination)

            self.assertEqual(result.downloaded_count, 2)
            self.assertEqual(
                sorted(path.name for path in destination.iterdir()),
                ["FloorPlan.pdf", "SitePlan.pdf"],
            )

    def test_generic_plan_fallback_matches_filename_identity(self) -> None:
        source_url = "https://planning.example.gov.uk/application/ABC123"
        document = PlanningDocument(
            title="Plan",
            url="https://planning.example.gov.uk/stale?fileName=SitePlan.pdf",
            source_url=source_url,
        )

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
            def open(self, request, timeout):
                url = request.full_url
                if url == source_url:
                    return FakeResponse(
                        url,
                        b"""
                        <html><body>
                          <a href="/current?fileName=OtherPlan.pdf">Plan</a>
                          <a href="/current?fileName=SitePlan.pdf">Plan</a>
                        </body></html>
                        """,
                        "text/html",
                    )
                if "/stale?" in url:
                    raise HTTPError(url, 404, "Expired", {}, None)
                return FakeResponse(url, b"%PDF-1.4", "application/pdf")

        downloaded = download_document_file(document, opener=FakeOpener())

        self.assertEqual(
            downloaded.final_url,
            "https://planning.example.gov.uk/current?fileName=SitePlan.pdf",
        )

    def test_source_document_candidates_prefer_exact_and_reject_ambiguous_fuzzy_matches(self) -> None:
        source_url = "https://planning.example.gov.uk/application/ABC123"
        wanted = PlanningDocument(
            title="Application Form",
            url="https://planning.example.gov.uk/files/stale.pdf",
            source_url=source_url,
        )
        exact_url = "https://planning.example.gov.uk/files/application-form.pdf"
        repeated_fuzzy_url = "https://planning.example.gov.uk/files/redacted-form.pdf"

        cases = (
            (
                "later exact match",
                [
                    PlanningDocument(
                        title="Application Form Covering Letter",
                        url="https://planning.example.gov.uk/files/covering-letter.pdf",
                    ),
                    PlanningDocument(title="Application Form", url=exact_url),
                ],
                [exact_url],
            ),
            (
                "one repeated fuzzy URL",
                [
                    PlanningDocument(title="Application Form Redacted", url=repeated_fuzzy_url),
                    PlanningDocument(title="Application Form Copy", url=repeated_fuzzy_url),
                ],
                [repeated_fuzzy_url],
            ),
            (
                "two fuzzy URLs",
                [
                    PlanningDocument(
                        title="Application Form Redacted",
                        url="https://planning.example.gov.uk/files/redacted-form.pdf",
                    ),
                    PlanningDocument(
                        title="Application Form Covering Letter",
                        url="https://planning.example.gov.uk/files/covering-letter.pdf",
                    ),
                ],
                [],
            ),
        )

        for label, candidates, expected in cases:
            with self.subTest(label=label):
                self.assertEqual(
                    source_document_candidates(
                        wanted,
                        object(),
                        cache={source_url: candidates},
                    ),
                    expected,
                )

    def test_non_generic_fallback_rejects_empty_candidate_identities(self) -> None:
        source_url = "https://planning.example.gov.uk/Planning/Display/2024/00577/4/CD"
        cases = (
            (
                "Forms",
                "ApplicationFormRedacted.pdf",
                "Additional Details",
                "Material%20Schedule%20July%202026.pdf",
            ),
            (
                "Additional Details",
                "Material%20Schedule%20July%202026.pdf",
                "Forms",
                "ApplicationFormRedacted.pdf",
            ),
        )

        for wanted_title, wanted_filename, other_title, other_filename in cases:
            with self.subTest(wanted_title=wanted_title):
                markup = f"""
                    <html><body><table>
                      <tr>
                        <td><a href="/Document/Download?fileName={other_filename}">-</a></td>
                        <td><a href="/Document/Download?fileName={other_filename}">{other_title}</a></td>
                      </tr>
                      <tr>
                        <td><a href="/Document/Download?fileName={wanted_filename}">-</a></td>
                        <td><a href="/Document/Download?fileName={wanted_filename}">{wanted_title}</a></td>
                      </tr>
                    </table></body></html>
                """
                wanted_url = (
                    "https://planning.example.gov.uk/Document/Download?fileName="
                    f"{wanted_filename}"
                )
                document = PlanningDocument(
                    title=wanted_title,
                    url=wanted_url,
                    source_url=source_url,
                )

                with patch(
                    "lead_generator.planning.leads._fetch_html_with_portal_session",
                    return_value=(markup, source_url),
                ):
                    candidates = source_document_candidates(document, object())

                self.assertEqual(candidates, [wanted_url])

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
            self.assertEqual(opener.urls[0], document.url)
            self.assertIn("documentdownload.do", opener.urls[1])
            self.assertNotIn(document.source_url, opener.urls)

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

    def test_element_backed_document_links_ignore_page_chrome(self) -> None:
        page_url = "https://planning.example.gov.uk/application/ABC123"
        document = html.fromstring(
            """
            <html><body>
              <footer>
                <button data-url="/files/footer-data.pdf">Footer data</button>
                <button onclick="window.location='/files/footer-onclick.pdf'">Footer onclick</button>
                <form method="get" action="/Document/Download">
                  <input type="hidden" name="id" value="footer-form">
                  <button>Footer form</button>
                </form>
                <iframe src="/files/footer-iframe.pdf"></iframe>
                <embed src="/files/footer-embed.pdf">
                <object data="/files/footer-object.pdf"></object>
              </footer>
              <main>
                <button data-url="/files/main-data.pdf">Main data</button>
                <button onclick="window.location='/files/main-onclick.pdf'">Main onclick</button>
                <form method="get" action="/Document/Download">
                  <input type="hidden" name="id" value="main-form">
                  <button>Main form</button>
                </form>
                <iframe src="/files/main-iframe.pdf"></iframe>
                <embed src="/files/main-embed.pdf">
                <object data="/files/main-object.pdf"></object>
              </main>
            </body></html>
            """
        )

        self.assertEqual(
            {href for href, _title in iter_document_links(document, page_url)},
            {
                "/files/main-data.pdf",
                "/files/main-onclick.pdf",
                "https://planning.example.gov.uk/Document/Download?id=main-form",
                "/files/main-iframe.pdf",
                "/files/main-embed.pdf",
                "/files/main-object.pdf",
            },
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

    def test_fetch_publisher_document_list_propagates_endpoint_failure(self) -> None:
        page_url = "https://app.example.test/planningdocuments=26%2F001"
        endpoint = "https://app.example.test/publisher/mvc/getDocumentList"
        unavailable = HTTPError(endpoint, 503, "Unavailable", {}, None)

        with patch("lead_generator.planning.leads._open_url_with_retry", side_effect=unavailable):
            with self.assertRaisesRegex(DocumentDiscoveryTransientError, "getDocumentList"):
                fetch_publisher_document_list(
                    '"url": "/publisher/mvc/getDocumentList"',
                    page_url,
                    object(),
                )

    def test_fetch_planit_documents_records_enterprise_failure_and_keeps_direct_links(self) -> None:
        page_url = "https://planning.example.test/application?applicationNumber=26%2F001"
        endpoint = "https://planning.example.test/documents/list"
        page_html = f"""
            <html><body>
              <a href="/documents/direct-plan.pdf">Direct plan.pdf</a>
              <div id="divDisplayDocumentsUrl" data-url="{endpoint}"></div>
            </body></html>
        """
        unavailable = HTTPError(endpoint, 503, "Unavailable", {}, None)
        discovery = DocumentDiscoveryResult()

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch("lead_generator.planning.leads._open_url_with_retry", side_effect=unavailable),
        ):
            documents = fetch_planit_documents(page_url, discovery_result=discovery)

        self.assertIn("Direct plan.pdf", [document.title for document in documents])
        self.assertEqual([failure.source_url for failure in discovery.failed_sources], [endpoint])
        self.assertIn("503", discovery.failed_sources[0].reason)

    def test_fetch_planit_documents_propagates_enterprise_failure_without_collector(self) -> None:
        page_url = "https://planning.example.test/application?applicationNumber=26%2F001"
        endpoint = "https://planning.example.test/documents/list"
        page_html = f'<div id="divDisplayDocumentsUrl" data-url="{endpoint}"></div>'
        unavailable = HTTPError(endpoint, 503, "Unavailable", {}, None)

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch("lead_generator.planning.leads._open_url_with_retry", side_effect=unavailable),
        ):
            with self.assertRaisesRegex(DocumentDiscoveryTransientError, "documents/list"):
                fetch_planit_documents(page_url)

    def test_fetch_enterprise_document_list_propagates_malformed_fragment(self) -> None:
        class EmptyResponse:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b""

        page_url = "https://planning.example.test/application?applicationNumber=26%2F001"
        endpoint = "https://planning.example.test/documents/list"
        page_html = f'<div id="divDisplayDocumentsUrl" data-url="{endpoint}"></div>'

        with patch(
            "lead_generator.planning.leads._open_url_with_retry",
            return_value=EmptyResponse(),
        ):
            with self.assertRaisesRegex(DocumentDiscoveryTransientError, "documents/list"):
                fetch_enterprise_document_list(page_html, page_url, object())

    def test_fetch_planit_documents_selects_public_register_for_community_detail(self) -> None:
        class FakeResponse:
            def __init__(self, payload: dict[str, object]) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return json.dumps(self.payload).encode()

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if "aura.ApexAction.execute=1" in request.full_url:
                    return FakeResponse(
                        {
                            "actions": [
                                {
                                    "state": "SUCCESS",
                                    "returnValue": {
                                        "returnValue": [
                                            {
                                                "Id": "068PUBLIC",
                                                "Title": "Proposed elevations",
                                                "FileExtension": "pdf",
                                            }
                                        ]
                                    },
                                }
                            ]
                        }
                    )
                if "findContentVersionsForPlanning=1" in request.full_url:
                    return FakeResponse({"actions": []})
                return FakeResponse(
                    {
                        "actions": [
                            {
                                "state": "ERROR",
                                "returnValue": None,
                                "error": [
                                    {
                                        "message": "You do not have access to the Apex class named 'FilesPublicCont'."
                                    }
                                ],
                            }
                        ]
                    }
                )

        boot = {
            "mode": "PROD",
            "app": "siteforce:communityApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:communityApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_url = "https://planning.example.test/pr/s/detail/a0iABC"
        page_html = (
            '<a href="/documents/direct-plan.pdf">Direct plan.pdf</a>'
            f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        )
        discovery = DocumentDiscoveryResult()
        opener = FakeOpener()

        with patch(
            "lead_generator.planning.leads._fetch_html_document_page",
            return_value=(page_html, page_url, opener),
        ):
            documents = fetch_planit_documents(page_url, discovery_result=discovery)

        self.assertEqual(
            [document.title for document in documents],
            ["Direct plan.pdf", "Proposed elevations.pdf"],
        )
        self.assertEqual(discovery.failed_sources, [])
        self.assertEqual(len(opener.urls), 1)
        self.assertIn("aura.ApexAction.execute=1", opener.urls[0])

    def test_fetch_planit_documents_selects_salesforce_viewer_for_papplication(self) -> None:
        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "",
        }
        page_url = "https://planning.example.test/s/papplication/a1M123/f26100751"
        page_html = f'<script src="/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        expected = PlanningDocument(
            title="Proposed site plan.pdf",
            url="https://planning.example.test/sfc/servlet.shepherd/version/download/068PLAN",
            source_url=page_url,
        )

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch(
                "lead_generator.planning.leads.fetch_arcus_salesforce_document_list",
                return_value=[expected],
            ) as salesforce,
            patch(
                "lead_generator.planning.leads.fetch_arcus_public_register_file_list",
                return_value=[],
            ) as public_register,
            patch(
                "lead_generator.planning.leads.fetch_arcus_files_public_document_list",
                return_value=[],
            ) as files_public,
        ):
            documents = fetch_planit_documents(page_url)

        self.assertEqual(documents, [expected])
        salesforce.assert_called_once()
        public_register.assert_not_called()
        files_public.assert_not_called()

    def test_fetch_planit_documents_selects_files_public_for_planning_application(self) -> None:
        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_url = "https://planning.example.test/pr/s/planning-application/a0iABC/pl202600001"
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        expected = PlanningDocument(
            title="Proposed floor plans.pdf",
            url="https://planning.example.test/pr/sfc/servlet.shepherd/version/download/068FLOOR",
            source_url=page_url,
        )

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch(
                "lead_generator.planning.leads.fetch_arcus_salesforce_document_list",
                return_value=[],
            ) as salesforce,
            patch(
                "lead_generator.planning.leads.fetch_arcus_public_register_file_list",
                return_value=[],
            ) as public_register,
            patch(
                "lead_generator.planning.leads.fetch_arcus_files_public_document_list",
                return_value=[expected],
            ) as files_public,
        ):
            documents = fetch_planit_documents(page_url)

        self.assertEqual(documents, [expected])
        salesforce.assert_not_called()
        public_register.assert_not_called()
        files_public.assert_called_once()

    def test_fetch_planit_documents_prefers_explicit_arcus_component_metadata(self) -> None:
        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_url = "https://planning.example.test/pr/s/papplication/a0iABC/pl202600001"
        page_html = (
            '<div data-component="markup://arcshared:FilesPublic"></div>'
            f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        )

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch(
                "lead_generator.planning.leads.fetch_arcus_salesforce_document_list",
                return_value=[],
            ) as salesforce,
            patch(
                "lead_generator.planning.leads.fetch_arcus_public_register_file_list",
                return_value=[],
            ) as public_register,
            patch(
                "lead_generator.planning.leads.fetch_arcus_files_public_document_list",
                return_value=[],
            ) as files_public,
        ):
            fetch_planit_documents(page_url)

        salesforce.assert_not_called()
        public_register.assert_not_called()
        files_public.assert_called_once()

    def test_fetch_planit_documents_does_not_guess_arcus_controller_for_unknown_route(self) -> None:
        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_url = "https://planning.example.test/pr/s/application/a0iABC"
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'

        with (
            patch(
                "lead_generator.planning.leads._fetch_html_document_page",
                return_value=(page_html, page_url, object()),
            ),
            patch("lead_generator.planning.leads.fetch_arcus_salesforce_document_list") as salesforce,
            patch("lead_generator.planning.leads.fetch_arcus_public_register_file_list") as public_register,
            patch("lead_generator.planning.leads.fetch_arcus_files_public_document_list") as files_public,
        ):
            documents = fetch_planit_documents(page_url)

        self.assertEqual(documents, [])
        salesforce.assert_not_called()
        public_register.assert_not_called()
        files_public.assert_not_called()

    def test_arcus_document_list_fetchers_propagate_malformed_and_invalid_json(self) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes) -> None:
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return self.payload

        boot = {
            "mode": "PROD",
            "app": "siteforce:napiliApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:napiliApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        page_url = "https://planning.example.test/pr/s/planning-application/a0iABC/pl202600001"
        fetchers = (
            fetch_arcus_salesforce_document_list,
            fetch_arcus_public_register_file_list,
            fetch_arcus_files_public_document_list,
        )

        for fetcher in fetchers:
            for payload in (b"not-json", b'{"actions": {}}'):
                with self.subTest(fetcher=fetcher.__name__, payload=payload):
                    with patch(
                        "lead_generator.planning.leads._open_url_with_retry",
                        return_value=FakeResponse(payload),
                    ):
                        with self.assertRaises(DocumentDiscoveryTransientError):
                            fetcher(page_html, page_url, object())

    def test_dynamic_document_fetchers_ignore_unadvertised_sources(self) -> None:
        page_url = "https://planning.example.test/application"
        fetchers = (
            fetch_enterprise_document_list,
            fetch_arcus_salesforce_document_list,
            fetch_arcus_public_register_file_list,
            fetch_arcus_files_public_document_list,
        )

        with patch(
            "lead_generator.planning.leads._open_url_with_retry",
            side_effect=AssertionError("No dynamic endpoint should be requested"),
        ):
            for fetcher in fetchers:
                with self.subTest(fetcher=fetcher.__name__):
                    self.assertEqual(fetcher("<html></html>", page_url, object()), [])

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

    def test_arcus_document_download_uses_working_direct_url_before_source_page(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

            def geturl(self) -> str:
                return self.url

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if "/s/detail/" in request.full_url:
                    raise AssertionError("source application page should not be requested")
                return FakeResponse(request.full_url)

        document = PlanningDocument(
            title="Proposed elevations.pdf",
            url="https://planning.example.test/pr/sfc/servlet.shepherd/version/download/068PLAN",
            source_url="https://planning.example.test/pr/s/detail/a0iABC",
        )
        opener = FakeOpener()

        downloaded = download_document_file(document, opener=opener)

        self.assertEqual(downloaded.payload, b"%PDF-1.4")
        self.assertEqual(opener.urls, [document.url])

    def test_arcus_document_download_isolates_failed_fallback_and_uses_replacement(self) -> None:
        class FakeResponse:
            headers = {"Content-Type": "application/pdf"}

            def __init__(self, url: str) -> None:
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return None

            def read(self) -> bytes:
                return b"%PDF-1.4"

            def geturl(self) -> str:
                return self.url

        class FakeOpener:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def open(self, request, timeout):
                self.urls.append(request.full_url)
                if request.full_url == document.url:
                    not_found = HTTPError(request.full_url, 404, "Not Found", {}, None)
                    not_found.close()
                    raise not_found
                return FakeResponse(request.full_url)

        boot = {
            "mode": "PROD",
            "app": "siteforce:communityApp",
            "fwuid": "FWUID",
            "loaded": {"APPLICATION@markup://siteforce:communityApp": "APPHASH"},
            "pathPrefix": "/pr",
        }
        source_url = "https://planning.example.test/pr/s/detail/a0iABC"
        page_html = f'<script src="/pr/s/sfsites/l/{quote(json.dumps(boot, separators=(",", ":")), safe="")}/bootstrap.js"></script>'
        document = PlanningDocument(
            title="Proposed elevations.pdf",
            url="https://planning.example.test/pr/sfc/servlet.shepherd/version/download/068STALE",
            source_url=source_url,
        )
        replacement = PlanningDocument(
            title=document.title,
            url="https://planning.example.test/pr/sfc/servlet.shepherd/version/download/068CURRENT",
            source_url=source_url,
        )
        optional_failure = DocumentDiscoveryTransientError(
            "https://planning.example.test/publisher/documents",
            "optional source unavailable",
        )
        opener = FakeOpener()

        try:
            with (
                patch(
                    "lead_generator.planning.leads._fetch_html_with_portal_session",
                    return_value=(page_html, source_url),
                ),
                patch(
                    "lead_generator.planning.leads.fetch_publisher_document_list",
                    side_effect=optional_failure,
                ),
                patch(
                    "lead_generator.planning.leads.fetch_enterprise_document_list",
                    return_value=[],
                ),
                patch(
                    "lead_generator.planning.leads.fetch_arcus_salesforce_document_list",
                    return_value=[],
                ) as salesforce,
                patch(
                    "lead_generator.planning.leads.fetch_arcus_public_register_file_list",
                    return_value=[replacement],
                ) as public_register,
                patch(
                    "lead_generator.planning.leads.fetch_arcus_files_public_document_list",
                    return_value=[],
                ) as files_public,
            ):
                downloaded = download_document_file(document, opener=opener)
        except DocumentDiscoveryTransientError as exc:
            self.fail(f"optional source failure escaped fallback isolation: {exc}")

        self.assertEqual(downloaded.payload, b"%PDF-1.4")
        self.assertEqual(opener.urls, [document.url, replacement.url])
        salesforce.assert_not_called()
        public_register.assert_called_once()
        files_public.assert_not_called()

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

    def test_fetch_json_rate_limit_wait_propagates_search_cancellation(self) -> None:
        url = "https://www.planit.org.uk/api/applics/json"
        cancel_checks = 0

        def should_cancel() -> bool:
            nonlocal cancel_checks
            cancel_checks += 1
            return cancel_checks >= 2

        with (
            patch(
                "lead_generator.planning.leads.urlopen",
                side_effect=HTTPError(
                    url,
                    429,
                    "Too Many Requests",
                    {"Retry-After": "120"},
                    None,
                ),
            ),
            patch("lead_generator.planning.leads._REQUEST_COOLDOWN_UNTIL", {}),
            patch("lead_generator.planning.leads._LAST_REQUEST_AT", {}),
            patch("lead_generator.planning.leads._throttle_request"),
            patch(
                "lead_generator.planning.leads._wait_for_cancelable_delay",
                wraps=leads_module._wait_for_cancelable_delay,
            ) as wait,
        ):
            with self.assertRaises(CouncilSearchCancelledError):
                _fetch_json_with_retry(url, should_cancel=should_cancel)

        wait.assert_called_once_with(120.0, should_cancel)

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
