# Document Download Throughput Design

## Problem

Document discovery and downloads currently run inside each council search worker. A slow or unavailable attachment therefore prevents that worker from moving to another council. Each failed attachment can also exhaust five low-level attempts and then enter a second batch retry after a ten-second cooldown. That cooldown is held inside the global two-batch download gate, so repeated failures can stop almost all useful work.

Evidence from the 21 July run shows applications with 12 files occupying a search worker for almost five minutes, plus a 13-minute period with no completed download. This matches the retry and worker-blocking path in `run_lead_search` and `download_pdf_documents`.

## Approved Behaviour

The run is split into three visible phases:

1. Search all selected councils and capture matching application rows.
2. Download documents for the captured applications with at most two document workers.
3. Enrich the CSV rows from the downloaded PDFs.

Council progress must not wait for document downloads. The GUI must show document progress as `x of y applications downloaded` before switching to the existing enrichment progress.

## Download Queue

Each matching application creates a document job containing the council, reference, application record, output folder, and CSV row. Jobs are processed only after the normal and final-retry council search phases finish.

Two workers process document jobs. A temporary rate-limit, service outage, timeout, or connection failure defers the affected job until all other first-attempt jobs have run. Deferred jobs receive one final pass after a bounded cooldown. Missing files and other permanent failures are logged immediately and do not trigger a queue-wide cooldown.

Downloads remain limited to two concurrent applications. This preserves the existing protection against council throttling while allowing unrelated portals to continue making progress.

## Efficiency And Resilience

Document work must not be classified as a council search failure. A document failure affects only that application and is recorded in the run log.

The downloader will:

- avoid a second batch retry for permanent HTTP failures such as 404;
- release its concurrency permit before any retry cooldown;
- reuse document-session state within an application where supported;
- check cancellation between documents, before cooldowns, and before deferred retries;
- retain successfully downloaded files when only part of an application fails;
- count an application under `Captured Documents` when at least one document was saved.

PDF enrichment runs for every queued application folder, including folders with no successful download, so its CSV fields retain the existing `Failed` values when no usable source exists.

## Cancellation

Cancellation stops scheduling new council, document, and enrichment work. Active network calls may finish, but no fixed retry sleep should make cancellation appear stuck. The run is recorded as `Cancelled`, and already captured CSV rows and files remain available.

## Tests

Regression coverage will prove that:

- all council searches finish before document downloads begin;
- document progress reports the queued application count;
- document failures cannot mark a council as failed;
- a 404 is not deferred or followed by the ten-second cooldown;
- a temporary failure is retried at the end of the document queue;
- cancellation skips remaining document jobs;
- successful files still contribute to `Captured Documents` and PDF enrichment;
- the GUI displays download and enrichment phases without changing layout dimensions.

The full automated suite and packaged executable smoke test must pass before release.
