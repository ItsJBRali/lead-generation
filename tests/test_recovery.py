from __future__ import annotations

import csv
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from lead_generator.planning.enrichment import ContactEnrichment
from lead_generator.planning.leads import (
    APPLICATION_CSV_FIELDS,
    CouncilTarget,
    DocumentDownloadBatchResult,
    DocumentDiscoveryResult,
    DocumentSourceFailure,
    append_csv_row,
    append_csv_rows,
    initialise_csv,
)
from lead_generator.planning.models import PlanningApplication, PlanningDocument
from lead_generator.planning.recovery import (
    _application_from_row,
    _recovery_item,
    recover_search_output,
)


def _catalogue_json(*authorities: str) -> str:
    return json.dumps(
        {
            "type": "FeatureCollection",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "authority": authority,
                        "portal_family": "socrata" if authority == "Camden" else "idox",
                        "scraper_type": "Socrata" if authority == "Camden" else "Idox",
                        "base_url": "https://planning.example.test",
                        "listing_url": "https://planning.example.test/search",
                    },
                    "geometry": {
                        "type": "Polygon",
                        "coordinates": [[[0, 0], [1, 0], [1, 1], [0, 1], [0, 0]]],
                    },
                }
                for authority in authorities
            ],
        }
    )


def _row(reference: str = "REF-1") -> dict[str, str]:
    row = {field_name: "" for field_name in APPLICATION_CSV_FIELDS}
    row.update(
        {
            "Reference": reference,
            "address": f"{reference} Site Road",
            "application link": f"https://planning.example.test/detail?id={reference}",
            "proposal": "Proposed extension",
            "date received": "2026-07-15",
            "council": "Example Council",
        }
    )
    return row


def test_application_from_row_uses_catalogue_and_reference_fallback() -> None:
    target = CouncilTarget(
        authority="Camden",
        portal_family="socrata",
        scraper_type="Socrata",
        base_url="https://opendata.camden.gov.uk",
        listing_url="https://opendata.camden.gov.uk/resource/2eiu-s2cw.json",
        geometry={},
    )
    row = {
        "Reference": "2026/2898/P",
        "address": "1 Example Street, London",
        "application link": "https://opendata.camden.gov.uk/resource/2eiu-s2cw.json",
        "proposal": "Proposed alterations",
        "date received": "2026-07-15",
        "council": "Camden",
    }

    application = _application_from_row(row, {"camden": target})

    assert application.authority == "Camden"
    assert application.reference == "2026/2898/P"
    assert application.uid == "2026/2898/P"
    assert application.url == row["application link"]
    assert application.address == row["address"]
    assert application.description == row["proposal"]
    assert application.date_received == row["date received"]
    assert application.raw == {
        "portal_family": "socrata",
        "scraper_type": "Socrata",
        "portal_url": row["application link"],
        "source_url": target.listing_url,
    }

    idox_target = CouncilTarget(
        authority="Example Council",
        portal_family="idox",
        scraper_type="Idox",
        base_url="https://planning.example.test",
        listing_url="https://planning.example.test/search",
        geometry={},
    )
    for query_key in (
        "keyVal",
        "PARAM0",
        "id",
        "case",
        "refval",
        "recordNumber",
        "applicationId",
    ):
        idox_row = {
            **row,
            "Reference": "PL/26/05693/ADJ",
            "council": "Example Council",
            "application link": (
                "https://planning.example.test/applicationDetails.do?"
                f"activeTab=summary&{query_key.swapcase()}=TI9E6KES0YN00"
            ),
        }
        assert _application_from_row(
            idox_row,
            {"example council": idox_target},
        ).uid == "TI9E6KES0YN00"


def test_application_from_row_uses_modern_agile_path_uid() -> None:
    target = CouncilTarget(
        authority="Richmond",
        portal_family="agile",
        scraper_type="Agile",
        base_url="https://planning.agileapplications.co.uk/richmond/",
        listing_url="https://planning.agileapplications.co.uk/richmond/search-applications",
        geometry={},
    )
    row = {
        **_row("PA26/2493"),
        "council": "Richmond",
        "application link": (
            "https://planning.agileapplications.co.uk/richmond/"
            "application-details/240740"
        ),
    }

    application = _application_from_row(row, {"richmond": target})

    assert application.reference == "PA26/2493"
    assert application.uid == "240740"


def test_application_from_row_prefers_query_uid_over_modern_agile_path_uid() -> None:
    target = CouncilTarget(
        authority="Richmond",
        portal_family="agile",
        scraper_type="Agile",
        base_url="https://planning.agileapplications.co.uk/richmond/",
        listing_url="https://planning.agileapplications.co.uk/richmond/search-applications",
        geometry={},
    )
    row = {
        **_row("PA26/2493"),
        "council": "Richmond",
        "application link": (
            "https://planning.agileapplications.co.uk/richmond/"
            "application-details/240740?applicationId=999999"
        ),
    }

    application = _application_from_row(row, {"richmond": target})

    assert application.uid == "999999"


@pytest.mark.parametrize(
    ("catalogue_family", "catalogue_scraper", "application_link", "expected_family", "expected_scraper"),
    [
        (
            "tascomi",
            "Tascomi",
            "https://planning.gloucestershire.gov.uk/online-applications/"
            "applicationDetails.do?keyVal=ABC123",
            "idox",
            "Idox",
        ),
        (
            "idox",
            "Idox",
            "https://planning.agileapplications.co.uk/richmond/application-details/240740",
            "agile",
            "Agile",
        ),
        (
            "idox",
            "Idox",
            "https://planning.example.test/planning/index.html?fa=getApplication&id=123",
            "tascomi",
            "Tascomi",
        ),
        (
            "bath_planning_api",
            "BathPlanningApi",
            "https://planning.bathnes.gov.uk/online-applications/"
            "applicationDetails.do?keyVal=ABC123",
            "bath_planning_api",
            "BathPlanningApi",
        ),
    ],
)
def test_application_from_row_reconciles_stale_catalogue_portal_family(
    catalogue_family: str,
    catalogue_scraper: str,
    application_link: str,
    expected_family: str,
    expected_scraper: str,
) -> None:
    target = CouncilTarget(
        authority="Example Council",
        portal_family=catalogue_family,
        scraper_type=catalogue_scraper,
        base_url="https://catalogue.example.test",
        listing_url="https://catalogue.example.test/search",
        geometry={},
    )
    row = {**_row(), "application link": application_link}

    application = _application_from_row(row, {"example council": target})

    assert application.raw["portal_family"] == expected_family
    assert application.raw["scraper_type"] == expected_scraper


def test_recovery_preserves_original_and_writes_corrected_audit_files(tmp_path: Path) -> None:
    original_csv = tmp_path / "applications.csv"
    row = _row()
    row.update(
        {
            "Architect / Company Name": "BAD COPYRIGHT VALUE",
            "Phone Number": "064646.00001",
            "Email Address": "bad@example.test",
            "Company Address": "1 Site Road, London, N1 1AA",
        }
    )
    initialise_csv(original_csv, APPLICATION_CSV_FIELDS)
    append_csv_row(original_csv, APPLICATION_CSV_FIELDS, row)
    original_text = original_csv.read_text(encoding="utf-8")
    catalogue = json.loads(_catalogue_json("Example Council"))
    clean = ContactEnrichment(
        architect_company_names=["Example Architects Ltd"],
        email_addresses=["studio@example.co.uk"],
        field_sources={
            "Architect / Company Name": ["Proposed Elevations.pdf"],
            "Email Address": ["Proposed Elevations.pdf"],
        },
        eligible_documents=["Proposed Elevations.pdf"],
    )
    discovered = DocumentDiscoveryResult(
        documents=[PlanningDocument("Proposed Elevations.pdf", "https://docs.test/one.pdf")],
        successful_sources=[row["application link"]],
    )

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", return_value=discovered),
        patch(
            "lead_generator.planning.recovery._download_pdf_documents_once",
            return_value=DocumentDownloadBatchResult(downloaded_count=1),
        ),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=clean),
    ):
        summary = recover_search_output(tmp_path)

    assert original_csv.read_text(encoding="utf-8") == original_text
    with summary.corrected_csv_path.open(encoding="utf-8") as handle:
        corrected_rows = list(csv.DictReader(handle))
    with summary.audit_csv_path.open(encoding="utf-8") as handle:
        audit_rows = list(csv.DictReader(handle))
    assert corrected_rows[0]["Architect / Company Name"] == "Example Architects Ltd"
    assert corrected_rows[0]["Phone Number"] == "Failed"
    assert corrected_rows[0]["Email Address"] == "studio@example.co.uk"
    assert corrected_rows[0]["Company Address"] == "Failed"
    assert audit_rows[0]["Architect Sources"] == "Proposed Elevations.pdf"
    assert audit_rows[0]["Remaining Failed Fields"] == (
        "Phone Number: field absent from eligible drawings; "
        "Company Address: field absent from eligible drawings"
    )
    assert audit_rows[0]["Document Discovery Status"] == "Completed: 1 source(s) checked"
    assert summary.rows_processed == 1
    assert summary.applications_with_documents == 0
    assert summary.discovery_failures == 0


def test_recovery_does_not_redownload_an_existing_file(tmp_path: Path) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    folder = tmp_path / "Example Council" / "REF-1"
    folder.mkdir(parents=True)
    (folder / "Proposed Elevations.pdf").write_bytes(b"%PDF-1.4")
    existing = PlanningDocument(
        "Proposed Elevations.pdf",
        "https://docs.test/proposed-elevations.pdf",
    )
    missing = PlanningDocument(
        "Proposed Floor Plans.pdf",
        "https://docs.test/proposed-floor-plans.pdf",
    )
    discovery = DocumentDiscoveryResult(
        documents=[existing, missing],
        successful_sources=[row["application link"]],
    )
    captured: list[PlanningDocument] = []

    def fake_download(documents, destination, **kwargs):
        batch = list(documents)
        captured.extend(batch)
        return DocumentDownloadBatchResult(downloaded_count=len(batch))

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", return_value=discovery),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", side_effect=fake_download),
        patch(
            "lead_generator.planning.recovery.enrich_application_folder",
            return_value=ContactEnrichment(),
        ),
    ):
        recover_search_output(tmp_path)

    assert [document.title for document in captured] == ["Proposed Floor Plans.pdf"]


def test_recovery_retries_partial_discovery_after_other_rows(tmp_path: Path) -> None:
    rows = [_row(reference) for reference in ("REF-1", "REF-2")]
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_rows(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, rows)
    catalogue = json.loads(_catalogue_json("Example Council"))
    one = PlanningDocument("one.pdf", "https://docs.test/REF-1/one.pdf")
    two = PlanningDocument("two.pdf", "https://docs.test/REF-1/two.pdf")
    three = PlanningDocument("three.pdf", "https://docs.test/REF-2/three.pdf")
    discovery_calls = {"REF-1": 0, "REF-2": 0}
    events: list[str] = []

    def fake_discovery(application: PlanningApplication) -> DocumentDiscoveryResult:
        reference = application.reference or ""
        discovery_calls[reference] += 1
        attempt = "first" if discovery_calls[reference] == 1 else "final"
        events.append(f"discover:{reference}:{attempt}")
        if reference == "REF-1" and attempt == "first":
            return DocumentDiscoveryResult(
                documents=[one],
                successful_sources=["https://docs.test/a"],
                failed_sources=[DocumentSourceFailure("https://docs.test/b", "HTTP 503")],
            )
        if reference == "REF-1":
            return DocumentDiscoveryResult(
                documents=[one, two],
                successful_sources=["https://docs.test/a", "https://docs.test/b"],
            )
        return DocumentDiscoveryResult(
            documents=[three],
            successful_sources=["https://docs.test/c"],
        )

    def fake_download(documents, destination, **kwargs):
        batch = list(documents)
        for document in batch:
            reference = document.url.split("/")[-2]
            events.append(f"download:{reference}:{document.title}")
            destination.mkdir(parents=True, exist_ok=True)
            (destination / document.title).write_bytes(b"%PDF-1.4")
        return DocumentDownloadBatchResult(downloaded_count=len(batch))

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", side_effect=fake_discovery),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", side_effect=fake_download),
        patch("lead_generator.planning.recovery._wait_for_document_retry_cooldown", return_value=True),
        patch(
            "lead_generator.planning.recovery.enrich_application_folder",
            return_value=ContactEnrichment(),
        ),
    ):
        summary = recover_search_output(tmp_path)

    assert events == [
        "discover:REF-1:first",
        "download:REF-1:one.pdf",
        "discover:REF-2:first",
        "download:REF-2:three.pdf",
        "discover:REF-1:final",
        "download:REF-1:two.pdf",
    ]
    assert summary.discovery_failures == 0


def test_recovery_isolates_row_errors_and_categorizes_missing_drawings(tmp_path: Path) -> None:
    rows = [_row(reference) for reference in ("REF-1", "REF-2")]
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_rows(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, rows)
    catalogue = json.loads(_catalogue_json("Example Council"))

    def fake_discovery(application: PlanningApplication) -> DocumentDiscoveryResult:
        if application.reference == "REF-1":
            raise RuntimeError("portal unavailable")
        return DocumentDiscoveryResult(successful_sources=[application.url])

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", side_effect=fake_discovery),
        patch("lead_generator.planning.recovery._wait_for_document_retry_cooldown", return_value=False),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        summary = recover_search_output(tmp_path)

    with summary.audit_csv_path.open(encoding="utf-8") as handle:
        audit_rows = list(csv.DictReader(handle))
    assert len(audit_rows) == 2
    assert audit_rows[0]["Document Discovery Status"].startswith(
        "Partial/Failed: https://planning.example.test/detail?id=REF-1: portal unavailable"
    )
    assert "no eligible drawing published" in audit_rows[0]["Remaining Failed Fields"]
    assert audit_rows[1]["Document Discovery Status"] == "Completed: 1 source(s) checked"
    assert summary.discovery_failures == 1


def test_recovery_reports_a_document_that_permanently_failed_to_download(tmp_path: Path) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    document = PlanningDocument(
        "Proposed Elevations.pdf",
        "https://docs.test/proposed-elevations.pdf",
    )
    discovery = DocumentDiscoveryResult(
        documents=[document],
        successful_sources=[row["application link"]],
    )
    failed_batch = SimpleNamespace(
        downloaded_count=0,
        transient_documents=[],
        failures=[SimpleNamespace(document=document, reason="HTTP 404")],
    )

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", return_value=discovery),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", return_value=failed_batch),
        patch("lead_generator.planning.recovery._wait_for_document_retry_cooldown", return_value=False),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        summary = recover_search_output(tmp_path)

    with summary.audit_csv_path.open(encoding="utf-8") as handle:
        audit_row = next(csv.DictReader(handle))
    assert audit_row["Document Discovery Status"] == (
        "Partial/Failed: https://docs.test/proposed-elevations.pdf: HTTP 404"
    )
    assert summary.discovery_failures == 1


def test_recovery_clears_a_download_failure_after_successful_final_retry(tmp_path: Path) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    document = PlanningDocument(
        "Proposed Elevations.pdf",
        "https://docs.test/proposed-elevations.pdf",
    )
    discovery = DocumentDiscoveryResult(
        documents=[document],
        successful_sources=[row["application link"]],
    )
    download_calls = 0

    def fake_download(documents, destination, **kwargs):
        nonlocal download_calls
        download_calls += 1
        attempted = list(documents)
        if download_calls == 1:
            return SimpleNamespace(
                downloaded_count=0,
                transient_documents=[],
                failures=[SimpleNamespace(document=attempted[0], reason="HTTP 503")],
            )
        (destination / "Proposed Elevations.pdf").write_bytes(b"%PDF-1.4")
        return SimpleNamespace(downloaded_count=1, transient_documents=[], failures=[])

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", return_value=discovery),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", side_effect=fake_download),
        patch("lead_generator.planning.recovery._wait_for_document_retry_cooldown", return_value=True),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        summary = recover_search_output(tmp_path)

    with summary.audit_csv_path.open(encoding="utf-8") as handle:
        audit_row = next(csv.DictReader(handle))
    assert download_calls == 2
    assert audit_row["Document Discovery Status"] == "Completed: 1 source(s) checked"
    assert summary.discovery_failures == 0


def test_recovery_isolates_application_folder_construction_per_row(tmp_path: Path) -> None:
    rows = [_row(reference) for reference in ("REF-1", "REF-2")]
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_rows(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, rows)
    original_text = (tmp_path / "applications.csv").read_text(encoding="utf-8")
    catalogue = json.loads(_catalogue_json("Example Council"))

    def fail_first_folder(row, targets, output_dir):
        if row["Reference"] == "REF-1":
            raise OSError("folder access denied")
        return _recovery_item(row, targets, output_dir)

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery._recovery_item", side_effect=fail_first_folder),
        patch(
            "lead_generator.planning.recovery.discover_application_documents",
            return_value=DocumentDiscoveryResult(successful_sources=[rows[1]["application link"]]),
        ),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        summary = recover_search_output(tmp_path)

    assert (tmp_path / "applications.csv").read_text(encoding="utf-8") == original_text
    with summary.corrected_csv_path.open(encoding="utf-8") as handle:
        corrected_rows = list(csv.DictReader(handle))
    with summary.audit_csv_path.open(encoding="utf-8") as handle:
        audit_rows = list(csv.DictReader(handle))
    assert [row["Reference"] for row in corrected_rows] == ["REF-1", "REF-2"]
    assert [row["Reference"] for row in audit_rows] == ["REF-1", "REF-2"]
    assert audit_rows[0]["Document Discovery Status"].endswith(
        "application folder setup failed: folder access denied"
    )
    assert audit_rows[1]["Document Discovery Status"] == "Completed: 1 source(s) checked"


def test_recovery_matches_extensionless_title_to_existing_pdf_only(tmp_path: Path) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    folder = tmp_path / "Example Council" / "REF-1"
    folder.mkdir(parents=True)
    (folder / "Proposed Elevations.pdf").write_bytes(b"%PDF-1.4")
    existing = PlanningDocument(
        "Proposed Elevations",
        "https://docs.test/proposed-elevations.pdf",
    )
    unrelated = PlanningDocument(
        "Proposed Elevations Revised",
        "https://docs.test/proposed-elevations-revised.pdf",
    )
    captured: list[PlanningDocument] = []

    def fake_download(documents, destination, **kwargs):
        captured.extend(documents)
        return DocumentDownloadBatchResult(downloaded_count=len(captured))

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch(
            "lead_generator.planning.recovery.discover_application_documents",
            return_value=DocumentDiscoveryResult(
                documents=[existing, unrelated],
                successful_sources=[row["application link"]],
            ),
        ),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", side_effect=fake_download),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        recover_search_output(tmp_path)

    assert captured == [unrelated]


@pytest.mark.parametrize(
    ("existing_suffix", "expected_download"),
    [
        (".txt", True),
        (".exe", True),
        (".PDF", False),
    ],
)
def test_extensionless_title_only_matches_existing_pdf(
    tmp_path: Path,
    existing_suffix: str,
    expected_download: bool,
) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    folder = tmp_path / "Example Council" / "REF-1"
    folder.mkdir(parents=True)
    (folder / f"Proposed Elevations{existing_suffix}").write_bytes(b"existing")
    document = PlanningDocument(
        "Proposed Elevations",
        "https://docs.test/proposed-elevations.pdf",
    )
    captured: list[PlanningDocument] = []

    def fake_download(documents, destination, **kwargs):
        batch = list(documents)
        captured.extend(batch)
        return DocumentDownloadBatchResult(downloaded_count=len(batch))

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch(
            "lead_generator.planning.recovery.discover_application_documents",
            return_value=DocumentDiscoveryResult(
                documents=[document],
                successful_sources=[row["application link"]],
            ),
        ),
        patch("lead_generator.planning.recovery._download_pdf_documents_once", side_effect=fake_download),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        recover_search_output(tmp_path)

    assert captured == ([document] if expected_download else [])


def test_recovery_summary_counts_each_unresolved_failure_entry(tmp_path: Path) -> None:
    row = _row()
    initialise_csv(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS)
    append_csv_row(tmp_path / "applications.csv", APPLICATION_CSV_FIELDS, row)
    catalogue = json.loads(_catalogue_json("Example Council"))
    discovery = DocumentDiscoveryResult(
        failed_sources=[
            DocumentSourceFailure("https://docs.test/source-a", "HTTP 503"),
            DocumentSourceFailure("https://docs.test/source-b", "HTTP 504"),
        ]
    )

    with (
        patch("lead_generator.planning.recovery.load_authority_catalogue", return_value=catalogue),
        patch("lead_generator.planning.recovery.discover_application_documents", return_value=discovery),
        patch("lead_generator.planning.recovery._wait_for_document_retry_cooldown", return_value=False),
        patch("lead_generator.planning.recovery.enrich_application_folder", return_value=ContactEnrichment()),
    ):
        summary = recover_search_output(tmp_path)

    assert summary.discovery_failures == 2
