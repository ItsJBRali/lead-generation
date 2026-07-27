# Search Completeness and Reconciliation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retrieve every available in-range planning application from each selected council, supplement silent portal omissions with a serial PlanIt reconciliation pass, and produce accurate unique run totals before document processing.

**Architecture:** Primary platform workers will search council portals without invoking PlanIt, preserving the existing deferred retry queue. Adapter pagination will establish complete primary result sets, then one serial reconciliation pass will union missing PlanIt references into the same relevance, GeoJSON, CSV, document, and enrichment pipeline. Run-level state will defer failed/no-application classification until both sources have completed.

**Tech Stack:** Python 3.11+, `dataclasses`, `threading`, `urllib`, `lxml`, CustomTkinter, `unittest`/`pytest`, PyInstaller.

## Global Constraints

- The uploaded GeoJSON is the authoritative search area and may cover all or part of an intersecting council.
- Search every intersecting council for the complete received-date range before applying the GeoJSON filter.
- PlanIt is a supplement and must never replace a successful primary portal record.
- Run reconciliation with exactly one worker and the existing PlanIt request throttle.
- Deduplicate exact stripped application references before totals, CSV rows, document jobs, and enrichment jobs are created.
- Exclude historical, out-of-range, missing-date, and unparseable-date records from `Total Applications`.
- Run document downloads and enrichment only after primary discovery, primary retries, and reconciliation have finished.
- Keep pagination, retries, waits, and cancellation bounded.
- Do not add a new runtime dependency.

---

### Task 1: Correct and paginate StatMap searches

**Files:**
- Modify: `src/lead_generator/planning/adapters/base.py`
- Modify: `src/lead_generator/planning/adapters/legacy_forms.py:747-829`
- Test: `tests/test_non_idox_scrapers.py:848-877`

**Interfaces:**
- Produces: `PortalSearchCompletenessError(RuntimeError)` in `adapters.base`.
- Produces: `StatMapPlanningScraper.search(...) -> list[PlanningApplication]` using the current top-level request schema.
- Consumes later: primary search retry handling treats `PortalSearchCompletenessError` as a normal retryable council-search error.

- [ ] **Step 1: Write failing StatMap payload and pagination tests**

Add an offset-aware fake HTTP client and tests that assert the real portal contract:

```python
class PaginatedStatMapHttp:
    def __init__(self, pages):
        self.pages = pages
        self.posts = []

    def post_json(self, url, data):
        self.posts.append((url, data))
        payload = self.pages[data["offset"]]
        return FetchResponse(url=url, status_code=200, text=json.dumps(payload))


def test_statmap_uses_current_date_filter_and_fetches_every_offset(self):
    http = PaginatedStatMapHttp({
        0: {"total": 3, "records": [statmap_record("MO/1", "2026-07-21"), statmap_record("MO/2", "2026-07-22")]},
        2: {"total": 3, "records": [statmap_record("MO/3", "2026-07-23")]},
    })
    scraper = StatMapPlanningScraper(
        LegacyFormsCouncilConfig("Mole Valley", "https://molevalley.example"),
        http_client=http,
    )

    applications = scraper.search(
        "https://molevalley.example/horizoNext/",
        start_date=date(2026, 7, 20),
        end_date=date(2026, 7, 26),
        limit=None,
    )

    assert [app.reference for app in applications] == ["MO/1", "MO/2", "MO/3"]
    assert [post[1]["offset"] for post in http.posts] == [0, 2]
    assert http.posts[0][1]["filter"] == {
        "parts": [{
            "filterItems": [
                {"columnName": "receivedDateFrom", "value": "2026-07-20", "operator": "="},
                {"columnName": "receivedDateTo", "value": "2026-07-26", "operator": "="},
            ]
        }]
    }
```

Add separate tests for an explicitly out-of-range response, a repeated page, and a final fetched count below the portal's `total`. Each must raise `PortalSearchCompletenessError`.

- [ ] **Step 2: Run the StatMap tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_non_idox_scrapers.py -k "statmap" -q
```

Expected: FAIL because the adapter still sends nested `pagination`, slices the first response, and does not raise a completeness error.

- [ ] **Step 3: Implement the current StatMap request contract**

Add the shared exception:

```python
class PortalSearchCompletenessError(RuntimeError):
    """A portal response could not prove that the requested result set is complete."""
```

Change StatMap to send:

```python
payload = {
    "pageSize": page_size,
    "offset": offset,
    "filter": {
        "parts": [{
            "filterItems": [
                {"columnName": "receivedDateFrom", "value": start_date.isoformat(), "operator": "="},
                {"columnName": "receivedDateTo", "value": end_date.isoformat(), "operator": "="},
            ]
        }]
    },
    "order": {"receivedDate": "desc"},
    "advancedFilter": {},
}
```

Use a page size of 100 unless a smaller explicit `limit` is supplied. Continue offsets until the unique fetched count reaches `total` or a short final page proves completion. Track page reference signatures; raise `PortalSearchCompletenessError` on a repeated page, invalid `records`/`total`, an empty page before `total`, or an explicitly out-of-range received date. Exclude missing and invalid dates from the returned applications.

- [ ] **Step 4: Run StatMap tests and verify GREEN**

Run the command from Step 2.

Expected: all StatMap tests PASS, including the two-record 20–26 July payload fixture.

- [ ] **Step 5: Commit Task 1**

```powershell
git add src/lead_generator/planning/adapters/base.py src/lead_generator/planning/adapters/legacy_forms.py tests/test_non_idox_scrapers.py
git commit -m "Fix StatMap date filtering and pagination"
```

---

### Task 2: Remove fixed Socrata and linked-page result caps

**Files:**
- Modify: `src/lead_generator/planning/adapters/legacy_forms.py:683-979`
- Test: `tests/test_non_idox_scrapers.py`

**Interfaces:**
- Produces: complete offset pagination from `SocrataPlanningScraper.search(...)`.
- Produces: `_linked_result_pages(...) -> list[FetchResponse]` for legacy HTML searches with explicit next-page links.
- Consumes: `PortalSearchCompletenessError` from Task 1.

- [ ] **Step 1: Write a failing Socrata test for more than 100 rows**

Use a fake that returns 100 records at offset 0 and one record at offset 100:

```python
def test_socrata_fetches_rows_beyond_the_first_hundred():
    http = PaginatedSocrataHttp(total_rows=101)
    scraper = SocrataPlanningScraper(
        LegacyFormsCouncilConfig("Camden", "https://opendata.camden.gov.uk"),
        http_client=http,
    )

    applications = scraper.search(
        "https://opendata.camden.gov.uk/Environment/Planning-Applications/2eiu-s2cw/about_data",
        start_date=date(2026, 7, 1),
        end_date=date(2026, 7, 31),
        limit=None,
    )

    assert len(applications) == 101
    assert [params["$offset"] for _, params in http.gets] == ["0", "100"]
```

Add a repeated-page test that raises `PortalSearchCompletenessError`.

- [ ] **Step 2: Write failing linked-page and reported-total tests**

Add fixtures where `HtmlListPlanningScraper`, `QueryFormPlanningScraper`,
`AstunPlanningScraper`, `EnterpriseStorePlanningScraper`, and
`AppSearchServPlanningScraper` receive a result page containing a unique
`<a rel="next" href="...">Next</a>` link. Assert both pages are merged by
reference for each parser.

Add a fixture whose text says `Showing 1-50 of 75` without a next link. Assert `PortalSearchCompletenessError` rather than a successful 50-record return.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_non_idox_scrapers.py -k "socrata or linked_result or reported_total" -q
```

Expected: FAIL because Socrata performs one `$limit=100` request and the generic legacy adapters do not follow result links or validate displayed totals.

- [ ] **Step 4: Implement bounded offset and linked-page pagination**

For Socrata, request `$limit=100` with `$offset` starting at `0`, retain the existing `$where` and stable `$order`, and stop only on a short page or the explicit caller limit. Reject repeated non-empty page signatures.

Add a private linked-page helper that:

```python
def _linked_result_pages(
    http: CouncilHttpClient,
    first_response: FetchResponse,
    *,
    max_pages: int = 100,
) -> list[FetchResponse]:
    ...
```

It follows only same-host links identified by `rel=next`, `aria-label=Next`, or exact visible `Next`/`>` pagination text. It tracks normalized URLs and raises on loops or more than 100 pages. Merge parsed applications by exact stripped reference.

Add `_reported_result_total(html_text: str) -> int | None` for explicit phrases such as `Showing 1-50 of 75`, `75 results`, and `Total records: 75`. Raise `PortalSearchCompletenessError` when an explicit total exceeds the unique parsed count after all linked pages.

Wire the helper and reported-total validation into `HtmlListPlanningScraper`,
`QueryFormPlanningScraper`, `AstunPlanningScraper`,
`EnterpriseStorePlanningScraper`, and `AppSearchServPlanningScraper`. Parse
each fetched page with that adapter's existing parser before merging.

For generic Astun forms, set `pagerecs` to the largest offered numeric option
before submission, matching the hardened Elmbridge adapter.

- [ ] **Step 5: Run the focused and complete non-Idox tests**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/test_non_idox_scrapers.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 2**

```powershell
git add src/lead_generator/planning/adapters/legacy_forms.py tests/test_non_idox_scrapers.py
git commit -m "Harden capped legacy planning searches"
```

---

### Task 3: Separate primary discovery from PlanIt reconciliation

**Files:**
- Modify: `src/lead_generator/planning/leads.py:239-406`
- Modify: `src/lead_generator/planning/leads.py:1369-1792`
- Test: `tests/test_leads.py:506-915`
- Test: `tests/test_leads.py:5699-5716`

**Interfaces:**
- Produces: `discover_portal_applications(...)` that searches only the primary portal.
- Produces: `discover_planit_reconciliation_applications(target, start_date, end_date, *, should_cancel=None) -> list[PlanningApplication]`.
- Preserves: `planit_authority_candidates(authority) -> tuple[str, ...]`.

- [ ] **Step 1: Write failing source-separation tests**

Replace the old PlanIt-first expectations with:

```python
def test_primary_discovery_does_not_wait_for_planit():
    with (
        patch("lead_generator.planning.leads._discover_portal_listing", return_value=(discovery, scraper)),
        patch("lead_generator.planning.leads.discover_planit_applications") as planit,
    ):
        applications = discover_portal_applications(target, start_date, end_date)

    assert applications == discovery.applications
    planit.assert_not_called()
```

Add reconciliation tests that verify aliases are tried, returned records are retagged with the selected council, an empty successful query is distinguishable from all candidates failing, and cancellation propagates as `CouncilSearchCancelledError`.

- [ ] **Step 2: Add failing PlanIt completeness tests**

Extend the existing pagination test so an empty second page with `total=150` raises:

```python
with pytest.raises(RuntimeError, match="ended at 100 of 150"):
    _discover_planit_applications_serial("Example", start_date, end_date)
```

Add a test proving records on later pages are returned exactly once.

- [ ] **Step 3: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "primary_discovery or reconciliation or planit_pagination" -q
```

Expected: FAIL because primary discovery currently invokes PlanIt for aliases, three problem authorities, empty results, detail failures, and Elmbridge.

- [ ] **Step 4: Implement source separation**

Remove `PLANIT_FIRST_AUTHORITIES` and `PLANIT_SUPPLEMENT_AUTHORITIES`. Remove the PlanIt special case from `council_platform_key`; primary tasks must be grouped by their actual council platform.

Keep primary portal retry/detail enrichment behavior, but do not invoke PlanIt inside `discover_portal_applications`.

Implement reconciliation candidate handling:

```python
def discover_planit_reconciliation_applications(
    target: CouncilTarget,
    start_date: date,
    end_date: date,
    *,
    should_cancel: CancelCallback | None = None,
) -> list[PlanningApplication]:
    successful_query = False
    errors: list[str] = []
    for authority in planit_authority_candidates(target.authority):
        try:
            applications = discover_planit_applications(
                authority, start_date, end_date, should_cancel=should_cancel
            )
        except CouncilSearchCancelledError:
            raise
        except Exception as exc:
            errors.append(f"{authority}: {exc}")
            continue
        successful_query = True
        if applications:
            return [_tag_planit_application(target, authority, app) for app in applications]
    if successful_query:
        return []
    raise RuntimeError("; ".join(errors) or "PlanIt reconciliation did not complete")
```

Update PlanIt pagination to raise when a page ends before the advertised total, while retaining repeated-page, page-count, request-throttle, and cancellation guards.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```powershell
git add src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Separate portal discovery from PlanIt reconciliation"
```

---

### Task 4: Introduce date-valid unique run state

**Files:**
- Modify: `src/lead_generator/planning/leads.py:328-443`
- Modify: `src/lead_generator/planning/leads.py:658-980`
- Test: `tests/test_leads.py`

**Interfaces:**
- Produces: `CouncilDiscoveryState`.
- Produces: `application_reference_key(application) -> str`.
- Produces: `application_is_in_date_range(application, start_date, end_date) -> bool`.
- Produces inside `run_lead_search`: one `process_discovered_applications(target, applications, source)` path shared by primary and reconciliation.

- [ ] **Step 1: Write failing strict-date and identity tests**

Add unit tests:

```python
def test_application_reference_key_is_the_exact_stripped_reference():
    app = PlanningApplication(authority="A", uid="uid", url="u", reference="  ABC/1  ")
    assert application_reference_key(app) == "ABC/1"


@pytest.mark.parametrize("value", [None, "", "not-a-date", "2026-07-19", "2026-07-27"])
def test_application_outside_or_without_received_date_is_not_in_range(value):
    app = PlanningApplication(authority="A", uid="uid", url="u", date_received=value)
    assert not application_is_in_date_range(app, date(2026, 7, 20), date(2026, 7, 26))
```

Add a run test returning two date-valid applications plus thousands of undated records. Assert `result.total_applications == 2`.

- [ ] **Step 2: Write a failing shared-pipeline deduplication test**

Arrange the same reference in primary and secondary records and assert one row, one captured callback, one document job, and one total application. Also assert the row retains the primary portal URL.

- [ ] **Step 3: Run focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "reference_key or in_range or undated or shared_pipeline" -q
```

Expected: FAIL because total counting currently uses raw list length per authority and row creation is embedded only in the primary worker.

- [ ] **Step 4: Implement run state and the shared application path**

Add:

```python
@dataclass(slots=True)
class CouncilDiscoveryState:
    target: CouncilTarget
    primary_succeeded: bool = False
    primary_error: str | None = None
    reconciliation_succeeded: bool = False
    reconciliation_error: str | None = None
    primary_date_valid_count: int = 0
    secondary_date_valid_count: int = 0
    undated_count: int = 0
    date_valid_references: set[str] = field(default_factory=set)
```

Replace `counted_authorities` with a run-level `total_application_references: set[str]`.

`process_discovered_applications` must:

1. reject missing, invalid, and out-of-range application dates;
2. add each exact stripped reference once to the run total and council state;
3. apply `application_matches` and `application_matches_search_area`;
4. reserve the reference once for output;
5. construct the existing CSV row, lead folder, enrichment job, and document job;
6. call `save_row` so the captured counter updates identically for both sources.

Keep the primary record by processing it first. Reconciliation duplicates therefore stop at `reserve_reference` and cannot replace its row or document metadata.

Increment the source-specific date-valid count for logging. Count and log
missing or unparseable dates as
`Council: excluded N application(s) without a usable in-range received date`;
do not add them to `date_valid_references` or the run total.

- [ ] **Step 5: Run focused tests and verify GREEN**

Run the command from Step 3.

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```powershell
git add src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Track unique date-valid planning applications"
```

---

### Task 5: Add the serial reconciliation phase

**Files:**
- Modify: `src/lead_generator/planning/leads.py:831-1269`
- Test: `tests/test_leads.py`

**Interfaces:**
- Consumes: `discover_planit_reconciliation_applications` from Task 3.
- Consumes: `process_discovered_applications` and `CouncilDiscoveryState` from Task 4.
- Produces: reconciliation logs in the form `Council: reconciliation primary=X secondary=Y added=Z`.

- [ ] **Step 1: Write a failing end-to-end reconciliation test**

Create two councils. Return one relevant primary application for the first council, one different relevant PlanIt application for each council, and a duplicate primary reference from PlanIt.

Assert:

```python
assert result.leads_found == 3
assert result.total_applications == 3
assert captured_counts == [1, 2, 3]
assert all("reconciliation" in message for message in reconciliation_messages)
assert events.index("all-primary-finished") < events.index("first-planit-query")
assert events.index("last-planit-query") < events.index("first-document-discovery")
```

- [ ] **Step 2: Write failing filter-equivalence tests**

Add secondary-only applications that are:

- outside the received-date range;
- excluded by proposal rules;
- missing the keyword;
- outside a partial-council GeoJSON polygon;
- inside that polygon.

Assert only the in-date, relevant, inside application creates a row.

- [ ] **Step 3: Write a failing reconciliation cancellation test**

Cancel during the serial pass. Assert no document phase starts, captured rows already written remain in the CSV, and `Completion` is `Cancelled`.

- [ ] **Step 4: Run the focused tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "reconciliation_phase or secondary_filter or reconciliation_cancel" -q
```

Expected: FAIL because no post-primary reconciliation phase exists.

- [ ] **Step 5: Implement the one-worker reconciliation loop**

Insert the phase after the final primary retry and before `if config.download_application_files:`.

For each target:

```python
_log(log, f"Reconciliation {index} of {len(targets)}: checking {target.authority}")
secondary = discover_planit_reconciliation_applications(
    target,
    config.start_date,
    config.end_date,
    should_cancel=cancellation_requested,
)
before = len(state.date_valid_references)
secondary_valid = process_discovered_applications(target, secondary, source="PlanIt")
added = len(state.date_valid_references) - before
_log(
    log,
    f"{target.authority}: reconciliation primary={state.primary_date_valid_count} "
    f"secondary={secondary_valid} added={added}",
)
```

Run this loop in the calling thread, which is exactly one reconciliation worker. Do not acquire a primary scheduler slot. Reuse the existing PlanIt semaphore, throttle, retry, timeout, and cancelable wait behavior.

Capture reconciliation exceptions in `CouncilDiscoveryState` and the failure CSV as nonfatal warnings at this stage. Continue to the next council unless cancellation was requested.

- [ ] **Step 6: Run focused tests and verify GREEN**

Run the command from Step 4.

Expected: PASS.

- [ ] **Step 7: Commit Task 5**

```powershell
git add src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Reconcile missing applications after portal searches"
```

---

### Task 6: Finalize council classification, history totals, and document handoff

**Files:**
- Modify: `src/lead_generator/planning/leads.py:784-1269`
- Test: `tests/test_leads.py:1700-1828`

**Interfaces:**
- Consumes: per-council source success/error state from Tasks 4 and 5.
- Produces: final `failed_councils`, `no_application_councils`, `completion`, and archive totals.

- [ ] **Step 1: Write failing final-classification tests**

Cover this matrix:

```text
primary success + secondary failure + zero records -> no applications
primary failure + secondary success + zero records -> no applications
primary failure + secondary success + records      -> recovered, neither list
primary failure + secondary failure                -> failed
primary success + secondary success + records      -> neither list
```

Assert a secondary failure never changes a successful primary run to `Failed`.

- [ ] **Step 2: Write a failing archive and document handoff test**

Use one primary record, one secondary-only record, one undated record, and one duplicate. Assert:

```python
assert history_row["Total Applications"] == "2"
assert history_row["Relevant Captured Applications"] == "2"
assert history_row["% Relevant"] == "100.00%"
assert document_references == ["PRIMARY/1", "SECONDARY/1"]
```

- [ ] **Step 3: Run tests and verify RED**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -k "source_classification or archive_reconciliation or document_handoff" -q
```

Expected: FAIL because failure/no-application lists and `had_error` are currently finalized inside primary workers.

- [ ] **Step 4: Finalize statuses after reconciliation**

Primary workers must only record success/error state and deferred retry outcome. After reconciliation:

```python
for state in council_states.values():
    any_source_succeeded = state.primary_succeeded or state.reconciliation_succeeded
    if state.date_valid_references:
        continue
    if any_source_succeeded:
        no_application_councils.append(state.target.authority)
    else:
        failed_councils.append(state.target.authority)
        had_error = True
```

Write one final fatal failure row when both sources failed; preserve primary and reconciliation reasons in the row. Keep individual responsive or incomplete-source warnings nonfatal.

Set `total_applications = len(total_application_references)` immediately before constructing `LeadSearchResult`. Confirm document and enrichment job totals are calculated after reconciliation.

- [ ] **Step 5: Run focused and full lead tests**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest tests/test_leads.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit Task 6**

```powershell
git add src/lead_generator/planning/leads.py tests/test_leads.py
git commit -m "Finalize search status after reconciliation"
```

---

### Task 7: Regression, live coverage, and packaged release

**Files:**
- Modify if required by verified defects: adapter files under `src/lead_generator/planning/adapters/`
- Modify if required by verified defects: corresponding `tests/test_*_scraper.py`
- Rebuild: `dist/PlanningLeadGenerator.exe`

**Interfaces:**
- Verifies all behavior introduced by Tasks 1-6.
- Produces the updated executable used for future runs.

- [ ] **Step 1: Run the focused search-completeness suite**

Run:

```powershell
$env:PYTHONPATH="$PWD\src"
& ".\.venv\Scripts\python.exe" -m pytest tests/test_non_idox_scrapers.py tests/test_leads.py -k "statmap or socrata or pagination or reconciliation or total_applications or no_application or failed_councils" -q
```

Expected: PASS with no hangs.

- [ ] **Step 2: Run the full automated suite**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
```

Expected: PASS.

- [ ] **Step 3: Run bounded live adapter checks**

Run one-week searches for Mole Valley, Elmbridge, Runnymede, Woking, Camden, and one council from every remaining instantiated adapter family. Record:

```text
authority | primary count | PlanIt count | added count | final unique count | warning
```

Verify:

- Mole Valley returns only in-range records and never loads the historical 122,950-record dataset.
- Camden can return beyond 100 records for a broad test interval.
- Elmbridge `2026/1681` is recovered when its received date overlaps the interval.
- Runnymede `RU.26/0904` and `RU.26/0943` are recovered when their dates overlap the interval.
- pagination never stops with a fetched count below an advertised total.
- existing numbered-page, cursor, offset, and date-splitting adapters still
  terminate on no progress and retain all unique references.

- [ ] **Step 4: Add a regression before fixing any live-only defect**

For every reproducible live defect, add a fixture-based failing test to the corresponding adapter test module, run it to verify RED, implement the smallest correction, and rerun it to GREEN. Do not patch an adapter from observation alone.

- [ ] **Step 5: Run cancellation and document-transition smoke tests**

Verify cancellation during primary pagination and reconciliation returns promptly, saves existing CSV rows, and skips document startup. Verify a secondary-only captured record creates and completes one document job when downloads are enabled.

- [ ] **Step 6: Build the executable**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --workpath .build-search-completeness --distpath .dist-search-completeness PlanningLeadGenerator.spec
```

After a successful build, replace `dist/PlanningLeadGenerator.exe` with `.dist-search-completeness/PlanningLeadGenerator.exe`.

- [ ] **Step 7: Smoke-test the packaged executable**

Launch the rebuilt executable, verify the default previous-Monday-to-Sunday dates, run-log position preservation, worker selector, reconciliation log messages, captured counter, CSV output, cancellation, and document-checkbox behavior.

- [ ] **Step 8: Run final verification and commit**

Run:

```powershell
& ".\.venv\Scripts\python.exe" -m pytest -q
git status --short
```

Commit only source, tests, plan, and the intended executable:

```powershell
git add src tests dist/PlanningLeadGenerator.exe
git commit -m "Harden planning search completeness"
```
