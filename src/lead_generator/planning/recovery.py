from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import parse_qsl, urlsplit

from lead_generator.planning.enrichment import (
    ENRICHMENT_CSV_FIELDS,
    ContactEnrichment,
    enrich_application_folder,
)
from lead_generator.planning.leads import (
    APPLICATION_CSV_FIELDS,
    DOCUMENT_DOWNLOAD_RETRY_DELAY_SECONDS,
    CouncilTarget,
    DocumentDownloadBatchResult,
    DocumentDiscoveryResult,
    DocumentSourceFailure,
    _download_pdf_documents_once,
    _looks_like_downloadable_document,
    _wait_for_document_retry_cooldown,
    append_csv_rows,
    discover_application_documents,
    initialise_csv,
    load_authority_catalogue,
    sanitize_path_part,
    write_csv,
)
from lead_generator.planning.models import PlanningApplication, PlanningDocument


RECOVERY_AUDIT_FIELDS = [
    "Reference",
    "Council",
    "Eligible Documents",
    "Architect Sources",
    "Phone Sources",
    "Email Sources",
    "Address Sources",
    "Remaining Failed Fields",
    "Document Discovery Status",
]

_UID_QUERY_KEYS = {
    "keyval",
    "param0",
    "id",
    "case",
    "refval",
    "recordnumber",
    "applicationid",
}

_AUDIT_SOURCE_FIELDS = {
    "Architect Sources": "Architect / Company Name",
    "Phone Sources": "Phone Number",
    "Email Sources": "Email Address",
    "Address Sources": "Company Address",
}


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    rows_processed: int
    applications_with_documents: int
    discovery_failures: int
    corrected_csv_path: Path
    audit_csv_path: Path


@dataclass(slots=True)
class _RecoveryItem:
    original_row: dict[str, str]
    application: PlanningApplication
    folder: Path
    processed_document_urls: set[str] = field(default_factory=set)
    pending_documents: list[PlanningDocument] = field(default_factory=list)
    successful_sources: set[str] = field(default_factory=set)
    discovery_failures: list[DocumentSourceFailure] = field(default_factory=list)
    enrichment: ContactEnrichment | None = None


def _catalogue_index(catalogue: dict[str, object]) -> dict[str, CouncilTarget]:
    targets: dict[str, CouncilTarget] = {}
    features = catalogue.get("features", [])
    if not isinstance(features, list):
        return targets
    for feature in features:
        if not isinstance(feature, dict):
            continue
        properties = feature.get("properties") or {}
        if not isinstance(properties, dict):
            continue
        authority = str(properties.get("authority") or properties.get("council_name") or "").strip()
        if not authority:
            continue
        targets[authority.casefold()] = CouncilTarget(
            authority=authority,
            portal_family=str(properties.get("portal_family") or "unknown"),
            scraper_type=str(
                properties.get("scraper_type")
                or properties.get("portal_family")
                or "unknown"
            ),
            base_url=str(properties.get("base_url") or ""),
            listing_url=str(
                properties.get("listing_url")
                or properties.get("planning_url")
                or ""
            ),
            geometry=feature.get("geometry") or {},
            link_test_ok=bool(properties.get("link_test_ok")),
        )
    return targets


def _application_from_row(
    row: dict[str, str],
    catalogue: dict[str, CouncilTarget],
) -> PlanningApplication:
    council = (row.get("council") or "").strip()
    reference = (row.get("Reference") or "").strip()
    application_link = (row.get("application link") or "").strip()
    target = catalogue.get(council.casefold())
    if target is None:
        parsed = urlsplit(application_link)
        base_url = f"{parsed.scheme}://{parsed.netloc}" if parsed.scheme and parsed.netloc else application_link
        target = CouncilTarget(
            authority=council,
            portal_family="unknown",
            scraper_type="unknown",
            base_url=base_url,
            listing_url=application_link or None,
            geometry={},
        )

    uid = next(
        (
            value
            for key, value in parse_qsl(urlsplit(application_link).query, keep_blank_values=False)
            if key.casefold() in _UID_QUERY_KEYS and value
        ),
        "",
    )
    raw = {
        "portal_family": target.portal_family,
        "scraper_type": target.scraper_type,
        "portal_url": application_link,
        "source_url": target.listing_url or target.base_url,
    }
    return PlanningApplication(
        authority=council,
        uid=uid or reference,
        url=application_link or target.base_url,
        reference=reference,
        address=row.get("address") or None,
        description=row.get("proposal") or None,
        date_received=row.get("date received") or None,
        source_url=target.listing_url,
        raw=raw,
    )


def _recovery_item(
    row: dict[str, str],
    catalogue: dict[str, CouncilTarget],
    output_dir: Path,
) -> _RecoveryItem:
    application = _application_from_row(row, catalogue)
    reference = application.reference or application.uid or "unknown"
    folder = (
        output_dir
        / sanitize_path_part(application.authority or "Unknown Council")
        / sanitize_path_part(reference)
    )
    folder.mkdir(parents=True, exist_ok=True)
    return _RecoveryItem(dict(row), application, folder)


def _recover_documents(
    item: _RecoveryItem,
    *,
    final_attempt: bool,
    log: Callable[[str], None] | None,
) -> None:
    try:
        discovery = discover_application_documents(item.application)
    except Exception as exc:
        discovery = DocumentDiscoveryResult(
            failed_sources=[DocumentSourceFailure(item.application.url, str(exc))]
        )
    item.successful_sources.update(discovery.successful_sources)
    item.discovery_failures = list(discovery.failed_sources)

    existing_names = {
        sanitize_path_part(path.name).casefold()
        for path in item.folder.iterdir()
        if path.is_file()
    }
    pending_by_url = {document.url: document for document in item.pending_documents}
    for document in discovery.documents:
        if not _looks_like_downloadable_document(document):
            continue
        if document.url in item.processed_document_urls or document.url in pending_by_url:
            continue
        expected_name = sanitize_path_part(document.title).casefold()
        if expected_name in existing_names:
            item.processed_document_urls.add(document.url)
            continue
        pending_by_url[document.url] = document

    attempted = list(pending_by_url.values())
    if not attempted:
        item.pending_documents = []
        return
    try:
        batch = _download_pdf_documents_once(
            attempted,
            item.folder,
            log=log,
            defer_transient=not final_attempt,
        )
    except Exception as exc:
        item.discovery_failures.append(
            DocumentSourceFailure(item.application.url, f"document download failed: {exc}")
        )
        batch = DocumentDownloadBatchResult(
            transient_documents=[] if final_attempt else attempted,
        )
    transient_urls = {document.url for document in batch.transient_documents}
    item.processed_document_urls.update(
        document.url for document in attempted if document.url not in transient_urls
    )
    item.pending_documents = list(batch.transient_documents)


def recover_search_output(
    output_dir: Path,
    *,
    log: Callable[[str], None] | None = None,
) -> RecoverySummary:
    output_dir = Path(output_dir)
    original_csv = output_dir / "applications.csv"
    corrected_path = output_dir / "applications.corrected.csv"
    audit_path = output_dir / "enrichment_audit.csv"

    with original_csv.open(newline="", encoding="utf-8-sig") as handle:
        input_rows = [dict(row) for row in csv.DictReader(handle)]
    catalogue = _catalogue_index(load_authority_catalogue())
    items = [_recovery_item(row, catalogue, output_dir) for row in input_rows]

    deferred_items: list[_RecoveryItem] = []
    for item in items:
        _recover_documents(item, final_attempt=False, log=log)
        if item.pending_documents or item.discovery_failures:
            deferred_items.append(item)

    if deferred_items and _wait_for_document_retry_cooldown(
        DOCUMENT_DOWNLOAD_RETRY_DELAY_SECONDS,
        None,
        log=log,
        deferred_count=len(deferred_items),
    ):
        for item in deferred_items:
            _recover_documents(item, final_attempt=True, log=log)

    for item in items:
        try:
            item.enrichment = enrich_application_folder(
                item.folder,
                site_address=item.application.address,
                log=log,
            )
        except Exception as exc:
            item.enrichment = ContactEnrichment()
            item.discovery_failures.append(
                DocumentSourceFailure(item.application.url, f"enrichment failed: {exc}")
            )

    corrected_rows = [_corrected_row(item) for item in items]
    audit_rows = [_audit_row(item) for item in items]
    write_csv(corrected_path, corrected_rows)
    _write_rows_atomically(audit_path, RECOVERY_AUDIT_FIELDS, audit_rows)

    return RecoverySummary(
        rows_processed=len(items),
        applications_with_documents=sum(_folder_has_permitted_pdf(item.folder) for item in items),
        discovery_failures=sum(bool(item.discovery_failures) for item in items),
        corrected_csv_path=corrected_path,
        audit_csv_path=audit_path,
    )


def _corrected_row(item: _RecoveryItem) -> dict[str, str]:
    enrichment = item.enrichment or ContactEnrichment()
    row = {field_name: item.original_row.get(field_name, "") for field_name in APPLICATION_CSV_FIELDS}
    row.update(enrichment.to_csv_row())
    return row


def _audit_row(item: _RecoveryItem) -> dict[str, str]:
    enrichment = item.enrichment or ContactEnrichment()
    row = {
        "Reference": item.application.reference or item.application.uid,
        "Council": item.application.authority,
        "Eligible Documents": "; ".join(enrichment.eligible_documents),
        "Remaining Failed Fields": _remaining_failed_fields(enrichment),
        "Document Discovery Status": _discovery_status(item),
    }
    for audit_field, enrichment_field in _AUDIT_SOURCE_FIELDS.items():
        row[audit_field] = "; ".join(enrichment.field_sources.get(enrichment_field, []))
    return row


def _remaining_failed_fields(enrichment: ContactEnrichment) -> str:
    values = enrichment.to_csv_row()
    missing_fields = [field_name for field_name in ENRICHMENT_CSV_FIELDS if values[field_name] == "Failed"]
    if not missing_fields:
        return ""
    eligible = set(enrichment.eligible_documents)
    unreadable = set(enrichment.unreadable_documents)
    if not eligible:
        reason = "no eligible drawing published"
    elif eligible.issubset(unreadable):
        reason = "drawing unreadable after bounded OCR"
    else:
        reason = "field absent from eligible drawings"
    return "; ".join(f"{field_name}: {reason}" for field_name in missing_fields)


def _discovery_status(item: _RecoveryItem) -> str:
    if item.discovery_failures:
        failures = " | ".join(
            f"{failure.source_url}: {failure.reason}"
            for failure in item.discovery_failures
        )
        return f"Partial/Failed: {failures}"
    return f"Completed: {len(item.successful_sources)} source(s) checked"


def _folder_has_permitted_pdf(folder: Path) -> bool:
    if not folder.exists():
        return False
    return any(
        path.is_file()
        and path.suffix.casefold() == ".pdf"
        and _looks_like_downloadable_document(
            PlanningDocument(path.name, path.name, document_type="pdf")
        )
        for path in folder.iterdir()
    )


def _write_rows_atomically(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    initialise_csv(temporary_path, fieldnames)
    append_csv_rows(temporary_path, fieldnames, rows)
    temporary_path.replace(path)


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Recover document downloads and drawing-only enrichment for a search output.",
    )
    parser.add_argument("output_directory", type=Path)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _argument_parser().parse_args(list(argv) if argv is not None else None)
    summary = recover_search_output(args.output_directory, log=print)
    print(f"Rows processed: {summary.rows_processed}")
    print(f"Applications with documents: {summary.applications_with_documents}")
    print(f"Unresolved discovery failures: {summary.discovery_failures}")
    print(f"Corrected applications: {summary.corrected_csv_path}")
    print(f"Enrichment audit: {summary.audit_csv_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
