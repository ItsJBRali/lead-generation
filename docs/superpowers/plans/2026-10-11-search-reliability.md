# Search reliability and measurement

Implement the six approved improvements on the existing development branch.

1. Allocate a unique timestamped run folder. Never truncate an earlier run.
2. Use the first parseable received date, otherwise the validated date, in discovery, matching, counts and CSV output.
3. Retry failed application details once; retain valid leads and expose unresolved essential fields as partial council coverage.
4. Persist run settings, source results, leads, document progress and enrichment in SQLite. Resume an explicitly selected run only when settings and boundary match. Reuse PDF extraction by content hash and extraction version; retain retryable failures.
5. Add a read-only, repeatable audit of modern Agile, AchieveForms and Wiltshire for small, large and historical windows. Capture totals/continuations and range consistency; report unknown completeness when evidence is insufficient.
6. Export per-council stage durations, request/retry counts, download counts and OCR/cache statistics.

Validation: regression tests for separate same-day runs, date priority/fallback, detail retries/partial status, interrupted-run resume, cache invalidation, completeness evidence and metrics. Run the full Windows test/build workflow and record live audit limitations accurately.
