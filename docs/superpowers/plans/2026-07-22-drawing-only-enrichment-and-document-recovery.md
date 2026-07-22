# Drawing-Only Enrichment and Document Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Restrict architect enrichment to proposed or existing drawing sheets, eliminate junk contact values, recover transiently missed application documents, and correct every row in the 21 July run.

**Architecture:** A focused `drawing_sources` module will make deterministic document-eligibility decisions shared by downloading and enrichment. Enrichment will process only eligible PDFs and attach field-level source evidence, while document discovery will return explicit success and failure state so partial or transient discovery enters the existing final retry queue. A recovery module will reconstruct the 85 captured applications, fill missing document folders, rerun enrichment from scratch, and write corrected and audit CSV files without altering the original CSV.

**Tech Stack:** Python 3.11+, `dataclasses`, `pathlib`, `urllib`, `lxml`, `pypdf`, RapidOCR, `unittest`/`pytest`, PyInstaller.

## Global Constraints

- Architect details may come only from proposed or existing plans, drawings, elevations, sections, or layouts.
- Existing-only drawing files are allowed; all other existing-only documents remain excluded.
- Application forms, portal agent metadata, reports, statements, assessments, surveys, letters, notices, certificates, schedules, and consultations contribute no enrichment output.
- Never substitute a junk value for a missing field; retain `Failed` independently for each unavailable field.
- Preserve valid partial enrichment and record the eligible source document for every populated field.
- Preserve all successfully downloaded files and retry transient or partial discovery only after the first document queue drains.
- Keep council search failures separate from document failures.
- Preserve the original 21 July `applications.csv`; write corrected and audit files beside it.
- Do not add third-party dependencies.
- Do not modify or stage the existing untracked build and distribution directories.

## File Map

- `src/lead_generator/planning/drawing_sources.py`: owns drawing eligibility and the existing-only drawing exception shared by downloading and enrichment.
- `src/lead_generator/planning/enrichment.py`: extracts validated title-block contacts and records per-field evidence without changing CSV columns.
- `src/lead_generator/planning/leads.py`: owns document source discovery, partial-failure state, end-of-queue retry, council-specific document URLs, and download filtering.
- `src/lead_generator/planning/recovery.py`: reconstructs captured applications, repairs one completed output folder, and writes corrected and audit CSV files.
- `tests/test_enrichment.py`: covers source gating, client exclusion, and contact validation regressions.
- `tests/test_leads.py`: covers discovery state, retry orchestration, council document sources, and existing-only drawing downloads.
- `tests/test_recovery.py`: covers reconstruction, idempotence, deferred recovery, output preservation, and audit reasons.
- `dist/PlanningLeadGenerator.exe`: packaged Windows application rebuilt only after source tests and supplied-run audit pass.

---

### Task 1: Classify Eligible Drawing Sources

**Files:**
- Create: `src/lead_generator/planning/drawing_sources.py`
- Modify: `tests/test_enrichment.py`
- Modify: `tests/test_leads.py`

**Interfaces:**
- Produces: `DrawingSourceDecision(eligible: bool, needs_text: bool, reason: str)`.
- Produces: `preclassify_drawing_source(filename: str) -> DrawingSourceDecision`.
- Produces: `classify_drawing_source(filename: str, text: str) -> DrawingSourceDecision`.
- Produces: `is_existing_only_drawing_metadata(value: str) -> bool` for the downloader.

- [ ] **Step 1: Write failing source-classification tests**

Add `import pytest` and import `classify_drawing_source` plus `is_existing_only_drawing_metadata` from the new module. Then add parameterized tests covering the approved boundary:

```python
@pytest.mark.parametrize(
    ("filename", "text", "eligible"),
    [
        ("Proposed Elevations.pdf", "", True),
        ("Existing Site Plan.pdf", "", True),
        ("Existing and Proposed Plans.pdf", "", True),
        ("PROPOSED_CAR_PARK.pdf", "DRAWING NUMBER 2411039 SCALE 1:500 REV G", True),
        ("PROPOSED_CAR_PARK.pdf", "Car park design summary", False),
        ("2 Drawings.pdf", "PROPOSED GATE DRAWING NUMBER 102 SCALE 1:50", True),
        ("2 Drawings.pdf", "Two drawings are enclosed", False),
        ("Drawings.pdf", "Proposed design discussed below", False),
        ("Existing Planning Statement.pdf", "DRAWING NUMBER 1 SCALE 1:100", False),
        ("Design and Access Statement.pdf", "Proposed plans are described below", False),
        ("Application Form.pdf", "Proposed elevation", False),
        ("Site Location Plan.pdf", "LOCATION PLAN SCALE 1:1250", False),
    ],
)
def test_drawing_source_boundary(filename: str, text: str, eligible: bool) -> None:
    assert classify_drawing_source(filename, text).eligible is eligible
```

Add downloader metadata assertions:

```python
assert is_existing_only_drawing_metadata("Existing elevations.pdf")
assert not is_existing_only_drawing_metadata("Existing survey report.pdf")
assert not is_existing_only_drawing_metadata("Existing planning statement.pdf")
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_enrichment.py tests/test_leads.py -k "drawing_source_boundary or existing_only_drawing_metadata" -q
```

Expected: FAIL because `drawing_sources.py` and its interfaces do not exist.

- [ ] **Step 3: Implement the deterministic classifier**

Create immutable marker sets and normalize to whole tokens so `plan` does not match `planning`. Narrative markers must take priority over drawing markers.

```python
@dataclass(frozen=True, slots=True)
class DrawingSourceDecision:
    eligible: bool
    needs_text: bool
    reason: str


STATUS_TOKENS = {"proposed", "existing"}
DRAWING_TOKENS = {
    "plan", "plans", "drawing", "drawings", "elevation", "elevations",
    "section", "sections", "layout", "layouts",
}
NARRATIVE_PHRASES = (
    "application form", "planning statement", "design and access statement",
    "supporting statement", "report", "assessment", "survey", "letter",
    "notice", "certificate", "schedule", "specification", "consultation",
    "photograph", "photos", "method statement", "heritage statement",
)
DRAWING_EVIDENCE_RE = re.compile(
    r"(?i)\b(?:drawing\s*(?:no|number)|scale|revision|rev|drawn\s+by|checked\s+by)\b"
)
DRAWING_CODE_RE = re.compile(r"(?i)^(?=.*\d)[a-z0-9][a-z0-9._ -]{2,}$")
CLASSIFICATION_TEXT_LIMIT = 12_000


def _phrase_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _has_narrative_marker(value: str) -> bool:
    normalized = _phrase_text(value)
    return any(phrase in normalized for phrase in NARRATIVE_PHRASES)


def preclassify_drawing_source(filename: str) -> DrawingSourceDecision:
    name = Path(filename).stem
    if _has_narrative_marker(name):
        return DrawingSourceDecision(False, False, "narrative document title")
    tokens = _tokens(name)
    has_status = bool(tokens & STATUS_TOKENS)
    has_drawing_type = bool(tokens & DRAWING_TOKENS)
    if has_status and has_drawing_type:
        return DrawingSourceDecision(True, False, "drawing status and type in title")
    if has_status or has_drawing_type or DRAWING_CODE_RE.fullmatch(name.strip()):
        return DrawingSourceDecision(False, True, "PDF title-block confirmation required")
    return DrawingSourceDecision(False, False, "not a proposed or existing drawing")


def classify_drawing_source(filename: str, text: str) -> DrawingSourceDecision:
    title_text = "\n".join(text.splitlines()[:30])[:3_000]
    bounded_text = "\n".join(
        (text[:CLASSIFICATION_TEXT_LIMIT], text[-CLASSIFICATION_TEXT_LIMIT:])
    )
    combined = f"{Path(filename).stem}\n{bounded_text}"
    if _has_narrative_marker(Path(filename).stem) or _has_narrative_marker(title_text):
        return DrawingSourceDecision(False, False, "narrative document marker")
    filename_tokens = _tokens(Path(filename).stem)
    tokens = _tokens(combined)
    has_status = bool(tokens & STATUS_TOKENS)
    clear_filename = bool(filename_tokens & STATUS_TOKENS) and bool(
        filename_tokens & DRAWING_TOKENS
    )
    evidence = {
        match.group(0).casefold()
        for match in DRAWING_EVIDENCE_RE.finditer(bounded_text)
    }
    eligible = clear_filename or (has_status and len(evidence) >= 2)
    reason = "eligible drawing" if eligible else "drawing status/type evidence incomplete"
    return DrawingSourceDecision(eligible, False, reason)


def is_existing_only_drawing_metadata(value: str) -> bool:
    tokens = _tokens(value)
    return (
        "existing" in tokens
        and "proposed" not in tokens
        and bool(tokens & DRAWING_TOKENS)
        and not _has_narrative_marker(value)
    )
```

Keep the tests as the authority for boundary behavior. The classifier must use whole tokens, preserve narrative priority, and never treat `planning` as the drawing token `plan`.

- [ ] **Step 4: Run classifier tests and verify GREEN**

Run the command from Step 2.

Expected: PASS.

- [ ] **Step 5: Commit the classifier**

```powershell
git add -- src/lead_generator/planning/drawing_sources.py tests/test_enrichment.py tests/test_leads.py
git commit -m "Add proposed and existing drawing source classifier"
```

### Task 2: Restrict and Harden Contact Extraction

**Files:**
- Modify: `src/lead_generator/planning/enrichment.py`
- Modify: `tests/test_enrichment.py`

**Interfaces:**
- Consumes: drawing-source functions from Task 1.
- Extends: `ContactEnrichment` with `field_sources`, `eligible_documents`, `unreadable_documents`, and `rejected_documents` audit state while preserving `to_csv_row()`.
- Preserves: `enrich_application_folder(folder, applicant_name=None, agent_name=None, site_address=None, log=None) -> ContactEnrichment`; `agent_name` remains accepted for compatibility but is never copied into output.

- [ ] **Step 1: Replace form/report expectations with failing drawing-only tests**

Update the existing application-form tests so a form and a design statement contribute nothing, while proposed and existing drawings remain eligible:

```python
def test_enrichment_uses_only_proposed_or_existing_drawings() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        paths = {
            name: folder / name
            for name in (
                "APPLICATION_FORM.pdf",
                "Design and Access Statement.pdf",
                "Existing Elevations.pdf",
                "Proposed Plan.pdf",
            )
        }
        for path in paths.values():
            path.touch()
        documents = {
            "APPLICATION_FORM.pdf": _fake_pdf(
                paths["APPLICATION_FORM.pdf"], APPLICATION_FORM_TEXT, application_form=True
            ),
            "Design and Access Statement.pdf": _fake_pdf(
                paths["Design and Access Statement.pdf"],
                PROFESSIONAL_REPORT_TEXT,
                application_form=False,
            ),
            "Existing Elevations.pdf": _fake_pdf(
                paths["Existing Elevations.pdf"],
                "EXISTING ELEVATIONS\nDRAWING NUMBER E01\nSCALE 1:100\n"
                "Architect: Existing Studio Architects Ltd",
                application_form=False,
            ),
            "Proposed Plan.pdf": _fake_pdf(
                paths["Proposed Plan.pdf"],
                "PROPOSED PLAN\nDRAWING NUMBER P01\nSCALE 1:50\n"
                "Proposed Design Architects Ltd\n020 7123 4567\n"
                "studio@proposed-design.co.uk\n12 Design Road\nLondon\nSW1A 1AA",
                application_form=False,
            ),
        }

        with patch.object(
            enrichment,
            "extract_pdf_text",
            side_effect=lambda path: documents[path.name],
        ):
            result = enrichment.enrich_application_folder(
                folder,
                applicant_name="Adam Client",
                agent_name="Portal Agent Ltd",
                site_address="1 Application Site Road, London, N1 1AA",
            )

    row = result.to_csv_row()
    assert row["Architect / Company Name"] == (
        "Existing Studio Architects Ltd; Proposed Design Architects Ltd"
    )
    assert row["Phone Number"] == "020 7123 4567"
    assert row["Email Address"] == "studio@proposed-design.co.uk"
    assert row["Company Address"] == "12 Design Road, London, SW1A 1AA"
    assert "Portal Agent" not in " ".join(row.values())
    assert "Studio Arc" not in " ".join(row.values())
```

Assert that passing `agent_name="Portal Agent Ltd"` does not add that value and that an application form alone produces four `Failed` fields.

Add an independent-field regression using one eligible drawing that contains only an architect label and email:

```python
def test_partial_drawing_contact_keeps_only_available_fields() -> None:
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / "Proposed Elevations.pdf"
        path.touch()
        document = _fake_pdf(
            path,
            "PROPOSED ELEVATIONS\nDRAWING NUMBER A-201\nSCALE 1:100\n"
            "Architect: Example Studio Ltd\nstudio@example.co.uk",
            application_form=False,
        )
        with patch.object(enrichment, "extract_pdf_text", return_value=document):
            row = enrichment.enrich_application_folder(folder).to_csv_row()

    assert row == {
        "Architect / Company Name": "Example Studio Ltd",
        "Phone Number": "Failed",
        "Email Address": "studio@example.co.uk",
        "Company Address": "Failed",
    }
```

- [ ] **Step 2: Add failing real-defect regressions**

Add compact text fixtures reproducing the observed errors:

```python
def test_decimal_coordinates_are_not_phone_numbers() -> None:
    assert enrichment._normalise_phone("064646.00001") == ""
    assert enrichment._normalise_phone("0.0306 0.557 0") == ""


def test_copyright_and_drawing_labels_are_not_names() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())
    enrichment.extract_professional_details(
        "XL Planning LTD\n01884 38662\ninfo@xlplanning.co.uk\n"
        "1 Fore Street\nCullompton\nDevon\nEX15 1JW\n"
        "ALL RIGHTS RESERVED Copyright (c) 2026 XL PLANNING LIMITED.\n"
        "CHECKED BY: APPROVED BY: SCALE: REV: DATE",
        "Proposed Block Plan.pdf",
        accumulator,
    )
    row = accumulator.result.to_csv_row()
    assert row["Architect / Company Name"] == "XL Planning LTD"
    assert "COPYRIGHT" not in row["Architect / Company Name"].upper()
```

Add the title-block and address regressions explicitly:

```python
def test_client_company_and_coordinates_are_rejected_from_drawing() -> None:
    accumulator = enrichment._Accumulator(enrichment._Exclusions())
    enrichment.extract_professional_details(
        "PROPOSED CAR PARK\nDRAWING NUMBER 2411039-18G\nSCALE 1:500\n"
        "CLIENT\nCroudace Homes Ltd\nPROJECT\nLand at Hitcham Farm\n"
        "064646.00001\n065898.00001\n066916.00001",
        "2411039-18G - PROPOSED CAR PARK.pdf",
        accumulator,
    )

    row = accumulator.result.to_csv_row()
    assert row["Architect / Company Name"] == "Failed"
    assert row["Phone Number"] == "Failed"


def test_site_address_similarity_rejects_reformatted_site_address() -> None:
    exclusions = enrichment._Exclusions()
    exclusions.add_address("Umbrook Farm, Ashill, Cullompton EX15 3LZ")
    accumulator = enrichment._Accumulator(exclusions)

    accumulator.add_address("David and Tamsyn Cowie, Umbrook Farm, Ashill, EX15 3LZ")

    assert accumulator.result.company_addresses == []
```

- [ ] **Step 3: Run the enrichment tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_enrichment.py -q
```

Expected: FAIL because all PDFs, form parties, and portal agent names are currently admitted and the validators accept the reproduced junk.

- [ ] **Step 4: Gate PDFs before extraction**

Change `enrich_application_folder` to:

1. preclassify each filename and skip clear non-drawings without opening them;
2. extract text only for candidate drawing PDFs;
3. reject application forms even when misnamed;
4. run the final text-aware drawing classification;
5. append accepted and rejected document names to audit state;
6. call `extract_professional_details` only for accepted documents.

Remove form-agent output and remove the `agent_name` call to `accumulator.add_name`. Keep applicant name and site address only in `_Exclusions`.

Use this loop shape so rejection and unreadable reasons remain auditable:

```python
accumulator = _Accumulator(exclusions)
for path in pdf_paths:
    preliminary = preclassify_drawing_source(path.name)
    if not preliminary.eligible and not preliminary.needs_text:
        accumulator.result.rejected_documents[path.name] = preliminary.reason
        continue
    try:
        document = extract_pdf_text(path)
    except Exception as exc:
        accumulator.result.rejected_documents[path.name] = f"read failed: {exc}"
        continue
    if document.application_form:
        accumulator.result.rejected_documents[path.name] = "application form"
        continue
    decision = classify_drawing_source(path.name, document.text)
    if not decision.eligible:
        accumulator.result.rejected_documents[path.name] = decision.reason
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
```

- [ ] **Step 5: Add field-level source evidence**

Extend the result and accumulator without changing CSV columns:

```python
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
```

Initialize `self.source_document: str | None = None` in `_Accumulator.__init__` and centralize provenance recording:

```python
def _record_source(self, field_name: str) -> None:
    if not self.source_document:
        return
    sources = self.result.field_sources.setdefault(field_name, [])
    _append_unique(sources, self.source_document)
```

Each successful `add_name`, `add_phone`, `add_email`, or `add_address` calls `_record_source` with its matching CSV field. Set `source_document` immediately before extracting one accepted PDF and clear it afterward. When an accepted PDF has no meaningful extracted text after bounded OCR, append its filename to `unreadable_documents`; do not attempt contact extraction from it.

- [ ] **Step 6: Enforce coherent title-block context**

Replace filename score bonuses with an explicit nine-line title-block window. Add these helpers and use them for every name, email, phone, and address candidate:

```python
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
```

A company line is accepted only when this window contains an architect/design/prepared-by/drawn-by role or another valid professional contact item. A person name is accepted only from an explicit professional label or credentials. Expand `_client_context` to inspect the preceding four lines including the candidate and recognize standalone and colon/dash forms of `client`, `applicant`, `owner`, `site owner`, and `contractor`. Compare line positions: return true when the nearest role label in that five-line slice is a client-role label, and false when a later architect/design/prepared-by/drawn-by label starts a new professional block. Treat `consultant` as exclusion context only when it is a project/key-notes role heading with no drawing-author or architectural marker in the same window. Always reject planning-authority, council, and case-officer blocks.

Add `_is_name_noise` and call it from `_looks_like_company` and `_valid_labelled_name`:

```python
NAME_NOISE_MARKERS = (
    "copyright", "all rights reserved", "checked by", "approved by",
    "surveyed", "authorised", "scale", "revision", "drawing status",
    "for planning",
)


def _is_name_noise(value: str) -> bool:
    folded = _clean_candidate(value).casefold()
    if not folded or any(marker in folded for marker in NAME_NOISE_MARKERS):
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
```

Remove the filename branch from `_professional_context_score`; narrative filenames must never increase a candidate score.

- [ ] **Step 7: Harden phone and address validation**

`_normalise_phone` must reject decimal coordinates and non-phone numeric fragments before stripping punctuation. Normalize `+44 (0)` and `0044` to a domestic digit sequence for validation, then require 10 or 11 digits beginning `01`, `02`, `03`, `07`, or `08`:

```python
if re.search(r"\b\d{4,6}\.\d{3,}\b", value):
    return ""
if re.search(r"(?:^|\s)0(?:[\s./-]+0){2,}(?:\s|$)", value):
    return ""
if re.fullmatch(r"\s*\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\s*", value):
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
return re.sub(r"\s+", " ", value).strip(" .,;:-")
```

Extend `_Exclusions.matches_address` with normalized meaningful-token similarity:

```python
ADDRESS_STOP_TOKENS = {
    "the", "road", "street", "lane", "drive", "avenue", "close",
    "way", "uk", "united", "kingdom",
}


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
```

Reject when the candidate and site address have either a matching postcode or `_similar_site_address(...)` is true. Require `_address_around_postcode` results to pass `_has_professional_contact_evidence`; reject project/site/client windows before adding the address.

Keep email extraction on `EMAIL_RE` full matches, retain `_blocked_email` government-domain filtering, and require the same professional title-block evidence and non-client context before calling `add_email`.

- [ ] **Step 8: Run focused and full enrichment tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_enrichment.py tests/test_leads.py -k "enrich or application_form or phone or company_address" -q
```

Expected: PASS.

- [ ] **Step 9: Commit extraction hardening**

```powershell
git add -- src/lead_generator/planning/enrichment.py tests/test_enrichment.py
git commit -m "Restrict enrichment to drawing title blocks"
```

### Task 3: Preserve Document Discovery Failures and Retry Partial Results

**Files:**
- Modify: `src/lead_generator/planning/leads.py`
- Modify: `tests/test_leads.py`

**Interfaces:**
- Produces: `DocumentDiscoveryTransientError(RuntimeError)`.
- Produces: `DocumentSourceFailure(source_url: str, reason: str)`.
- Produces: `DocumentDiscoveryResult(documents: list[PlanningDocument], successful_sources: list[str], failed_sources: list[DocumentSourceFailure])`.
- Produces: `discover_application_documents(application: PlanningApplication) -> DocumentDiscoveryResult`.
- Extends: `fetch_planit_documents(docs_url: str, *, discovery_result: DocumentDiscoveryResult | None = None) -> list[PlanningDocument]` so failed sub-sources do not discard direct page links.
- Preserves: `enrich_application_documents(application) -> PlanningApplication` as a compatibility wrapper.
- Extends: `DocumentDownloadJob` with processed-document URLs and rediscovery state.

- [ ] **Step 1: Write a failing partial-discovery test**

Patch two source URLs so the first returns a PDF and the second raises `HTTPError(503)`. Add the new interfaces to the import list and add this test:

```python
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
```

- [ ] **Step 2: Write a failing end-of-queue rediscovery test**

In `run_lead_search`, make first discovery return one document plus one failed source and final discovery return the original plus a second document with no failures:

```python
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
    self.assertEqual(progress, [(0, 1), (1, 1)])
    self.assertEqual(result.captured_documents, 1)
```

Add the successful-empty companion regression so a council that explicitly returns no files is completed without entering the cooldown queue:

```python
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
```

- [ ] **Step 3: Write a failing publisher-endpoint test**

Supply HTML containing a Publisher `getDocumentList` endpoint, make its AJAX request raise `HTTPError(503)`, and assert the failure propagates instead of returning `[]`:

```python
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
```

- [ ] **Step 4: Run discovery tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "partial_discovery or rediscovery or publisher_endpoint_failure" -q
```

Expected: FAIL because source errors are currently swallowed and no structured discovery result exists.

- [ ] **Step 5: Implement structured discovery reporting**

Add the result types next to the existing document job types:

```python
class DocumentDiscoveryTransientError(RuntimeError):
    """A published document source could not be checked reliably."""

    def __init__(self, source_url: str, reason: str) -> None:
        self.source_url = source_url
        self.reason = reason
        super().__init__(f"{source_url}: {reason}")


@dataclass(frozen=True, slots=True)
class DocumentSourceFailure:
    source_url: str
    reason: str


@dataclass(slots=True)
class DocumentDiscoveryResult:
    documents: list[PlanningDocument] = field(default_factory=list)
    successful_sources: list[str] = field(default_factory=list)
    failed_sources: list[DocumentSourceFailure] = field(default_factory=list)
```

Refactor the body of `enrich_application_documents` into `discover_application_documents`. Use one helper for every HTTP-backed source, including nested `RunThirdPartySearch`, Agile, Tascomi browser fallback, and Publisher AJAX:

```python
def _record_document_source(
    result: DocumentDiscoveryResult,
    source_url: str,
    fetch: Callable[[], list[PlanningDocument]],
) -> list[PlanningDocument]:
    try:
        documents = fetch()
    except Exception as exc:
        failed_url = (
            exc.source_url
            if isinstance(exc, DocumentDiscoveryTransientError)
            else source_url
        )
        result.failed_sources.append(DocumentSourceFailure(failed_url, str(exc)))
        return []
    result.successful_sources.append(source_url)
    return documents
```

Merge returned documents by normalized URL in first-seen order. Keep static documents already present on the application and Civica documents extracted from raw metadata. Treat a `RunThirdPartySearch` URL as a nested source, not as a downloadable file: record its failure separately and retain other parent-page PDFs, but do not enqueue the failed navigation URL for download. The compatibility wrapper must use this exact condition:

```python
def enrich_application_documents(application: PlanningApplication) -> PlanningApplication:
    result = discover_application_documents(application)
    application.documents = result.documents
    if result.failed_sources and not result.successful_sources and not result.documents:
        failures = "; ".join(
            f"{failure.source_url}: {failure.reason}" for failure in result.failed_sources
        )
        raise DocumentDiscoveryTransientError(
            result.failed_sources[0].source_url,
            failures,
        )
    return application
```

The document queue must call `discover_application_documents` directly so it can preserve partial documents and failed-source state.

- [ ] **Step 6: Propagate Publisher list failures**

When Publisher markup advertises `getDocumentList`, network, HTTP, malformed JSON, and invalid `data` shape must raise `DocumentDiscoveryTransientError` with the endpoint URL. A valid `{"data": []}` response remains a successful empty list. Implement the failure boundary as:

```python
try:
    with _open_url_with_retry(request, timeout=45, opener=opener) as response:
        payload = response.read().decode("utf-8", errors="replace")
    data = json.loads(payload)
except Exception as exc:
    raise DocumentDiscoveryTransientError(
        endpoint,
        f"Publisher document list failed: {exc}",
    ) from exc
rows = data.get("data")
if not isinstance(rows, list):
    raise DocumentDiscoveryTransientError(
        endpoint,
        "Publisher document list returned invalid data",
    )
```

In `fetch_planit_documents`, catch that exception only when a `discovery_result` was supplied: append `DocumentSourceFailure(exc.source_url, str(exc))` and continue returning direct links already parsed from the parent page. Without a result collector, re-raise so direct callers and the Publisher regression still observe the failure.

- [ ] **Step 7: Add partial retry state to document jobs**

Extend the job:

```python
@dataclass(slots=True)
class DocumentDownloadJob:
    reference: str
    council: str
    application: PlanningApplication
    folder: Path
    row: dict[str, str]
    pending_documents: list[PlanningDocument] = field(default_factory=list)
    processed_document_urls: set[str] = field(default_factory=set)
    successful_document_sources: set[str] = field(default_factory=set)
    document_source_failures: list[DocumentSourceFailure] = field(default_factory=list)
    rediscovery_required: bool = True
    downloaded_count: int = 0
```

On each pass, rediscover when required, merge only normalized URLs not processed or pending, download all available documents, and preserve successful files. Update `successful_document_sources` with every successful source and replace `document_source_failures` with the latest result's failures. After `_download_pdf_documents_once`, mark every attempted URL except `result.transient_documents` as processed. Set `rediscovery_required = bool(job.document_source_failures)`.

Use this completion rule:

```python
needs_retry = bool(job.pending_documents) or job.rediscovery_required
if needs_retry and not final_attempt:
    deferred_document_jobs.append(job)
    return
if final_attempt and job.rediscovery_required:
    _log(log, f"{job.reference}: partial document capture; one or more sources remain unavailable")
mark_document_complete(job, total)
```

Do not mark a document job complete before that branch. On the final pass, log each unresolved source URL and complete the application without marking its council failed.

Use distinct messages for empty and failed states:

```python
if not job.pending_documents and not job.rediscovery_required:
    if job.successful_document_sources:
        _log(log, f"{job.reference}: no documents currently published by the council")
    else:
        _log(log, f"{job.reference}: no public document source was available")
if needs_retry and not final_attempt:
    _log(log, f"{job.reference}: document discovery deferred for final retry")
```

- [ ] **Step 8: Update existing orchestration tests to patch the new interface**

Replace patches of `enrich_application_documents` inside document-phase tests with `discover_application_documents` returning explicit `DocumentDiscoveryResult` values. Keep compatibility-wrapper unit tests unchanged.

- [ ] **Step 9: Run document orchestration tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "document or discovery or publisher" -q
```

Expected: PASS.

- [ ] **Step 10: Commit discovery retry state**

```powershell
git add -- src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Retry partial application document discovery"
```

### Task 4: Add Missing Council Document Sources and Existing Drawing Downloads

**Files:**
- Modify: `src/lead_generator/planning/leads.py`
- Modify: `tests/test_leads.py`

**Interfaces:**
- Extends: `application_document_source_urls(application) -> list[str]`.
- Adds: `_associated_document_source_urls(html_text: str, page_url: str) -> list[str]`.
- Extends: `fetch_planit_documents(docs_url: str, *, follow_associated: bool = True, discovery_result: DocumentDiscoveryResult | None = None) -> list[PlanningDocument]`.
- Consumes: `is_existing_only_drawing_metadata` from Task 1.

- [ ] **Step 1: Write failing Camden and Exeter source tests**

Add `urlencode` to the `urllib.parse` test import and add these tests:

```python
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
```

Also retain the current Bath `/planningdocuments=<encoded reference>` assertion.

- [ ] **Step 2: Write a failing Wandsworth associated-link test**

Add `_associated_document_source_urls` to the test import list and add:

```python
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
```

- [ ] **Step 3: Change the existing-file exclusion regression**

Replace the old expectation that skips `Existing elevations.pdf` with this complete body:

```python
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
```

- [ ] **Step 4: Run source and exclusion tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "camden_document_source or exeter_document_source or associated_document_source or existing_only_files" -q
```

Expected: FAIL because the custom sources and narrow existing-drawing exception are absent.

- [ ] **Step 5: Add deterministic council source resolvers**

In `application_document_source_urls`, add Camden and Exeter sources before generic application URLs, keyed by case-insensitive authority and encoded reference. Keep Bath on the active application host. Use these exact branches and do not invoke a broad PlanIt search per application:

```python
authority = application.authority.casefold()
if authority == "camden" and reference:
    add(
        "https://camdocs.camden.gov.uk/CMWebDrawer/PlanRec?"
        + urlencode({"q": f'recContainer:"{reference}"'}),
        allow_listing=True,
    )
if authority == "exeter" and reference:
    add(
        "https://exeter.gov.uk/planning-services/permissions-and-applications/"
        "related-documents?" + urlencode({"appref": reference}),
        allow_listing=True,
    )
```

Replace the current Camden `planningrecords.camden.gov.uk/NECSWS/Redirection` branch; do not retain that known-500 redirect as a second source once `camdocs` is available.

- [ ] **Step 6: Follow explicit associated-document pages once**

Parse anchors whose normalized label contains `associated application documents`, `view related documents`, or `documents for this application`:

```python
def _associated_document_source_urls(html_text: str, page_url: str) -> list[str]:
    document = html.fromstring(html_text)
    urls: list[str] = []
    for anchor in document.xpath("//a[@href]"):
        label = clean_text(" ".join(anchor.xpath(".//text()"))).casefold()
        if not any(
            marker in label
            for marker in (
                "associated application documents",
                "view related documents",
                "documents for this application",
            )
        ):
            continue
        url = normalize_url(urljoin(page_url, anchor.get("href") or ""))
        if url and url != normalize_url(page_url) and url not in urls:
            urls.append(url)
    return urls
```

When `follow_associated` is true, fetch each returned URL once with `follow_associated=False`. When an associated source succeeds, append it to `discovery_result.successful_sources`; when it fails, append `DocumentSourceFailure(url, str(exc))` while retaining documents already parsed from the parent page. If no `discovery_result` was supplied, propagate the associated-source exception to the caller.

An associated page that explicitly says an application is not yet available and exposes no files is a successful empty source, not a transient failure.

- [ ] **Step 7: Narrow the existing-only download exclusion**

Change `_is_excluded_document` to the following. Narrative priority in Task 1 prevents `Existing Planning Statement` from matching the `plan` substring:

```python
def _is_excluded_document(document: PlanningDocument) -> bool:
    text = _document_filter_text(document)
    if ".exe" in text or _path_suffix(document.url) == ".exe":
        return True
    if "existing" in text and "proposed" not in text:
        return not is_existing_only_drawing_metadata(text)
    return False
```

- [ ] **Step 8: Run focused tests and verify GREEN**

Run the command from Step 4.

Expected: PASS.

- [ ] **Step 9: Commit council document sources**

```powershell
git add -- src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Recover council-specific planning document sources"
```

### Task 5: Build a Repeatable Run-Recovery Audit

**Files:**
- Create: `src/lead_generator/planning/recovery.py`
- Create: `tests/test_recovery.py`

**Interfaces:**
- Produces: `RecoverySummary(rows_processed: int, applications_with_documents: int, discovery_failures: int, corrected_csv_path: Path, audit_csv_path: Path)`.
- Produces: `recover_search_output(output_dir: Path, *, log: Callable[[str], None] | None = None) -> RecoverySummary`.
- Produces CLI: `python -m lead_generator.planning.recovery <output-directory>`.

- [ ] **Step 1: Write a failing reconstruction test**

Add imports for `CouncilTarget`, `APPLICATION_CSV_FIELDS`, and the recovery interfaces. Add this complete catalogue fixture helper before the test class:

```python
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
```

Then add this reconstruction test:

```python
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
```

Append this second assertion to the same test. Query-key matching must be case-insensitive and cover `keyVal`, `PARAM0`, `id`, `case`, `refval`, `recordNumber`, and `applicationId`:

```python
idox_target = CouncilTarget(
    authority="Example Council",
    portal_family="idox",
    scraper_type="Idox",
    base_url="https://planning.example.test",
    listing_url="https://planning.example.test/search",
    geometry={},
)
idox_row = {
    **row,
    "Reference": "PL/26/05693/ADJ",
    "council": "Example Council",
    "application link": (
        "https://planning.example.test/applicationDetails.do?"
        "activeTab=summary&keyVal=TI9E6KES0YN00"
    ),
}
assert _application_from_row(
    idox_row,
    {"example council": idox_target},
).uid == "TI9E6KES0YN00"
```

- [ ] **Step 2: Write a failing recovery-output test**

Create an input row with every `APPLICATION_CSV_FIELDS` key, then patch discovery, one-pass downloading, and enrichment:

```python
def test_recovery_preserves_original_and_writes_corrected_audit_files(tmp_path: Path) -> None:
    original_csv = tmp_path / "applications.csv"
    row = {field_name: "" for field_name in APPLICATION_CSV_FIELDS}
    row.update(
        {
            "Reference": "REF-1",
            "address": "1 Site Road, London, N1 1AA",
            "application link": "https://planning.example.test/detail?id=UID-1",
            "proposal": "Proposed extension",
            "date received": "2026-07-15",
            "council": "Example Council",
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
        successful_sources=["https://planning.example.test/detail?id=UID-1"],
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
    corrected_rows = list(csv.DictReader(summary.corrected_csv_path.open(encoding="utf-8")))
    audit_rows = list(csv.DictReader(summary.audit_csv_path.open(encoding="utf-8")))
    assert corrected_rows[0]["Architect / Company Name"] == "Example Architects Ltd"
    assert corrected_rows[0]["Phone Number"] == "Failed"
    assert corrected_rows[0]["Email Address"] == "studio@example.co.uk"
    assert corrected_rows[0]["Company Address"] == "Failed"
    assert audit_rows[0]["Architect Sources"] == "Proposed Elevations.pdf"
    assert audit_rows[0]["Remaining Failed Fields"] == (
        "Phone Number: field absent from eligible drawings; "
        "Company Address: field absent from eligible drawings"
    )
```

`_catalogue_json` is a test helper that returns a one-feature catalogue using the same property names as `write_search_fixture`. The corrected row must replace all four enrichment fields from a clean result. The audit row must contain council, reference, eligible documents, field-source documents, categorized remaining failures, and discovery status.

- [ ] **Step 3: Write a failing idempotent-download test**

Place `Proposed Elevations.pdf` in the application folder and return a document with that title plus a second missing document. Capture the one-pass download input and assert only the missing document is passed:

```python
def test_recovery_does_not_redownload_an_existing_file(tmp_path: Path) -> None:
    row = {field_name: "" for field_name in APPLICATION_CSV_FIELDS}
    row.update(
        {
            "Reference": "REF-1",
            "address": "1 Site Road",
            "application link": "https://planning.example.test/detail?id=UID-1",
            "proposal": "Proposed extension",
            "date received": "2026-07-15",
            "council": "Example Council",
        }
    )
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
```

- [ ] **Step 4: Write a failing deferred-recovery test**

Use two rows. The first discovery pass for row one returns one document and a failed source; row two succeeds. The final discovery for row one adds another document. Assert call order proves row two finishes its first pass before row one is retried, and assert the first document is not downloaded twice:

```python
def test_recovery_retries_partial_discovery_after_other_rows(tmp_path: Path) -> None:
    rows: list[dict[str, str]] = []
    for reference in ("REF-1", "REF-2"):
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
        rows.append(row)
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
```

- [ ] **Step 5: Run recovery tests and verify RED**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_recovery.py -q
```

Expected: FAIL because the recovery module does not exist.

- [ ] **Step 6: Implement CSV reconstruction and safe file matching**

Read `applications.csv` with `utf-8-sig`. `load_authority_catalogue()` returns bundled GeoJSON, so convert its features into a case-folded target index explicitly:

```python
def _catalogue_index(catalogue: dict[str, object]) -> dict[str, CouncilTarget]:
    targets: dict[str, CouncilTarget] = {}
    for feature in catalogue.get("features", []):
        properties = feature.get("properties") or {}
        authority = str(properties.get("authority") or properties.get("council_name") or "")
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
```

Extract UID candidates from the case-insensitive query keys listed in Step 1 and use the reference as fallback. Reconstruct metadata with this shape:

```python
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
```

Compare existing files and discovered document titles with the same normalized path-part function used by the downloader. Do not delete files and do not redownload a document whose normalized title already exists.

- [ ] **Step 7: Implement two-pass recovery and clean re-enrichment**

Create this internal state type:

```python
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
```

Add `_recovery_item(row: dict[str, str], catalogue: dict[str, CouncilTarget]) -> _RecoveryItem` and `_recover_documents(item: _RecoveryItem, *, final_attempt: bool, log: Callable[[str], None] | None) -> None`. Process every item once in CSV order:

1. reconstruct the `PlanningApplication`;
2. call `discover_application_documents`;
3. merge only permitted documents whose normalized target filename is not already present;
4. call `_download_pdf_documents_once(..., defer_transient=True)`;
5. retain the successful files and processed URLs;
6. place the item in `deferred_items` when discovery sources or transient downloads failed.

Implement the pass body with URL deduplication and existing-file matching:

```python
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
    batch = _download_pdf_documents_once(
        attempted,
        item.folder,
        log=log,
        defer_transient=not final_attempt,
    )
    transient_urls = {document.url for document in batch.transient_documents}
    item.processed_document_urls.update(
        document.url for document in attempted if document.url not in transient_urls
    )
    item.pending_documents = list(batch.transient_documents)
```

After every item has completed its first pass, wait once with `_wait_for_document_retry_cooldown`. For each deferred item, rediscover, merge only new/unprocessed documents, and call `_download_pdf_documents_once(..., defer_transient=False)`. Then enrich every folder exactly once from a fresh `ContactEnrichment`, replace all four fields in copied rows, and append one audit row in original CSV order.

The core loop must follow this control flow:

```python
items = [_recovery_item(row, catalogue) for row in input_rows]
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
    item.enrichment = enrich_application_folder(
        item.folder,
        site_address=item.application.address,
        log=log,
    )
```

Catch errors per application so one council cannot abort the remaining rows. Record exact source URLs and error text in `Document Discovery Status`. Never feed CSV enrichment values back into the extractor.

- [ ] **Step 8: Categorize failures and write both CSV files atomically**

Use the repository's `write_csv` helper. The corrected file keeps `APPLICATION_CSV_FIELDS`. The audit headers are:

```python
RECOVERY_AUDIT_FIELDS = [
    "Reference", "Council", "Eligible Documents", "Architect Sources",
    "Phone Sources", "Email Sources", "Address Sources",
    "Remaining Failed Fields", "Document Discovery Status",
]


@dataclass(frozen=True, slots=True)
class RecoverySummary:
    rows_processed: int
    applications_with_documents: int
    discovery_failures: int
    corrected_csv_path: Path
    audit_csv_path: Path
```

Build `Remaining Failed Fields` as semicolon-separated `field: reason` entries. Use `no eligible drawing published` when `eligible_documents` is empty, `drawing unreadable after bounded OCR` when all eligible documents are in `unreadable_documents`, and `field absent from eligible drawings` otherwise. Set a successful status to `Completed: N source(s) checked`; set an unresolved status to `Partial/Failed: <source URL>: <error>` with additional failures separated by ` | `. An unresolved document discovery failure increments `RecoverySummary.discovery_failures`.

Use `write_csv(corrected_path, corrected_rows)` for the application output. Add a generic atomic audit writer using the existing CSV helpers:

```python
def _write_rows_atomically(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, str]],
) -> None:
    temporary_path = path.with_name(f".{path.name}.tmp")
    initialise_csv(temporary_path, fieldnames)
    append_csv_rows(temporary_path, fieldnames, rows)
    temporary_path.replace(path)
```

Add `argparse` entry behavior under `if __name__ == "__main__": main()` and print the final output paths and counts. `RecoverySummary.applications_with_documents` counts folders containing at least one permitted PDF after both passes.

- [ ] **Step 9: Run recovery tests and verify GREEN**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_recovery.py -q
```

Expected: PASS.

- [ ] **Step 10: Commit the recovery utility**

```powershell
git add -- src/lead_generator/planning/recovery.py tests/test_recovery.py
git commit -m "Add auditable enrichment run recovery"
```

### Task 6: Recover and Audit All 85 Supplied Records

**Files:**
- Read: `C:/Users/AliBouhaddou-Robinso/Downloads/TEST2107 ENRICH NO2/2026-07-21/applications.csv`
- Read/write: application folders beneath `C:/Users/AliBouhaddou-Robinso/Downloads/TEST2107 ENRICH NO2/2026-07-21`
- Create: `C:/Users/AliBouhaddou-Robinso/Downloads/TEST2107 ENRICH NO2/2026-07-21/applications.corrected.csv`
- Create: `C:/Users/AliBouhaddou-Robinso/Downloads/TEST2107 ENRICH NO2/2026-07-21/enrichment_audit.csv`

**Interfaces:**
- Consumes: `recover_search_output` from Task 5.
- Produces: corrected and auditable output for every supplied record.

- [ ] **Step 1: Run focused tests before touching the supplied output**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_enrichment.py tests/test_recovery.py tests/test_leads.py -k "enrich or document or recovery" -q
```

Expected: PASS.

- [ ] **Step 2: Run the recovery command**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m lead_generator.planning.recovery "C:\Users\AliBouhaddou-Robinso\Downloads\TEST2107 ENRICH NO2\2026-07-21"
```

Expected: 85 rows processed; the original CSV remains unchanged; corrected and audit CSV paths are reported.

- [ ] **Step 3: Verify every corrected row against its audit evidence**

Run this read-only audit:

```powershell
$auditScript = @'
import csv
import re
from pathlib import Path

root = Path(r"C:\Users\AliBouhaddou-Robinso\Downloads\TEST2107 ENRICH NO2\2026-07-21")
with (root / "applications.corrected.csv").open(encoding="utf-8-sig", newline="") as handle:
    corrected_rows = list(csv.DictReader(handle))
with (root / "enrichment_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
    audit_rows = list(csv.DictReader(handle))

source_columns = {
    "Architect / Company Name": "Architect Sources",
    "Phone Number": "Phone Sources",
    "Email Address": "Email Sources",
    "Company Address": "Address Sources",
}
assert len(corrected_rows) == 85
assert len(audit_rows) == 85
assert [row["Reference"] for row in corrected_rows] == [row["Reference"] for row in audit_rows]
assert not any(
    "copyright" in row["Architect / Company Name"].casefold()
    for row in corrected_rows
)
assert not any(
    re.search(r"\b\d{4,6}\.\d{3,}\b", row["Phone Number"])
    for row in corrected_rows
)
assert not any(
    row[field_name] != "Failed" and not audit[source_column].strip()
    for row, audit in zip(corrected_rows, audit_rows)
    for field_name, source_column in source_columns.items()
)

problem = next(row for row in corrected_rows if row["Reference"] == "PL/26/05353/FA")
problem_text = " ".join(problem.values()).casefold()
for forbidden in (
    "croudace homes",
    "064646.00001",
    "065898.00001",
    "066916.00001",
    "all rights reserved",
    "checked by:approved by:scale",
    "land west of lent rise road",
):
    assert forbidden not in problem_text, forbidden

print("Verified 85 corrected rows and 85 source-audit rows")
'@
$auditScript | & ".\.venv\Scripts\python.exe" -
```

Expected: `Verified 85 corrected rows and 85 source-audit rows`.

- [ ] **Step 4: Review every remaining failure category**

Run this category check and inspect every printed unresolved discovery row:

```powershell
$reviewScript = @'
import csv
from collections import Counter
from pathlib import Path

root = Path(r"C:\Users\AliBouhaddou-Robinso\Downloads\TEST2107 ENRICH NO2\2026-07-21")
with (root / "enrichment_audit.csv").open(encoding="utf-8-sig", newline="") as handle:
    rows = list(csv.DictReader(handle))
allowed = {
    "no eligible drawing published",
    "drawing unreadable after bounded OCR",
    "field absent from eligible drawings",
}
counts = Counter()
for row in rows:
    for item in filter(None, row["Remaining Failed Fields"].split("; ")):
        field_name, separator, reason = item.partition(": ")
        assert separator and reason in allowed, (row["Reference"], item)
        counts[reason] += 1
    if row["Document Discovery Status"].startswith("Partial/Failed:"):
        print(
            f'UNRESOLVED {row["Council"]} {row["Reference"]}: '
            f'{row["Document Discovery Status"]}'
        )
print(dict(sorted(counts.items())))
'@
$reviewScript | & ".\.venv\Scripts\python.exe" -
```

Expected: every missing field has exactly one allowed evidence category. The recovery module has already retried all transient discovery cases once after the first 85-row pass; investigate any `UNRESOLVED` line before release and record a currently unavailable public source rather than inventing contact data.

- [ ] **Step 5: Recheck Birmingham and Powys without blocking release**

Run each council alone for 13-19 July 2026 through the bounded search wrapper:

```powershell
$env:PYTHONPATH = "$PWD\src"
$probeScript = @'
from datetime import date
from lead_generator.planning.leads import (
    discover_portal_applications_with_deadline,
    load_authority_catalogue,
)
from lead_generator.planning.recovery import _catalogue_index

targets = {
    target.authority: target
    for target in _catalogue_index(load_authority_catalogue()).values()
    if target.authority in {"Birmingham", "Powys"}
}
for authority in ("Birmingham", "Powys"):
    try:
        applications = discover_portal_applications_with_deadline(
            targets[authority],
            date(2026, 7, 13),
            date(2026, 7, 19),
            timeout_seconds=60,
            max_elapsed_seconds=180,
            heartbeat_seconds=30,
            log=lambda message, authority=authority: print(f"{authority}: {message}"),
        )
    except Exception as exc:
        print(f"{authority}: unavailable after bounded verification: {exc}")
    else:
        print(f"{authority}: {len(applications)} application(s)")
'@
$probeScript | & ".\.venv\Scripts\python.exe" -
```

Record whether the direct portal, public metadata fallback, or neither returns applications. Do not classify a document error as a council search error. The 180-second cap prevents either challenge page from blocking release.

### Task 7: Full Verification and Executable Rebuild

**Files:**
- Modify: `dist/PlanningLeadGenerator.exe`
- Verify: all tracked source, test, specification, and plan files.

**Interfaces:**
- Consumes all prior tasks.
- Produces the tested Windows executable on branch `enrichment`.

- [ ] **Step 1: Run repository hygiene checks**

Run:

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only intended tracked changes plus the pre-existing untracked build outputs are present.

- [ ] **Step 2: Run the complete automated test suite**

Run:

```powershell
$env:PYTHONPATH = "$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest -q
```

Expected: all tests pass with no errors or warnings introduced by this change.

- [ ] **Step 3: Build the executable in isolated output paths**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --workpath .build-drawing-enrichment --distpath .dist-drawing-enrichment PlanningLeadGenerator.spec
```

After a successful build, replace `dist/PlanningLeadGenerator.exe` with `.dist-drawing-enrichment/PlanningLeadGenerator.exe`.

```powershell
Copy-Item -LiteralPath ".dist-drawing-enrichment\PlanningLeadGenerator.exe" -Destination "dist\PlanningLeadGenerator.exe" -Force
if (-not (Test-Path -LiteralPath "dist\PlanningLeadGenerator.exe")) {
    throw "Packaged executable was not copied into dist"
}
```

- [ ] **Step 4: Smoke-test the packaged GUI**

Launch only the newly built executable, verify its process stays alive long enough to initialize the GUI, then stop only that process. Record the executable's SHA-256 hash and verify Git LFS recognizes the tracked binary:

```powershell
$exe = (Resolve-Path -LiteralPath "dist\PlanningLeadGenerator.exe").Path
$process = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
try {
    Start-Sleep -Seconds 8
    if ($process.HasExited) {
        throw "PlanningLeadGenerator.exe exited during GUI startup with code $($process.ExitCode)"
    }
} finally {
    if (-not $process.HasExited) {
        Stop-Process -Id $process.Id
        $process.WaitForExit()
    }
}
Get-FileHash -Algorithm SHA256 -LiteralPath $exe
git check-attr filter -- "dist/PlanningLeadGenerator.exe"
git lfs ls-files --include="dist/PlanningLeadGenerator.exe"
```

Expected: the process remains alive for eight seconds, `filter: lfs` is reported, and the executable appears in `git lfs ls-files`.

- [ ] **Step 5: Re-run tests after packaging**

Run the complete test command from Step 2 again.

Expected: all tests pass.

- [ ] **Step 6: Review and commit the release artifact**

Inspect and stage only intended source, tests, plan, and `dist/PlanningLeadGenerator.exe`:

```powershell
git diff --stat
git diff --check
git status --short
git add -- `
  src/lead_generator/planning/drawing_sources.py `
  src/lead_generator/planning/enrichment.py `
  src/lead_generator/planning/leads.py `
  src/lead_generator/planning/recovery.py `
  tests/test_enrichment.py `
  tests/test_leads.py `
  tests/test_recovery.py `
  docs/superpowers/plans/2026-07-22-drawing-only-enrichment-and-document-recovery.md `
  dist/PlanningLeadGenerator.exe
git diff --cached --stat
git diff --cached --check
git commit -m "Harden drawing enrichment and document recovery"
```

Expected: only the listed files are staged; the commit succeeds; the pre-existing untracked build/distribution directories and `dist/search_history.csv` remain untouched.

Do not push until the user requests publication.
