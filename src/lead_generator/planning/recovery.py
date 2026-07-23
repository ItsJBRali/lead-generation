from __future__ import annotations

import argparse
import csv
import re
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
    DocumentDownloadFailure,
    DocumentDiscoveryResult,
    DocumentSourceFailure,
    _document_identity,
    _document_identity_title,
    _download_pdf_documents_once,
    _known_file_extensions,
    _looks_like_downloadable_document,
    _wait_for_document_retry_cooldown,
    append_csv_rows,
    discover_application_documents,
    initialise_csv,
    load_authority_catalogue,
    normalize_url,
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

_RECOVERY_SCRAPER_NAMES = {
    "agile": "Agile",
    "bath_planning_api": "BathPlanningApi",
    "idox": "Idox",
    "tascomi": "Tascomi",
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
    download_failures: dict[str, DocumentDownloadFailure] = field(default_factory=dict)
    processing_failures: list[DocumentSourceFailure] = field(default_factory=list)
    setup_failed: bool = False
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
    council = _row_text(row, "council")
    reference = _row_text(row, "Reference")
    application_link = _row_text(row, "application link")
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
    route_family = _application_route_family(council, application_link)
    portal_family = route_family or target.portal_family
    scraper_type = (
        _RECOVERY_SCRAPER_NAMES[route_family]
        if route_family
        else target.scraper_type
    )
    if not uid and portal_family.casefold() == "agile":
        path_parts = [part for part in urlsplit(application_link).path.split("/") if part]
        if (
            len(path_parts) >= 2
            and path_parts[-2].casefold() == "application-details"
            and path_parts[-1].isdigit()
        ):
            uid = path_parts[-1]
    raw = {
        "portal_family": portal_family,
        "scraper_type": scraper_type,
        "portal_url": application_link,
        "source_url": target.listing_url or target.base_url,
    }
    return PlanningApplication(
        authority=council,
        uid=uid or reference,
        url=application_link or target.base_url,
        reference=reference,
        address=_row_text(row, "address") or None,
        description=_row_text(row, "proposal") or None,
        date_received=_row_text(row, "date received") or None,
        source_url=target.listing_url,
        raw=raw,
    )


def _application_route_family(council: str, application_link: str) -> str | None:
    parts = urlsplit(application_link)
    host = (parts.hostname or "").casefold()
    path = parts.path.casefold().rstrip("/")
    query = {
        key.casefold(): value.casefold()
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
    }

    if (
        council.casefold() in {"bath", "bath and north east somerset"}
        and host == "app.bathnes.gov.uk"
        and path.endswith("/webforms/planning/details.html")
    ):
        return "bath_planning_api"
    if (
        path.endswith("/applicationdetails.do")
        and "/online-applications/" in path
    ):
        return "idox"
    path_parts = [part for part in path.split("/") if part]
    if (
        host == "planning.agileapplications.co.uk"
        and len(path_parts) >= 2
        and path_parts[-2] == "application-details"
        and path_parts[-1].isdigit()
    ):
        return "agile"
    if query.get("fa") == "getapplication":
        return "tascomi"
    return None


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


def _row_text(row: dict[str, str], key: str) -> str:
    value = row.get(key)
    return "" if value is None else str(value).strip()


def _failed_recovery_item(
    row: dict[str, str],
    catalogue: dict[str, CouncilTarget],
    output_dir: Path,
    exc: Exception,
) -> _RecoveryItem:
    try:
        application = _application_from_row(row, catalogue)
    except Exception:
        council = _row_text(row, "council")
        reference = _row_text(row, "Reference")
        application_link = _row_text(row, "application link")
        application = PlanningApplication(
            authority=council,
            uid=reference or "unknown",
            url=application_link,
            reference=reference,
            address=_row_text(row, "address") or None,
            description=_row_text(row, "proposal") or None,
            date_received=_row_text(row, "date received") or None,
        )
    reference = application.reference or application.uid or "unknown"
    folder = (
        output_dir
        / sanitize_path_part(application.authority or "Unknown Council")
        / sanitize_path_part(reference)
    )
    source = application.url or f"input row {reference}"
    return _RecoveryItem(
        original_row=dict(row),
        application=application,
        folder=folder,
        processing_failures=[
            DocumentSourceFailure(source, f"application folder setup failed: {exc}")
        ],
        setup_failed=True,
    )


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

    existing_paths = [
        path
        for path in item.folder.iterdir()
        if path.is_file()
    ]
    pending_by_url = {document.url: document for document in item.pending_documents}
    for document in discovery.documents:
        if not _looks_like_downloadable_document(document):
            continue
        if document.url in item.processed_document_urls or document.url in pending_by_url:
            continue
        if any(_existing_file_matches_document(path, document) for path in existing_paths):
            item.processed_document_urls.add(document.url)
            _clear_matching_download_failures(item, document)
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
        batch = DocumentDownloadBatchResult(
            transient_documents=[] if final_attempt else attempted,
            failures=[
                DocumentDownloadFailure(
                    document,
                    f"document download failed: {exc}",
                )
                for document in attempted
            ],
        )
    transient_urls = {document.url for document in batch.transient_documents}
    failed_urls = {failure.document.url for failure in batch.failures}
    for failure in batch.failures:
        item.download_failures[failure.document.url] = failure
    successful_urls = {
        document.url
        for document in attempted
        if document.url not in transient_urls and document.url not in failed_urls
    }
    for document in attempted:
        if document.url in successful_urls:
            _clear_matching_download_failures(item, document)
    item.processed_document_urls.update(
        successful_urls
    )
    item.pending_documents = list(batch.transient_documents)


def _clear_matching_download_failures(
    item: _RecoveryItem,
    successful_document: PlanningDocument,
) -> None:
    successful_key = _logical_document_key(successful_document)
    for url, failure in list(item.download_failures.items()):
        if url == successful_document.url or (
            successful_key is not None
            and _logical_document_key(failure.document) == successful_key
        ):
            item.download_failures.pop(url, None)


def _logical_document_key(
    document: PlanningDocument,
) -> tuple[str, str] | None:
    if not document.source_url:
        return None
    identity = _document_identity(document)
    if not identity:
        return None
    return normalize_url(document.source_url), identity


def _existing_file_matches_document(path: Path, document: PlanningDocument) -> bool:
    existing_base, existing_extensions = _saved_file_identity(path.name)
    expected_base, expected_extensions = _saved_file_identity(
        _document_identity_title(document)
    )
    if not expected_base:
        return False
    if (
        existing_base == expected_base
        and existing_extensions == expected_extensions
    ):
        return True
    if not _compatible_saved_extensions(
        existing_extensions,
        expected_extensions,
    ):
        return False
    if existing_base == expected_base:
        return True
    generated_identity = _generated_collision_saved_file_identity(path.name)
    return bool(
        generated_identity
        and generated_identity[0] == expected_base
        and _compatible_saved_extensions(
            generated_identity[1],
            expected_extensions,
        )
    )


def _generated_collision_saved_file_identity(
    value: str,
) -> tuple[str, tuple[str, ...]] | None:
    name = sanitize_path_part(Path(value.replace("\\", "/")).name).casefold()
    match = re.fullmatch(
        r"(.+(\.[a-z0-9]+))-([2-9]\d*)(\.[a-z0-9]+)",
        name,
    )
    if not match or match.group(2) != match.group(4):
        return None
    return _saved_file_identity(f"{match.group(1)}{match.group(4)}")


def _saved_file_identity(value: str) -> tuple[str, tuple[str, ...]]:
    name = sanitize_path_part(Path(value.replace("\\", "/")).name).casefold()
    extensions: list[str] = []
    while True:
        suffix = Path(name).suffix.casefold()
        if suffix not in _known_file_extensions():
            break
        extensions.append(suffix)
        name = Path(name).stem
    return name, tuple(extensions)


def _compatible_saved_extensions(
    existing_extensions: tuple[str, ...],
    expected_extensions: tuple[str, ...],
) -> bool:
    if not expected_extensions:
        return bool(existing_extensions) and set(existing_extensions) == {".pdf"}
    return bool(set(existing_extensions).intersection(expected_extensions))


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
    items: list[_RecoveryItem] = []
    for row in input_rows:
        try:
            item = _recovery_item(row, catalogue, output_dir)
        except Exception as exc:
            item = _failed_recovery_item(row, catalogue, output_dir, exc)
        items.append(item)

    deferred_items: list[_RecoveryItem] = []
    for item in items:
        if item.setup_failed:
            continue
        _recover_documents(item, final_attempt=False, log=log)
        if item.pending_documents or item.discovery_failures or item.download_failures:
            deferred_items.append(item)

    if deferred_items and _wait_for_document_retry_cooldown(
        DOCUMENT_DOWNLOAD_RETRY_DELAY_SECONDS,
        None,
        log=log,
        deferred_count=len(deferred_items),
    ):
        for item in deferred_items:
            _recover_documents(item, final_attempt=True, log=log)

    late_items = [
        item
        for item in deferred_items
        if item.pending_documents or item.discovery_failures or item.download_failures
    ]
    if late_items and _wait_for_document_retry_cooldown(
        DOCUMENT_DOWNLOAD_RETRY_DELAY_SECONDS,
        None,
        log=log,
        deferred_count=len(late_items),
    ):
        for item in late_items:
            _recover_documents(item, final_attempt=True, log=log)

    for item in items:
        if item.setup_failed:
            item.enrichment = ContactEnrichment()
            continue
        try:
            item.enrichment = enrich_application_folder(
                item.folder,
                site_address=item.application.address,
                log=log,
            )
        except Exception as exc:
            item.enrichment = ContactEnrichment()
            item.processing_failures.append(
                DocumentSourceFailure(item.application.url, f"enrichment failed: {exc}")
            )

    corrected_rows = [_corrected_row(item) for item in items]
    audit_rows = [_audit_row(item) for item in items]
    write_csv(corrected_path, corrected_rows)
    _write_rows_atomically(audit_path, RECOVERY_AUDIT_FIELDS, audit_rows)

    return RecoverySummary(
        rows_processed=len(items),
        applications_with_documents=sum(_folder_has_permitted_pdf(item.folder) for item in items),
        discovery_failures=sum(len(_unresolved_failures(item)) for item in items),
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
    failures = _unresolved_failures(item)
    if failures:
        failures = " | ".join(
            f"{failure.source_url}: {failure.reason}"
            for failure in failures
        )
        return f"Partial/Failed: {failures}"
    return f"Completed: {len(item.successful_sources)} source(s) checked"


def _unresolved_failures(item: _RecoveryItem) -> list[DocumentSourceFailure]:
    download_failures = [
        DocumentSourceFailure(failure.document.url, failure.reason)
        for failure in item.download_failures.values()
    ]
    return [
        *item.discovery_failures,
        *download_failures,
        *item.processing_failures,
    ]


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
