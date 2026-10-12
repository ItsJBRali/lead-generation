# Live completeness audit — 11 October 2026

Read-only checks covered one day, 30 days, two 15-day partitions, and the same
30-day interval in the previous year. Evidence is saved in the accompanying JSON.

## Wiltshire

- The one-day search returned a successful empty array. The adapter now accepts
  that live response format as an empty result rather than a failed request.
- The recent 30-day window returned 516 unique records. Its two partitions
  returned 309 and 207; their union matched the broad search exactly.
- The historical window returned 650 records.
- Returned dates were in the requested windows and did not require inferred dates.
- The response exposes no verified authoritative application total or continuation
  contract. These consistency checks support the sampled windows, but completeness
  remains Unknown. Salesforce performance totals are excluded from result totals.

## Modern Agile — New Forest National Park

The shared HTTP path returned HTTP 401 for all five windows. A diagnostic request
using the former direct transport returned HTTP 429. A single-slash shared request
also returned 401. Neither path provided usable results, so pagination and server
caps could not be verified. No unsupported paging parameters were added.

## AchieveForms — former Nuneaton portal

The catalogue's former planning-search page no longer exposes an AchieveForms
definition URI. All five checks failed at definition discovery. The application's
existing runtime override already routes Nuneaton to its newer Tascomi portal.
The old endpoint cannot establish a current AchieveForms date-window contract.

## Repeatability

Run `python -m lead_generator.planning.completeness_audit --councils ... --output audit.json`.
`--catalogue-portals` audits original catalogue URLs, including superseded portals.
The report stores response hashes, sanitized URLs, counts, date anomalies and
partition differences. Session identifiers and response bodies are not retained.
