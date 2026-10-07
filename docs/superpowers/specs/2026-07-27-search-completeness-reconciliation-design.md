# Search Completeness and Reconciliation Design

## Problem

The 27 July run searched 210 councils but captured only 62 relevant
applications. Its history row reported 61,499 total applications, which is not
a credible weekly total.

Live reproduction identified two independent failure classes:

- Mole Valley's StatMap endpoint ignored the adapter's obsolete nested
  pagination and date-filter payload. It returned 122,950 historical records in
  one response. Because many records lacked a usable received date, 56,511
  survived the generic date filter and inflated the run total.
- Some council portals return a complete-looking but incomplete date search.
  Runnymede reported and paginated eight applications for the week while its
  public application feed contained additional in-range references, including
  relevant applications. More pagination against the same portal result cannot
  recover records that the portal omitted.

The adapter audit also found remaining fixed-size searches, including a
100-record Socrata request, and one-page legacy adapters that do not prove that
the complete date range was retrieved. Other adapter families already paginate
but do not consistently validate the portal-reported total or detect repeated
pages.

The uploaded GeoJSON is the authoritative geographic search area. It may cover
all or only part of any selected council. Every intersecting council must first
be searched for the complete date range; the resulting applications must then
be filtered against the GeoJSON.

## Approved Behaviour

The search will use a two-stage discovery process:

1. retrieve the complete available date range from each council's primary
   portal using a hardened platform adapter;
2. after primary searches and their final retries finish, run a low-rate
   reconciliation pass against the existing PlanIt source and add references
   missing from the primary results.

PlanIt is a supplement, not a replacement. It is itself incomplete for some
councils, while primary portals contain records that PlanIt does not. The union
of both sources provides better coverage than either source alone.

All applications from both stages must pass the same received-date, proposal
exclusion, keyword, and GeoJSON checks. Reconciled applications must not bypass
normal relevance or location filtering.

The application reference is the run-level identity. A reconciled record whose
reference already exists must not create another CSV row, document job, count,
or enrichment job. The primary portal record remains preferred when both
sources contain the same reference because it normally has the best portal URL
and document metadata.

## Complete Portal Retrieval

Every adapter must make completeness explicit rather than relying on an
arbitrary first response.

Platform adapters will use the portal's supported pagination mechanism:

- numbered pages continue until the reported total is reached or a page adds no
  new references;
- offset APIs continue in stable page-size increments until the total is
  reached or the final short page is returned;
- cursor APIs continue until the service supplies no next cursor;
- portals with a hard result cap split the requested date interval into smaller
  non-overlapping windows and merge the results by reference.

An adapter must not silently return a partial result when:

- the portal reports more records than were retrieved;
- a fixed cap is reached without proof that it is the final page;
- the service repeats a page or cursor before the reported total is reached;
- returned received dates show that the requested server-side filter was
  ignored;
- records needed to validate the requested date range have no usable date.

These conditions produce a retryable completeness error. They are logged with
the fetched count, reported count, page or offset, and affected date window.
Retries remain bounded and use the existing deferred-council queue.

The StatMap adapter will be updated to the current top-level request schema and
will validate that returned records observe the requested date interval. It
must never count the portal's unfiltered historical database as a weekly
result. If the service ignores the date filter, the adapter will use the
current date-specific or weekly-list route rather than accepting the response.

The Socrata adapter will paginate with offsets beyond its current 100-record
limit. Remaining one-page adapters will either implement their portal's
pagination, use bounded date splitting, or raise a visible completeness error
when completeness cannot be established.

Application dates that are missing or unparseable cannot be counted as
in-range applications. They may be retried through detail enrichment when a
detail page can establish the received date; otherwise they are excluded and
reported as undated records rather than admitted into the weekly total.

## Reconciliation Pass

Reconciliation runs after all primary council searches and final primary
retries, but before document downloads. This keeps the existing platform worker
queues moving and prevents the secondary service from blocking primary portal
workers.

One dedicated reconciliation worker processes the selected councils in order.
It uses bounded retries, a request timeout, and a short cooldown for rate-limit
responses. The worker count is deliberately fixed at one because PlanIt is a
shared service rather than a separate council host.

For each council, reconciliation will:

1. query every page for the requested date range;
2. reject records outside the requested received-date range;
3. compare references with the primary discovery set;
4. pass only missing records through normal proposal and GeoJSON filtering;
5. add relevant records, output rows, and optional document jobs through the
   same processing path used by primary searches.

A PlanIt timeout or failure must not turn a successful primary council search
into a failed council. It is recorded as a reconciliation warning. If the
primary search failed and reconciliation also cannot retrieve the council, the
council remains failed. If reconciliation recovers usable records after a
primary failure, the log identifies the source and the council is not
incorrectly listed as having no applications.

The run log will show reconciliation progress and a concise per-council
summary: primary count, secondary count, and newly added references. The GUI's
captured-application counter will increase when a newly reconciled application
passes all filters.

## Counting and Archive Semantics

`Total Applications` will be the number of unique, date-valid applications
discovered across the primary and reconciliation stages before proposal,
keyword, and GeoJSON relevance filtering. Historical, out-of-range, and
undated records are not included.

`Relevant Captured Applications` remains the number of unique applications
that pass all proposal, keyword, date, and GeoJSON checks.

The no-applications list is decided only after reconciliation. A council belongs
there when at least one source completed successfully and the combined
date-valid result is genuinely empty. A council belongs in the failed list only
when no source produced a usable result and the search ended in an error.

Document download and enrichment begin only after the combined application set
is final. A reference added during reconciliation receives exactly the same
document treatment as a reference found by the primary portal.

## Performance and Safety

Primary platform queues and their worker limits remain unchanged. Reconciliation
adds a serial network pass rather than increasing concurrent pressure on council
or PlanIt servers. For a full 210-council run this may add several minutes, but
it must not hold a primary search worker or trigger document downloads between
councils.

All loops have explicit page, elapsed-time, and no-progress guards. A malformed
portal cannot create an infinite page loop or another 122,950-record in-memory
response. Page results are deduplicated as they arrive so repeated pages do not
inflate counts or memory use.

Cancellation remains responsive during pagination, cooldowns, and
reconciliation. A cancelled run preserves all rows already captured and records
`Cancelled` in the search archive.

## Tests and Release Checks

Regression tests will prove that:

- StatMap's obsolete nested payload and an ignored date filter cannot admit an
  unfiltered historical response;
- StatMap retrieves a complete valid weekly result using its current request
  path;
- Socrata retrieves more than 100 records using stable offsets;
- numbered, offset, cursor, and date-split adapters collect every page exactly
  once;
- a repeated page, cap hit, or reported-total mismatch raises a bounded
  completeness error instead of silently succeeding;
- missing and unparseable dates do not inflate `Total Applications`;
- primary and PlanIt records are merged by reference while preserving the
  primary record;
- secondary-only records pass the same proposal, date, keyword, and GeoJSON
  filters as primary records;
- a partial-council GeoJSON accepts only applications inside the uploaded
  geometry;
- a reconciliation failure does not downgrade a successful primary search;
- reconciled records create one CSV row and at most one document job;
- no-applications and failed-council archive classifications are made after
  reconciliation.

Live smoke checks will cover Mole Valley, Elmbridge, Runnymede, Woking, Camden,
and at least one council from every adapter family. Known references such as
Elmbridge `2026/1681`, Runnymede `RU.26/0904`, and Runnymede `RU.26/0943` will
be used where their dates overlap the test interval.

Before release, the focused tests, full automated suite, a multi-adapter live
search, cancellation test, document-download handoff, and packaged executable
smoke test must pass. The live run report must make any remaining portal outage
or reconciliation warning visible rather than presenting an incomplete result
as complete.
