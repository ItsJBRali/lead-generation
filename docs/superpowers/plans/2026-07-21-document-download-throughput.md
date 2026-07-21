# Document Download Throughput Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prevent document downloads and retries from blocking council discovery while retaining comprehensive, rate-limit-aware attachment capture.

**Architecture:** `run_lead_search` will capture lightweight document jobs during council discovery and process them in a separate two-worker phase. A single-pass download helper will distinguish temporary from permanent failures so only temporary failures are retried after the first document queue drains. The existing GUI status line will display document progress before switching to PDF enrichment progress.

**Tech Stack:** Python 3.11+, `threading`, `queue.Queue`, `urllib`, CustomTkinter, `unittest`/`pytest`, PyInstaller.

## Global Constraints

- Run at most two application document batches concurrently.
- Finish normal and final-retry council searches before starting any document download.
- Retry only temporary document failures at the end of the document queue.
- Do not treat document failures as council search failures.
- Keep every successfully downloaded file and count an application once when at least one file is saved.
- Preserve the existing PDF enrichment fields and exclude application forms from phone/email extraction.
- Make cancellation responsive between downloads and during retry cooldowns.
- Do not add dependencies or change the output directory structure.

---

### Task 1: Classify And Bound Document Retries

**Files:**
- Modify: `tests/test_leads.py`
- Modify: `src/lead_generator/planning/leads.py`

**Interfaces:**
- Produces: `DocumentDownloadBatchResult(downloaded_count, transient_documents)`.
- Produces: `_download_pdf_documents_once(documents, destination, log=None, should_cancel=None, defer_transient=True)`.
- Produces: `_is_transient_document_error(exc) -> bool`.
- Preserves: `download_pdf_documents(...) -> int` for existing callers.

- [ ] **Step 1: Write a failing permanent-error regression test**

Add a test which patches `download_document_file` to raise `HTTPError(..., 404, ...)`, patches `sleep`, calls `download_pdf_documents`, and asserts the link is attempted once and no cooldown sleep occurs.

- [ ] **Step 2: Run the permanent-error test and verify RED**

Run:

```powershell
python -m pytest tests/test_leads.py -k "does_not_retry_permanent_404" -q
```

Expected: FAIL because the current implementation retries every failed link after ten seconds.

- [ ] **Step 3: Write a failing shared-session regression test**

Create two documents with the same `source_url`, use a fake opener, and assert the source page is fetched only once while both documents download successfully.

- [ ] **Step 4: Run the shared-session test and verify RED**

Run:

```powershell
python -m pytest tests/test_leads.py -k "reuses_source_page_session" -q
```

Expected: FAIL because each document currently creates a new opener and reloads its source page.

- [ ] **Step 5: Implement one-pass download reporting**

Add the result type and split the current downloader so actual network work happens only while the semaphore is held:

```python
@dataclass(slots=True)
class DocumentDownloadBatchResult:
    downloaded_count: int = 0
    transient_documents: list[PlanningDocument] = field(default_factory=list)


def _is_transient_document_error(exc: Exception) -> bool:
    if isinstance(exc, HTTPError):
        return exc.code in {403, 408, 425, 429, 500, 502, 503, 504}
    if isinstance(exc, ValueError):
        return False
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError)) or "timeout" in str(exc).casefold()
```

`_download_pdf_documents_once` must log permanent failures immediately, return temporary failures to its caller, and check `should_cancel` between documents. `download_pdf_documents` must call the helper, wait outside `_DOCUMENT_DOWNLOAD_GATE`, and retry only `transient_documents`.

- [ ] **Step 6: Reuse the regular HTTP session within one application**

Allow `download_document_file` and `_download_document_file` to accept a shared opener and source-document cache. Update `source_document_candidates` to cache parsed source-page candidates by `source_url`, then filter the cached candidates by each document title.

- [ ] **Step 7: Bound low-level rate-limit waiting**

Set `DOCUMENT_DOWNLOAD_ATTEMPTS = 2` and cap each document retry delay at `DOCUMENT_DOWNLOAD_MAX_RETRY_DELAY_SECONDS = 15.0`. The end-of-queue retry supplies the later attempts without repeatedly blocking on one link.

- [ ] **Step 8: Run focused download tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_leads.py -k "document_download or download_pdf_documents or source_page_session" -q
```

Expected: PASS.

### Task 2: Separate Council Search And Document Phases

**Files:**
- Modify: `tests/test_leads.py`
- Modify: `src/lead_generator/planning/leads.py`

**Interfaces:**
- Produces: `DocumentDownloadJob(reference, council, application, folder, row, pending_documents, downloaded_count)`.
- Adds: `document_progress: DocumentProgressCallback | None` to `run_lead_search`.
- Consumes: `_download_pdf_documents_once` from Task 1.

- [ ] **Step 1: Write a failing phase-order regression test**

Use two councils with `worker_count=1`. Record calls from council discovery and document downloading, then assert both `search:<council>` entries occur before the first `download:<reference>` entry.

- [ ] **Step 2: Run the phase-order test and verify RED**

Run:

```powershell
python -m pytest tests/test_leads.py -k "searches_all_councils_before_downloading_documents" -q
```

Expected: FAIL because the current worker downloads each matched application before taking the next council.

- [ ] **Step 3: Write failing retry-queue and progress tests**

Cover these outcomes:

```python
assert events == ["first:REF-1", "first:REF-2", "retry:REF-1"]
assert document_progress == [(0, 2), (1, 2), (2, 2)]
assert result.failed_councils == []
```

The first application temporarily fails, the second succeeds, and the first is retried only after the second has run.

- [ ] **Step 4: Run the queue tests and verify RED**

Run:

```powershell
python -m pytest tests/test_leads.py -k "document_jobs_retry_at_end or document_progress" -q
```

Expected: FAIL because there is no separate document queue or callback.

- [ ] **Step 5: Capture document jobs during council discovery**

Replace inline enrichment and downloading with lightweight job creation:

```python
lead_folder = create_lead_folder(output_dir, target.authority, application)
document_job = DocumentDownloadJob(
    reference=reference,
    council=target.authority,
    application=application,
    folder=lead_folder,
    row=row,
)
save_row(row, enrichment_job=enrichment_job, document_job=document_job)
```

The worker must save the CSV row and continue to the next application/council without document network access.

- [ ] **Step 6: Implement the two-worker document phase**

After the council final-retry phase, process all first-attempt jobs with `min(MAX_CONCURRENT_DOCUMENT_BATCHES, len(jobs))` workers. Store only jobs with temporary failures for a final pass. Sleep once, outside the semaphore, before that final pass, and make the cooldown cancellation-aware.

- [ ] **Step 7: Isolate document failures from council results**

Catch document discovery and download exceptions inside the document worker. Log them against the application reference, mark that application processed, and do not call `save_failure` or change `failed_councils`.

- [ ] **Step 8: Preserve document counts and enrichment jobs**

Call `add_captured_document_application` as soon as a job has saved at least one file. Keep every `EnrichmentJob` created during search so folders with partial or no downloads retain the existing `Failed` values during enrichment.

- [ ] **Step 9: Run focused search orchestration tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_leads.py -k "run_lead_search or document_jobs or document_progress" -q
```

Expected: PASS.

### Task 3: Show The Document Phase In The GUI

**Files:**
- Modify: `tests/test_gui.py`
- Modify: `src/lead_generator/planning/gui.py`

**Interfaces:**
- Consumes: `run_lead_search(..., document_progress=callback)` from Task 2.
- Produces: `LeadGeneratorApp._set_document_progress(completed, total, requested=True)`.

- [ ] **Step 1: Write a failing GUI text test**

Add a test using `FakeLabel`:

```python
LeadGeneratorApp._set_document_progress(
    SimpleNamespace(enrichment_label=label), 3, 8, requested=True
)
assert label.text == "3 of 8 applications downloaded"
```

- [ ] **Step 2: Run the GUI test and verify RED**

Run:

```powershell
python -m pytest tests/test_gui.py -k "document_progress" -q
```

Expected: FAIL because `_set_document_progress` does not exist.

- [ ] **Step 3: Wire document progress through the GUI message queue**

Pass a `document_progress` callback from `_run_worker`, handle a new `documents` queue message in `_poll_messages`, and update the existing enrichment status label. Do not add a new panel or alter geometry.

- [ ] **Step 4: Keep disabled-download messaging intact**

When downloads are unchecked, continue showing `PDF enrichment not requested`. When checked, initialise the line as `0 of 0 applications downloaded`; enrichment callbacks replace it once document work finishes.

- [ ] **Step 5: Run GUI tests and verify GREEN**

Run:

```powershell
python -m pytest tests/test_gui.py -q
```

Expected: PASS.

### Task 4: Full Verification And Executable

**Files:**
- Modify: `dist/PlanningLeadGenerator.exe`

**Interfaces:**
- Consumes all prior tasks.
- Produces the updated Windows executable tracked through Git LFS.

- [ ] **Step 1: Run formatting and whitespace checks**

Run:

```powershell
git diff --check
```

Expected: no output and exit code 0.

- [ ] **Step 2: Run the complete automated test suite**

Run:

```powershell
python -m pytest -q
```

Expected: all tests pass.

- [ ] **Step 3: Build the executable in clean temporary paths**

Run the repository's PyInstaller command from `README.md`, using `.build-throughput` and `.dist-throughput` as temporary paths, then replace `dist/PlanningLeadGenerator.exe` with the successful build.

- [ ] **Step 4: Smoke-test the packaged executable**

Launch the executable, confirm the process remains running long enough to create the GUI, then close only that launched process. Verify its SHA-256 and Git LFS pointer state.

- [ ] **Step 5: Re-run tests after packaging**

Run:

```powershell
python -m pytest -q
```

Expected: all tests still pass.

- [ ] **Step 6: Review and publish**

Inspect `git diff --stat`, `git diff --check`, and the final branch status. Commit only the source, tests, plan, and executable, then push `enrichment` to `origin`.
