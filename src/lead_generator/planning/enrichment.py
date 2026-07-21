from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Callable, Iterable

from pypdf import PdfReader


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
)
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
            for excluded in self.addresses
        )


class _Accumulator:
    def __init__(self, exclusions: _Exclusions) -> None:
        self.result = ContactEnrichment()
        self.exclusions = exclusions

    def add_name(self, value: str | None) -> None:
        value = _clean_candidate(value)
        if not value or self.exclusions.matches_party(value) or _is_generic_company_heading(value):
            return
        if any(
            _same_value(value, existing) or _similar_company_name(value, existing)
            for existing in self.result.architect_company_names
        ):
            return
        self.result.architect_company_names.append(value)

    def add_phone(self, value: str | None) -> None:
        value = _normalise_phone(value or "")
        if value and not any(
            re.sub(r"\D", "", value) == re.sub(r"\D", "", existing)
            for existing in self.result.phone_numbers
        ):
            self.result.phone_numbers.append(value)

    def add_email(self, value: str | None) -> None:
        value = (value or "").strip(" <>.,;:").casefold()
        if value.startswith("email-"):
            value = value.removeprefix("email-")
        if value and not _blocked_email(value):
            _append_unique(self.result.email_addresses, value)

    def add_address(self, value: str | None) -> None:
        value = _clean_candidate(value)
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

    documents: list[_PdfText] = []
    if not pdf_paths and log:
        log("No downloaded PDFs were available to enrich")
    for path in pdf_paths:
        try:
            if log:
                log(f"Reading {path.name} for professional contact details")
            document = extract_pdf_text(path)
            documents.append(document)
            if document.ocr_pages and log:
                log(f"OCR read {document.ocr_pages} page(s) from {path.name}")
        except Exception as exc:  # pragma: no cover - malformed live documents vary widely
            if log:
                log(f"Could not read {path.name} for enrichment: {exc}")

    agent_parties: list[_Party] = []
    for document in documents:
        if not document.application_form:
            continue
        applicant, agent = extract_application_form_parties(document.text)
        if applicant:
            for value in applicant.exclusion_values():
                if value == applicant.address:
                    exclusions.add_address(value)
                else:
                    exclusions.add_party(value)
        if agent and agent.display_name:
            agent_parties.append(agent)

    accumulator = _Accumulator(exclusions)
    for party in agent_parties:
        accumulator.add_name(party.display_name)
        accumulator.add_address(party.address)
    if agent_name:
        accumulator.add_name(agent_name)

    for document in documents:
        if document.application_form:
            # Phone and email details from application forms are intentionally never used.
            continue
        extract_professional_details(document.text, document.path.name, accumulator)

    return accumulator.result


def extract_pdf_text(path: Path) -> _PdfText:
    page_text: dict[int, str] = {}
    page_count = 0
    reader_error: Exception | None = None
    try:
        reader = PdfReader(path, strict=False)
        if reader.is_encrypted:
            reader.decrypt("")
        page_count = len(reader.pages)
        for page_index, page in enumerate(reader.pages):
            try:
                page_text[page_index] = page.extract_text() or ""
            except Exception:
                page_text[page_index] = ""
    except Exception as exc:
        reader_error = exc

    if not page_count:
        page_count = _pdfium_page_count(path)
    ocr_candidates = [
        index
        for index in _preferred_ocr_pages(page_count)
        if _needs_ocr(page_text.get(index, ""))
    ]
    ocr_text = _ocr_pdf_pages(path, ocr_candidates) if ocr_candidates else {}
    for index, text in ocr_text.items():
        if _meaningful_text(text):
            page_text[index] = "\n".join(
                value for value in (page_text.get(index, ""), text) if value
            )
    combined = "\n\n".join(page_text.get(index, "") for index in range(page_count)).strip()
    if not combined and reader_error:
        raise reader_error
    return _PdfText(
        path=path,
        text=combined,
        application_form=is_application_form(path, combined),
        ocr_pages=len(ocr_text),
    )


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
        if PROFESSIONAL_CREDENTIAL_RE.search(line):
            credentialled_name = _name_before_credentials(lines, index)
            if credentialled_name and not _client_context(lines, index):
                accumulator.add_name(credentialled_name)
        labelled = PROFESSIONAL_LABEL_RE.match(line)
        if labelled and _valid_labelled_name(labelled.group(1)) and not _client_context(lines, index):
            accumulator.add_name(labelled.group(1))
        if _looks_like_company(line) and _professional_context_score(lines, index, filename) >= 2:
            accumulator.add_name(line)

        for email in EMAIL_RE.findall(line):
            domain = email.rsplit("@", 1)[-1].casefold()
            score = _professional_context_score(lines, index, filename)
            if domain not in FREE_EMAIL_DOMAINS:
                score += 2
            if score >= 3 and not _client_context(lines, index):
                accumulator.add_email(email)
                company = _nearest_company(lines, index)
                if company:
                    accumulator.add_name(company)

        for phone_match in PHONE_RE.finditer(line):
            if _professional_context_score(lines, index, filename) >= 3 and not _client_context(lines, index):
                accumulator.add_phone(phone_match.group(0))
                company = _nearest_company(lines, index)
                if company:
                    accumulator.add_name(company)

        if POSTCODE_RE.search(line):
            address = _address_around_postcode(lines, index)
            if (
                address
                and _professional_context_score(lines, index, filename) >= 2
                and not _client_context(lines, index)
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


def _professional_context_score(lines: list[str], index: int, filename: str) -> int:
    nearby = " ".join(lines[max(0, index - 7):min(len(lines), index + 8)]).casefold()
    close = " ".join(lines[max(0, index - 2):index + 1]).casefold()
    score = 0
    if any(marker in nearby for marker in PROFESSIONAL_ROLE_MARKERS):
        score += 4
    if any(_looks_like_company(line) for line in lines[max(0, index - 5):min(len(lines), index + 6)]):
        score += 3
    if any(marker in filename.casefold() for marker in ("drawing", "plan", "statement", "report", "letter")):
        score += 1
    if any(marker in close for marker in CLIENT_ROLE_MARKERS):
        score -= 7
    if any(marker in close for marker in ("planning authority", "council", "planning portal", "case officer")):
        score -= 6
    return score


def _client_context(lines: list[str], index: int) -> bool:
    close_lines = lines[max(0, index - 2):index + 1]
    for line in close_lines:
        folded = line.casefold().strip()
        if any(re.match(rf"^{re.escape(marker)}\s*[:\-]", folded) for marker in CLIENT_ROLE_MARKERS):
            return True
    return False


def _nearest_company(lines: list[str], index: int) -> str:
    candidates: list[tuple[int, str]] = []
    for candidate_index in range(max(0, index - 6), min(len(lines), index + 7)):
        candidate = lines[candidate_index]
        if _looks_like_company(candidate) and not _client_context(lines, candidate_index):
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
    if not value or len(value) > 100 or len(value.split()) > 12:
        return False
    if EMAIL_RE.search(value) or PHONE_RE.search(value):
        return False
    return not any(
        marker in value.casefold()
        for marker in ("information relating", "all other relevant", "best practice", "the council")
    )


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
    if "@" not in value:
        return True
    domain = value.rsplit("@", 1)[-1]
    return any(domain.endswith(blocked) for blocked in BLOCKED_EMAIL_DOMAINS)


def _normalise_phone(value: str) -> str:
    value = re.sub(r"\s+", " ", value).strip(" .,;:-")
    if re.search(r"\b\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\b", value):
        return ""
    digits = re.sub(r"\D", "", value)
    if value.startswith("+44"):
        valid_length = 12 <= len(digits) <= 13
    elif value.startswith("0044"):
        valid_length = 14 <= len(digits) <= 15
    else:
        valid_length = 10 <= len(digits) <= 11
    return value if valid_length else ""


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
