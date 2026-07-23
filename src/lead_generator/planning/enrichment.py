from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable

from pypdf import PdfReader

from lead_generator.planning.drawing_sources import (
    classify_drawing_source,
    preclassify_drawing_source,
)


FAILED_ENRICHMENT_VALUE = "Failed"
ENRICHMENT_CSV_FIELDS = [
    "Architect / Company Name",
    "Phone Number",
    "Email Address",
    "Company Address",
]

MIN_SELECTABLE_PAGE_CHARACTERS = 50
MAX_OCR_PAGES_PER_DOCUMENT = 6
OCR_RENDER_SCALE = 1.0

EMAIL_RE = re.compile(
    r"(?i)(?<![\w.+-])([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})(?![\w.-])"
)
PHONE_RE = re.compile(
    r"(?<![\w])(?:\+44\s*(?:\(0\)\s*)?|0044\s*|0)(?:[\s().-]*\d){9,11}(?!\d)"
)
POSTCODE_RE = re.compile(
    r"(?i)\b(?:GIR\s?0AA|(?:[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]?\s*\d[ABD-HJLNP-UW-Z]{2}))\b"
)

APPLICATION_FORM_FILENAME_MARKERS = (
    "application form",
    "application_form",
    "application-form",
    "applicationform",
    "app form",
    "app_form",
    "appform",
)
APPLICATION_FORM_TEXT_MARKERS = (
    "applicant details",
    "agent details",
    "applicant name and address",
    "agent name and address",
    "are you an agent acting on behalf of the applicant",
)

PROFESSIONAL_ROLE_MARKERS = (
    "architect",
    "architecture",
    "planning agent",
    "planning consultant",
    "agent details",
    "prepared by",
    "report prepared by",
    "drawn by",
    "designed by",
    "design consultant",
    "project architect",
    "landscape architect",
    "chartered surveyor",
    "structural engineer",
    "civil engineer",
    "transport consultant",
    "consulting engineer",
    " arb ",
    "mciat",
    "mciob",
    "mrtpi",
    "mrics",
    " riba ",
)
CLIENT_ROLE_MARKERS = (
    "applicant",
    "client",
    "site owner",
    "landowner",
    "property owner",
    "owner details",
    "owner",
    "contractor",
)
AUTHOR_ROLE_MARKERS = (
    "architect",
    "architecture",
    "prepared by",
    "drawn by",
    "designed by",
    "designer",
    "design",
)
NAME_NOISE_MARKERS = (
    "copyright",
    "all rights reserved",
    "this drawing",
    "property of",
    "not copied",
    "checked by",
    "checked and",
    "approved by",
    "surveyed",
    "authorised",
    "scale",
    "revision",
    "drawing status",
    "for planning",
)
ADDRESS_STOP_TOKENS = {
    "the",
    "road",
    "street",
    "lane",
    "drive",
    "avenue",
    "close",
    "way",
    "uk",
    "united",
    "kingdom",
}
COMPANY_END_RE = re.compile(
    r"(?i)\b(?:architects?|architecture|associates|consultants?|consulting|"
    r"surveyors?|engineers?|planning\s+(?:group|consultancy|services)|"
    r"design\s+(?:studio|consultancy|services|group)|studio|practice|partnership|"
    r"limited|ltd\.?|llp|plc)\s*[.)']*$"
)
PROFESSIONAL_LABEL_RE = re.compile(
    r"(?i)^\s*(?:prepared\s+by|report\s+prepared\s+by|architect|project\s+architect|"
    r"planning\s+agent|planning\s+consultant|agent|designer|designed\s+by|"
    r"drawn\s+by|consultant|author)\s*[:\-]\s*(.+?)\s*$"
)
PROFESSIONAL_CREDENTIAL_RE = re.compile(
    r"(?i)\b(?:ARB|ASI|MASI|CIAT|MCIAT|MCIOB|MRTPI|MRICS|RIBA|CMLI|MEng|BArch|DipArch)\b"
)
ADDRESS_WORD_RE = re.compile(
    r"(?i)\b(?:street|road|lane|avenue|court|house|building|park|way|square|drive|"
    r"close|terrace|place|yard|unit|suite|floor|centre|center|business|estate|offices?)\b"
)

BLOCKED_EMAIL_DOMAINS = (
    ".gov.uk",
    "planningportal.co.uk",
    "planningportal.gov.uk",
    "pins.gsi.gov.uk",
    "greatercambridgeplanning.org",
)
FREE_EMAIL_DOMAINS = {
    "aol.com",
    "gmail.com",
    "googlemail.com",
    "hotmail.com",
    "hotmail.co.uk",
    "icloud.com",
    "live.com",
    "live.co.uk",
    "outlook.com",
    "outlook.co.uk",
    "proton.me",
    "protonmail.com",
    "yahoo.com",
    "yahoo.co.uk",
}

GENERIC_COMPANY_HEADINGS = {
    "application for planning permission",
    "design and access statement",
    "planning application",
    "planning statement",
    "proposed design",
    "architectural design",
    "planning portal",
    "local planning authority",
}
FORM_STOP_PREFIXES = (
    "description of",
    "site visit",
    "materials",
    "vehicle parking",
    "trees and hedges",
    "assessment of flood",
    "authority employee",
    "ownership certificates",
    "declaration",
    "biodiversity",
    "listed building",
    "related proposals",
    "immunity from listing",
    "eligibility",
    "site area",
    "proposal details",
)
FORM_FIELD_LABELS = {
    "name company",
    "title",
    "first name",
    "surname",
    "last name",
    "company",
    "company name",
    "address",
    "address line 1",
    "address line 2",
    "address line 3",
    "town city",
    "town",
    "county",
    "country",
    "postcode",
    "primary number",
    "secondary number",
    "fax number",
    "email address",
    "contact details",
    "applicant contact details",
    "applicant details",
    "agent details",
}

_OCR_ENGINE: object | None = None
_OCR_ENGINE_LOCK = threading.Lock()


@dataclass(slots=True)
class ContactEnrichment:
    architect_company_names: list[str] = field(default_factory=list)
    phone_numbers: list[str] = field(default_factory=list)
    email_addresses: list[str] = field(default_factory=list)
    company_addresses: list[str] = field(default_factory=list)
    field_sources: dict[str, list[str]] = field(default_factory=dict)
    eligible_documents: list[str] = field(default_factory=list)
    unreadable_documents: list[str] = field(default_factory=list)
    rejected_documents: dict[str, str] = field(default_factory=dict)

    def to_csv_row(self) -> dict[str, str]:
        return {
            "Architect / Company Name": _joined_or_failed(self.architect_company_names),
            "Phone Number": _joined_or_failed(self.phone_numbers),
            "Email Address": _joined_or_failed(self.email_addresses),
            "Company Address": _joined_or_failed(self.company_addresses),
        }


@dataclass(slots=True)
class _Party:
    person_name: str = ""
    company_name: str = ""
    address: str = ""

    @property
    def display_name(self) -> str:
        if self.person_name and self.company_name:
            return f"{self.person_name} ({self.company_name})"
        return self.company_name or self.person_name

    def exclusion_values(self) -> list[str]:
        return [value for value in (self.person_name, self.company_name, self.address) if value]


@dataclass(slots=True)
class _PdfText:
    path: Path
    text: str
    application_form: bool
    ocr_pages: int = 0
    first_page_text: str = ""
    cache: _PdfReadCache | None = field(default=None, repr=False)


@dataclass(slots=True)
class _PdfReadCache:
    reader: object | None
    page_count: int
    page_text: dict[int, str] = field(default_factory=dict)
    ocr_attempted: set[int] = field(default_factory=set)
    reader_error: Exception | None = None


@dataclass(slots=True)
class _Exclusions:
    parties: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)

    def add_party(self, value: str | None) -> None:
        value = _clean_candidate(value)
        if value:
            _append_unique(self.parties, value)

    def add_address(self, value: str | None) -> None:
        value = _clean_candidate(value)
        if value:
            _append_unique(self.addresses, value)

    def matches_party(self, value: str) -> bool:
        return any(_same_value(value, excluded) for excluded in self.parties)

    def matches_address(self, value: str) -> bool:
        value_postcodes = set(_postcodes(value))
        return any(
            _same_value(value, excluded)
            or bool(value_postcodes.intersection(_postcodes(excluded)))
            or _similar_site_address(value, excluded)
            for excluded in self.addresses
        )


class _Accumulator:
    def __init__(self, exclusions: _Exclusions) -> None:
        self.result = ContactEnrichment()
        self.exclusions = exclusions
        self.source_document: str | None = None

    def _record_source(self, field_name: str) -> None:
        if not self.source_document:
            return
        sources = self.result.field_sources.setdefault(field_name, [])
        source_key = _normalise_value(self.source_document)
        if not any(_normalise_value(existing) == source_key for existing in sources):
            sources.append(self.source_document)

    def add_name(self, value: str | None) -> None:
        value = _clean_candidate(value)
        value = re.sub(r"^(?:\u00c2\s*)?[\u00a9\u00ae]\s*", "", value).strip()
        if (
            not value
            or _is_name_noise(value)
            or self.exclusions.matches_party(value)
            or _is_generic_company_heading(value)
        ):
            return
        if any(
            _same_value(value, existing) or _similar_company_name(value, existing)
            for existing in self.result.architect_company_names
        ):
            return
        self.result.architect_company_names.append(value)
        self._record_source("Architect / Company Name")

    def add_phone(self, value: str | None) -> None:
        value = _normalise_phone(value or "")
        if value and not any(
            re.sub(r"\D", "", value) == re.sub(r"\D", "", existing)
            for existing in self.result.phone_numbers
        ):
            self.result.phone_numbers.append(value)
            self._record_source("Phone Number")

    def add_email(self, value: str | None) -> None:
        value = (value or "").strip(" <>.,;:").casefold()
        if value.startswith("email-"):
            value = value.removeprefix("email-")
        if value and not _blocked_email(value):
            for index, existing in enumerate(self.result.email_addresses):
                if not _similar_ocr_email(value, existing):
                    continue
                if _prefer_shorter_ocr_email(value, existing):
                    self.result.email_addresses[index] = value
                    self._record_source("Email Address")
                return
            self.result.email_addresses.append(value)
            self._record_source("Email Address")

    def add_address(self, value: str | None) -> None:
        value = _clean_professional_address(value)
        if not value or self.exclusions.matches_address(value):
            return
        value_postcodes = set(_postcodes(value))
        if any(
            _same_value(value, existing)
            or bool(value_postcodes.intersection(_postcodes(existing)))
            for existing in self.result.company_addresses
        ):
            return
        self.result.company_addresses.append(value)
        self._record_source("Company Address")


def empty_enrichment_row(*, requested: bool) -> dict[str, str]:
    value = FAILED_ENRICHMENT_VALUE if requested else ""
    return {field_name: value for field_name in ENRICHMENT_CSV_FIELDS}


def enrich_application_folder(
    folder: Path,
    *,
    applicant_name: str | None = None,
    agent_name: str | None = None,
    site_address: str | None = None,
    log: Callable[[str], None] | None = None,
) -> ContactEnrichment:
    """Extract professional contact details from every PDF saved for one application."""

    exclusions = _Exclusions()
    exclusions.add_party(applicant_name)
    exclusions.add_address(site_address)
    pdf_paths = sorted(
        (path for path in folder.iterdir() if path.is_file() and _is_pdf(path)),
        key=lambda path: path.name.casefold(),
    ) if folder.exists() else []

    if not pdf_paths and log:
        log("No downloaded PDFs were available to enrich")
    accumulator = _Accumulator(exclusions)
    for path in pdf_paths:
        preliminary = preclassify_drawing_source(path.name)
        if not preliminary.eligible and not preliminary.needs_text:
            accumulator.result.rejected_documents[path.name] = preliminary.reason
            continue
        first_page: _PdfText | None = None
        try:
            if log:
                log(f"Checking the first page of {path.name}")
            first_page = extract_pdf_first_page_text(path)
            if first_page.application_form:
                accumulator.result.rejected_documents[path.name] = "application form"
                continue

            decision = classify_drawing_source(path.name, first_page.text)
            if (
                not decision.eligible
                and decision.needs_text
                and preliminary.needs_text
                and _needs_ocr(first_page.text)
            ):
                first_page = extract_pdf_first_page_with_ocr(path, first_page)
                if first_page.ocr_pages and log:
                    log(f"OCR read the first page of {path.name}")
                if first_page.application_form:
                    accumulator.result.rejected_documents[path.name] = "application form"
                    continue
                decision = classify_drawing_source(path.name, first_page.text)
            if not decision.eligible:
                accumulator.result.rejected_documents[path.name] = decision.reason
                continue

            if log:
                log(f"Reading {path.name} for professional contact details")
            document = extract_pdf_text(path, first_page=first_page)
            if document.ocr_pages and log:
                log(f"OCR read {document.ocr_pages} page(s) from {path.name}")
            if document.application_form:
                accumulator.result.rejected_documents[path.name] = "application form"
                continue

            final_decision = classify_drawing_source(
                path.name,
                document.first_page_text or first_page.text,
            )
            if not final_decision.eligible:
                accumulator.result.rejected_documents[path.name] = final_decision.reason
                continue

            accumulator.result.eligible_documents.append(path.name)
            if not _meaningful_text(document.text):
                accumulator.result.unreadable_documents.append(path.name)
                continue
            accumulator.source_document = path.name
            try:
                extract_professional_details(document.text, path.name, accumulator)
            finally:
                accumulator.source_document = None
            if all(
                (
                    accumulator.result.architect_company_names,
                    accumulator.result.phone_numbers,
                    accumulator.result.email_addresses,
                    accumulator.result.company_addresses,
                )
            ):
                break
        except Exception as exc:  # pragma: no cover - malformed live documents vary widely
            accumulator.result.rejected_documents[path.name] = f"read failed: {exc}"
            if log:
                log(f"Could not read {path.name} for enrichment: {exc}")
        finally:
            _close_pdf_cache(first_page)

    return accumulator.result


def extract_pdf_first_page_text(path: Path) -> _PdfText:
    cache = _open_pdf_cache(path)
    try:
        _read_selectable_pages(cache, [0])
        return _pdf_text_from_cache(path, cache, [0])
    except Exception:
        _close_pdf_read_cache(cache)
        raise


def extract_pdf_first_page_with_ocr(path: Path, first_page: _PdfText) -> _PdfText:
    cache = first_page.cache or _open_pdf_cache(path)
    _read_selectable_pages(cache, [0])
    _ocr_cached_pages(path, cache, [0])
    return _pdf_text_from_cache(path, cache, [0])


def extract_pdf_text(
    path: Path,
    *,
    first_page: _PdfText | None = None,
) -> _PdfText:
    cache = first_page.cache if first_page and first_page.cache else _open_pdf_cache(path)
    page_indexes = _preferred_ocr_pages(cache.page_count)
    _read_selectable_pages(cache, page_indexes)
    _ocr_cached_pages(
        path,
        cache,
        [
            page_index
            for page_index in page_indexes
            if _needs_ocr(cache.page_text.get(page_index, ""))
        ],
    )
    return _pdf_text_from_cache(path, cache, page_indexes)


def _open_pdf_cache(path: Path) -> _PdfReadCache:
    reader: object | None = None
    page_count = 0
    reader_error: Exception | None = None
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            reader.decrypt("")
        page_count = len(reader.pages)
    except Exception as exc:
        reader_error = exc

    if not page_count:
        try:
            page_count = _pdfium_page_count(path)
        except Exception:
            _close_pdf_read_cache(
                _PdfReadCache(
                    reader=reader,
                    page_count=0,
                    reader_error=reader_error,
                )
            )
            raise
    return _PdfReadCache(
        reader=reader,
        page_count=page_count,
        reader_error=reader_error,
    )


def _read_selectable_pages(cache: _PdfReadCache, page_indexes: Iterable[int]) -> None:
    if cache.reader is None:
        return
    for page_index in page_indexes:
        if page_index in cache.page_text:
            continue
        try:
            cache.page_text[page_index] = (
                cache.reader.pages[page_index].extract_text() or ""
            )
        except Exception:
            cache.page_text[page_index] = ""


def _ocr_cached_pages(
    path: Path,
    cache: _PdfReadCache,
    page_indexes: Iterable[int],
) -> None:
    candidates = [
        page_index
        for page_index in page_indexes
        if page_index not in cache.ocr_attempted
    ]
    if not candidates:
        return
    cache.ocr_attempted.update(candidates)
    ocr_text = _ocr_pdf_pages(path, candidates)
    for index, text in ocr_text.items():
        if _meaningful_text(text):
            cache.page_text[index] = "\n".join(
                value for value in (cache.page_text.get(index, ""), text) if value
            )


def _pdf_text_from_cache(
    path: Path,
    cache: _PdfReadCache,
    page_indexes: Iterable[int],
) -> _PdfText:
    indexes = list(page_indexes)
    combined = "\n\n".join(
        cache.page_text.get(index, "") for index in indexes
    ).strip()
    if not combined and cache.reader_error and not cache.page_count:
        raise cache.reader_error
    first_page_text = cache.page_text.get(0, "")
    return _PdfText(
        path=path,
        text=combined,
        application_form=is_application_form(path, combined),
        ocr_pages=len(cache.ocr_attempted),
        first_page_text=first_page_text,
        cache=cache,
    )


def _close_pdf_cache(document: _PdfText | None) -> None:
    cache = document.cache if document else None
    if cache:
        _close_pdf_read_cache(cache)


def _close_pdf_read_cache(cache: _PdfReadCache) -> None:
    if cache.reader is None:
        return
    stream = getattr(cache.reader, "stream", None)
    close = getattr(stream, "close", None)
    if callable(close):
        close()
    cache.reader = None


def is_application_form(path: Path, text: str) -> bool:
    filename = path.stem.casefold().replace("+", " ")
    if any(marker in filename for marker in APPLICATION_FORM_FILENAME_MARKERS):
        return True
    folded = text.casefold()
    return sum(marker in folded for marker in APPLICATION_FORM_TEXT_MARKERS) >= 2


def extract_application_form_parties(text: str) -> tuple[_Party | None, _Party | None]:
    lines = _text_lines(text)
    if not lines:
        return None, None
    agent_index = next(
        (index for index, line in enumerate(lines) if _normalise_label(line) == "agent details"),
        None,
    )
    if agent_index is None:
        return None, None

    agent_end = len(lines)
    for index in range(agent_index + 1, len(lines)):
        folded = lines[index].casefold()
        if any(folded.startswith(prefix) for prefix in FORM_STOP_PREFIXES):
            agent_end = index
            break

    applicant_start = 0
    for index in range(agent_index):
        if _normalise_label(lines[index]) == "name company":
            applicant_start = index
    applicant = _parse_form_party(lines[applicant_start:agent_index])
    agent = _parse_form_party(lines[agent_index + 1:agent_end])
    return (applicant if applicant.display_name or applicant.address else None), (
        agent if agent.display_name or agent.address else None
    )


def extract_professional_details(text: str, filename: str, accumulator: _Accumulator) -> None:
    lines = _text_lines(text)
    if not lines:
        return

    for index, line in enumerate(lines):
        has_contact_evidence = _title_block_contact_evidence(lines, index)
        excluded_context = _client_context(lines, index) or _authority_context(lines, index)
        if PROFESSIONAL_CREDENTIAL_RE.search(line):
            credentialled_name = _name_before_credentials(lines, index)
            if credentialled_name and not excluded_context:
                accumulator.add_name(credentialled_name)
        labelled = PROFESSIONAL_LABEL_RE.match(line)
        if labelled and _valid_labelled_name(labelled.group(1)) and not excluded_context:
            accumulator.add_name(labelled.group(1))
        if _looks_like_company(line) and has_contact_evidence and not excluded_context:
            accumulator.add_name(line)

        for email in EMAIL_RE.findall(line):
            if has_contact_evidence and not excluded_context:
                accumulator.add_email(email)
                company = _nearest_company(lines, index)
                if company:
                    accumulator.add_name(company)

        for phone_match in PHONE_RE.finditer(line):
            if has_contact_evidence and not excluded_context:
                accumulator.add_phone(phone_match.group(0))
                company = _nearest_company(lines, index)
                if company:
                    accumulator.add_name(company)

        if POSTCODE_RE.search(line):
            address = _address_around_postcode(lines, index)
            if (
                address
                and has_contact_evidence
                and not excluded_context
            ):
                accumulator.add_address(address)


def _parse_form_party(lines: list[str]) -> _Party:
    first_name = _form_field(lines, "first name")
    surname = _form_field(lines, "surname", "last name")
    person_name = " ".join(part for part in (first_name, surname) if part)
    company_name = _form_field(lines, "company name", "company")
    if not person_name and not company_name:
        combined = _form_field(lines, "name/company")
        if combined:
            company_name = combined
    address_parts = [
        _form_field(lines, "address line 1", "address 1"),
        _form_field(lines, "address line 2", "address 2"),
        _form_field(lines, "address line 3", "address 3"),
        _form_field(lines, "town/city", "town", "city"),
        _form_field(lines, "county"),
        _form_field(lines, "country"),
        _form_field(lines, "postcode"),
    ]
    address = ", ".join(_unique_values(part for part in address_parts if part))
    return _Party(
        person_name=_clean_candidate(person_name),
        company_name=_clean_candidate(company_name),
        address=_clean_candidate(address),
    )


def _form_field(lines: list[str], *aliases: str) -> str:
    normalised_aliases = {_normalise_label(alias) for alias in aliases}
    for index, line in enumerate(lines):
        label, separator, remainder = line.partition(":")
        normalised_line = _normalise_label(label if separator else line)
        matched_alias = next(
            (
                alias
                for alias in normalised_aliases
                if normalised_line == alias or normalised_line.startswith(f"{alias} ")
            ),
            None,
        )
        if not matched_alias:
            continue
        if separator and _clean_candidate(remainder):
            return _clean_candidate(remainder)
        for candidate in lines[index + 1:index + 5]:
            value = _clean_candidate(candidate)
            label_value = _normalise_label(value)
            if not value or "optional" in label_value or label_value.startswith("planning portal reference"):
                continue
            if label_value in FORM_FIELD_LABELS or any(
                label_value.startswith(f"{field_label} ") for field_label in FORM_FIELD_LABELS
            ):
                return ""
            return value
    return ""


def _client_context(lines: list[str], index: int) -> bool:
    window, start = _contact_window(lines, index)
    role_labels: list[tuple[int, bool]] = []
    for offset, line in enumerate(window[: index - start + 1]):
        folded = line.casefold().strip()
        if _is_role_label(folded, CLIENT_ROLE_MARKERS):
            role_labels.append((start + offset, True))
        elif _is_role_label(folded, AUTHOR_ROLE_MARKERS):
            role_labels.append((start + offset, False))
    if role_labels:
        return role_labels[-1][1]

    window_text = " ".join(window).casefold()
    if "consultant" in window_text and any(
        marker in window_text for marker in ("project", "key notes", "keynote")
    ):
        return not any(marker in window_text for marker in AUTHOR_ROLE_MARKERS)
    return False


def _contact_window(lines: list[str], index: int) -> tuple[list[str], int]:
    start = max(0, index - 4)
    return lines[start:min(len(lines), index + 5)], start


def _has_professional_contact_evidence(lines: list[str], index: int) -> bool:
    window, _start = _contact_window(lines, index)
    text = " ".join(window).casefold()
    has_role = any(marker in text for marker in PROFESSIONAL_ROLE_MARKERS)
    has_company = any(_looks_like_company(line) for line in window)
    has_contact = bool(
        EMAIL_RE.search(text)
        or any(_normalise_phone(match.group(0)) for match in PHONE_RE.finditer(text))
        or POSTCODE_RE.search(text)
    )
    return has_role or (has_company and has_contact)


def _title_block_contact_evidence(lines: list[str], index: int) -> bool:
    if _has_professional_contact_evidence(lines, index):
        return True
    for anchor_index in range(max(0, index - 4), index):
        if _has_professional_contact_evidence(lines, anchor_index):
            return True
    return any(
        PROFESSIONAL_CREDENTIAL_RE.search(line)
        and _name_before_credentials(lines, credential_index)
        for credential_index, line in enumerate(lines[max(0, index - 5):index], start=max(0, index - 5))
    )


def _authority_context(lines: list[str], index: int) -> bool:
    window, _start = _contact_window(lines, index)
    text = " ".join(window).casefold()
    return any(
        marker in text
        for marker in ("planning authority", "local authority", "council", "case officer")
    )


def _is_role_label(value: str, markers: tuple[str, ...]) -> bool:
    normalized = _normalise_label(value)
    return any(
        normalized == _normalise_label(marker)
        or normalized.startswith(f"{_normalise_label(marker)} ")
        for marker in markers
    )


def _nearest_company(lines: list[str], index: int) -> str:
    candidates: list[tuple[int, str]] = []
    for candidate_index in range(max(0, index - 6), min(len(lines), index + 7)):
        candidate = lines[candidate_index]
        if (
            _looks_like_company(candidate)
            and _has_professional_contact_evidence(lines, candidate_index)
            and not _client_context(lines, candidate_index)
            and not _authority_context(lines, candidate_index)
        ):
            candidates.append((abs(candidate_index - index), candidate))
    return min(candidates, default=(0, ""))[1]


def _address_around_postcode(lines: list[str], index: int) -> str:
    postcode_line = lines[index]
    folded_postcode_line = postcode_line.casefold()
    if any(
        marker in folded_postcode_line
        for marker in (
            "site address",
            "site location",
            "application site",
            "project:",
            "project ",
            "revision",
            "scale:",
            "proposal",
            "drawing",
            "telephone:",
            "tel:",
            " t:",
            " e:",
        )
    ):
        return ""
    if len(postcode_line) > 100 or len(postcode_line.split()) > 15:
        return ""
    if ADDRESS_WORD_RE.search(postcode_line) and len(postcode_line.split()) >= 2:
        return _clean_candidate(postcode_line)

    preceding: list[str] = []
    for line in reversed(lines[max(0, index - 5):index]):
        value = _clean_candidate(line)
        if not value or len(value) > 70 or len(value.split()) > 9:
            break
        if EMAIL_RE.search(value) or PHONE_RE.search(value):
            break
        if PROFESSIONAL_LABEL_RE.match(value) or _looks_like_company(value):
            break
        if PROFESSIONAL_CREDENTIAL_RE.search(value):
            break
        if value.endswith(".") and len(value.split()) > 7:
            break
        if any(
            marker in value.casefold()
            for marker in ("site address", "site location", "project", "revision", "scale", "proposal")
        ):
            break
        preceding.append(value)
        if len(preceding) == 4:
            break
    preceding.reverse()
    address_parts = preceding + [_clean_candidate(postcode_line)]
    address = ", ".join(_unique_values(address_parts))
    if not ADDRESS_WORD_RE.search(address) and len(preceding) < 2:
        return ""
    return address


def _looks_like_company(value: str) -> bool:
    value = _clean_candidate(value)
    if (
        not value
        or _is_name_noise(value)
        or len(value) > 100
        or len(value.split()) > 12
        or EMAIL_RE.search(value)
        or PHONE_RE.search(value)
    ):
        return False
    if _is_generic_company_heading(value):
        return False
    if not COMPANY_END_RE.search(value):
        return False
    words = re.findall(r"[A-Za-z][A-Za-z'&-]*", value)
    if not words:
        return False
    title_words = sum(
        word[0].isupper() or word.isupper() or word.casefold() in {"and", "of", "the"}
        for word in words
    )
    return (title_words / len(words)) >= 0.6


def _valid_labelled_name(value: str) -> bool:
    value = _clean_candidate(value)
    if not value or _is_name_noise(value) or len(value) > 100 or len(value.split()) > 12:
        return False
    if EMAIL_RE.search(value) or PHONE_RE.search(value):
        return False
    return not any(
        marker in value.casefold()
        for marker in ("information relating", "all other relevant", "best practice", "the council")
    )


def _is_name_noise(value: str) -> bool:
    folded = _clean_candidate(value).casefold()
    if not folded or any(marker in folded for marker in NAME_NOISE_MARKERS):
        return True
    if len(re.sub(r"[^a-z0-9]", "", folded)) <= 2:
        return True
    if folded in {"studio", "group limited", "group ltd"}:
        return True
    if folded.startswith("of "):
        return True
    if re.search(r"(?:^|\s)rev(?:ision)?\s*[:\-]", folded):
        return True
    if re.search(r"(?:^|\s)date\s*[:\-]", folded):
        return True
    labels = re.findall(
        r"\b(?:(?:checked|approved|drawn)\s*by|scale|rev(?:ision)?|date)\s*:",
        folded,
    )
    return len(labels) >= 2


def _name_before_credentials(lines: list[str], index: int) -> str:
    parts: list[str] = []
    for value in reversed(lines[max(0, index - 4):index]):
        value = _clean_candidate(value)
        if not value or len(value.split()) > 4 or not re.fullmatch(r"[A-Za-z .'-]+", value):
            break
        if PROFESSIONAL_CREDENTIAL_RE.fullmatch(value):
            continue
        if value.casefold() in {"architect", "architects", "consultant", "director"}:
            break
        if not all(word[0].isupper() or word.isupper() for word in value.split() if word):
            break
        parts.append(value)
        if sum(len(part.split()) for part in parts) >= 3:
            break
    parts.reverse()
    name = " ".join(parts)
    return name if 2 <= len(name.split()) <= 4 else ""


def _is_generic_company_heading(value: str) -> bool:
    folded = _normalise_value(value)
    if folded in GENERIC_COMPANY_HEADINGS:
        return True
    return any(
        phrase in folded
        for phrase in (
            "application for planning",
            "town and country planning",
            "description of proposed",
            "design and access statement",
        )
    )


def _preferred_ocr_pages(page_count: int) -> list[int]:
    if page_count <= MAX_OCR_PAGES_PER_DOCUMENT:
        return list(range(page_count))
    first_count = MAX_OCR_PAGES_PER_DOCUMENT - 2
    return list(range(first_count)) + [page_count - 2, page_count - 1]


def _ocr_pdf_pages(path: Path, page_indexes: Iterable[int]) -> dict[int, str]:
    indexes = list(page_indexes)
    if not indexes:
        return {}
    import pypdfium2 as pdfium

    engine = _get_ocr_engine()
    pdf = pdfium.PdfDocument(path)
    text_by_page: dict[int, str] = {}
    try:
        for page_index in indexes:
            try:
                page = pdf[page_index]
                try:
                    bitmap = page.render(scale=OCR_RENDER_SCALE)
                    try:
                        image = bitmap.to_numpy()
                        with _OCR_ENGINE_LOCK:
                            result = engine(image, use_cls=False)
                        lines = getattr(result, "txts", None) or ()
                        text_by_page[page_index] = "\n".join(str(line) for line in lines)
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            except Exception:
                continue
    finally:
        pdf.close()
    return text_by_page


def _get_ocr_engine():
    global _OCR_ENGINE
    with _OCR_ENGINE_LOCK:
        if _OCR_ENGINE is None:
            from rapidocr import RapidOCR

            _OCR_ENGINE = RapidOCR()
        return _OCR_ENGINE


def _pdfium_page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(path)
    try:
        return len(pdf)
    finally:
        pdf.close()


def _is_pdf(path: Path) -> bool:
    if path.suffix.casefold() == ".pdf":
        return True
    try:
        with path.open("rb") as handle:
            return handle.read(5) == b"%PDF-"
    except OSError:
        return False


def _meaningful_text(text: str) -> bool:
    return sum(character.isalnum() for character in text) >= MIN_SELECTABLE_PAGE_CHARACTERS


def _needs_ocr(text: str) -> bool:
    character_count = sum(character.isalnum() for character in text)
    if character_count < MIN_SELECTABLE_PAGE_CHARACTERS:
        return True
    if character_count >= 200:
        return False
    folded = f" {text.casefold()} "
    return not (
        EMAIL_RE.search(text)
        or PHONE_RE.search(text)
        or POSTCODE_RE.search(text)
        or any(marker in folded for marker in PROFESSIONAL_ROLE_MARKERS)
    )


def _blocked_email(value: str) -> bool:
    if value.count("@") != 1 or not re.fullmatch(
        r"(?i)[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", value
    ):
        return True
    local_part, domain = value.rsplit("@", 1)
    if re.match(r"\d{3,}", local_part):
        return True
    if re.search(
        r"(?i)(?:\.(?:co|org|ac|gov)\.uk|\.(?:com|net|org))[a-z0-9]",
        domain,
    ):
        return True
    public_suffixes = re.findall(
        r"(?i)\.(?:co\.uk|org\.uk|ac\.uk|gov\.uk|com|net|org)\b",
        domain,
    )
    if len(public_suffixes) > 1:
        return True
    return any(domain.endswith(blocked) for blocked in BLOCKED_EMAIL_DOMAINS)


def _normalise_phone(value: str) -> str:
    parenthesis_depth = 0
    for character in value:
        if character == "(":
            parenthesis_depth += 1
        elif character == ")":
            if parenthesis_depth == 0:
                return ""
            parenthesis_depth -= 1
    if parenthesis_depth:
        return ""
    if re.search(r"\b\d{4,6}\.\d{3,}\b", value):
        return ""
    if re.search(r"(?:^|\s)0(?:[\s./-]+0){2,}(?:\s|$)", value):
        return ""
    if re.fullmatch(r"\s*\d{1,2}[./-]\d{1,6}[./-]\d{1,4}\s*", value):
        return ""
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00440"):
        digits = digits[4:]
    elif digits.startswith("0044"):
        digits = "0" + digits[4:]
    elif digits.startswith("440"):
        digits = digits[2:]
    elif digits.startswith("44"):
        digits = "0" + digits[2:]
    if len(digits) not in {10, 11} or not digits.startswith(("01", "02", "03", "07", "08")):
        return ""
    if digits.startswith("02") and digits[:3] not in {"020", "023", "024", "028", "029"}:
        return ""
    return re.sub(r"\s+", " ", value).strip(" .,;:-")


def _clean_professional_address(value: str | None) -> str:
    value = _clean_candidate(value)
    value = re.sub(r"(?i)\bstudi0\b", "Studio", value)
    postcode_match = POSTCODE_RE.search(value)
    if not value or not postcode_match:
        return ""
    address = value[:postcode_match.end()].strip(" ,;:-")
    folded = address.casefold()
    if folded.startswith("of "):
        return ""
    if re.match(r"^(?:architects?|architecture|interiors?)\s*[+,;:]", folded):
        return ""
    if any(
        marker in folded
        for marker in (
            "copyright",
            "all rights reserved",
            "this drawing",
            "property of",
            "not copied",
            "construction",
            "issued on",
            "http://",
            "https://",
            "landscape plan",
        )
    ):
        return ""
    if re.search(r"(?i)\b(?:www|tel(?:ephone)?)\b", address):
        return ""
    if re.search(r"(?i)\b(?:phone|email|web)\s*:", address):
        return ""
    if EMAIL_RE.search(address) or PHONE_RE.search(address):
        return ""
    if not ADDRESS_WORD_RE.search(address) and address.count(",") < 2:
        return ""
    return address


def _similar_ocr_email(left: str, right: str) -> bool:
    if left == right:
        return True
    left_local, left_domain = left.rsplit("@", 1)
    right_local, right_domain = right.rsplit("@", 1)
    if left_domain != right_domain:
        return False
    if abs(len(left_local) - len(right_local)) == 1:
        longer, shorter = sorted((left_local, right_local), key=len, reverse=True)
        return longer[0] in {"e", "i", "l", "o", "0"} and longer[1:] == shorter
    if len(left_local) != len(right_local):
        return False
    differences = [
        index
        for index, (left_character, right_character) in enumerate(
            zip(left_local, right_local)
        )
        if left_character != right_character
    ]
    if differences != [0]:
        return False
    return frozenset((left_local[0], right_local[0])) in {
        frozenset(("m", "n")),
        frozenset(("i", "l")),
        frozenset(("o", "0")),
    }


def _prefer_shorter_ocr_email(candidate: str, existing: str) -> bool:
    candidate_local, candidate_domain = candidate.rsplit("@", 1)
    existing_local, existing_domain = existing.rsplit("@", 1)
    return (
        candidate_domain == existing_domain
        and len(candidate_local) + 1 == len(existing_local)
        and existing_local[0] in {"e", "i", "l", "o", "0"}
        and existing_local[1:] == candidate_local
    )


def _text_lines(text: str) -> list[str]:
    return [value for line in text.splitlines() if (value := _clean_candidate(line))]


def _clean_candidate(value: str | None) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(" \t\r\n|,;:")
    if not value or "redacted" in value.casefold() or value.casefold() in {"n/a", "none", "not applicable"}:
        return ""
    return value


def _normalise_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _normalise_value(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()


def _postcodes(value: str) -> list[str]:
    return [re.sub(r"\s+", "", match.group(0)).casefold() for match in POSTCODE_RE.finditer(value)]


def _meaningful_address_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) > 1 and token not in ADDRESS_STOP_TOKENS
    }


def _similar_site_address(candidate: str, excluded: str) -> bool:
    left = _meaningful_address_tokens(candidate)
    right = _meaningful_address_tokens(excluded)
    shared = left & right
    union = left | right
    return len(shared) >= 3 and bool(union) and len(shared) / len(union) >= 0.65


def _same_value(left: str, right: str) -> bool:
    left_key = _normalise_value(left)
    right_key = _normalise_value(right)
    if not left_key or not right_key:
        return False
    if left_key == right_key:
        return True
    return min(len(left_key), len(right_key)) >= 8 and (left_key in right_key or right_key in left_key)


def _similar_company_name(left: str, right: str) -> bool:
    left_values = [left, *re.findall(r"\(([^)]+)\)", left)]
    right_values = [right, *re.findall(r"\(([^)]+)\)", right)]
    for left_value in left_values:
        if not _looks_like_company(left_value):
            continue
        left_key = re.sub(r"[^a-z0-9]", "", left_value.casefold())
        for right_value in right_values:
            if not _looks_like_company(right_value):
                continue
            right_key = re.sub(r"[^a-z0-9]", "", right_value.casefold())
            if min(len(left_key), len(right_key)) >= 10 and SequenceMatcher(None, left_key, right_key).ratio() >= 0.82:
                return True
    return False


def _append_unique(values: list[str], value: str) -> None:
    if not any(_same_value(value, existing) for existing in values):
        values.append(value)


def _unique_values(values: Iterable[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value:
            _append_unique(unique, value)
    return unique


def _joined_or_failed(values: list[str]) -> str:
    return "; ".join(values) if values else FAILED_ENRICHMENT_VALUE
