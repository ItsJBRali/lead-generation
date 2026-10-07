# Task 6 Remediation B3 Report

## Status

Implemented transient document recovery and logical document identity handling in
`leads.py` and `recovery.py` without adding dependencies or touching Downloads data.

## RED Evidence

Each focused regression was added before production edits and run with the parent
interpreter and the worktree `src` directory on `PYTHONPATH`.

- GUI temporary 404 queue retry: failed because events stopped after
  `first:REF-1`, `first:REF-2`; `retry:REF-1` never occurred.
- Top-level temporary 404 retry: failed with one download call instead of two.
- Per-file 404 handling: failed because the missing file was recorded immediately
  instead of appearing in `transient_documents`; its valid same-host sibling did run.
- Session-bound Publisher refresh: failed when the second opener reused the first
  opener's cached Publisher URL and received HTTP 404.
- Generic `Plan` identity: failed by selecting `OtherPlan.pdf` instead of the exact
  `SitePlan.pdf` filename identity.
- Page-chrome filtering: failed by emitting the Exeter breadcrumb, Chelmsford
  `externalDocuments` tab, and Westminster footer PDF.
- Associated-source traversal: failed by emitting `Plans & Documents` as a file and
  never reaching the nested proposed plan.
- Rotating recovery URL reconciliation: failed because the stale SitePlan failure
  remained after its replacement succeeded.
- Bounded late recovery: failed with two attempts instead of the required third and
  final attempt.

## GREEN Evidence

- Focused regressions: `9 passed in 2.89s`.
- Complete touched files: `137 passed, 41 subtests passed in 21.52s`.
- Full suite: `260 passed, 89 subtests passed in 24.53s`.
- `git diff --check`: clean before the report was written; rerun at the final gate.

## Implementation

- HTTP 404 is deferred per file on non-final passes and never blocks same-host
  siblings; final-pass 404 remains one recorded failure.
- GUI document jobs naturally carry deferred 404 files into the existing bounded
  final queue pass.
- Publisher `/publisher/docs/` and Northgate `/Document/Download` URLs are refreshed
  through the active opener and are excluded from reusable candidate caches. Static
  candidate caching and Arcus direct-first behavior remain intact.
- Generic document labels use filename/name query values or path filenames as their
  logical identity, preventing cross-attachment substitution.
- Breadcrumb, navigation, footer, and Idox active-tab links are excluded from file
  emission while explicit associated-source labels remain followable.
- Associated source traversal uses a visited set and a maximum depth of two links.
- Recovery clears only failures with the same normalized source and logical identity
  when a replacement URL succeeds or matches an existing file.
- Recovery performs one additional cooldown-controlled late pass for unresolved
  items, preserving existing files and stopping after that pass.

## Self-Review

- Confirmed rate-limit host blocking remains unchanged for 429/503 and other
  transient host failures; only 404 is per-file.
- Confirmed final 404 handling records one failure and the retry loops are finite.
- Confirmed safe static candidate caching remains available.
- Confirmed associated traversal cannot loop and does not become general crawling.
- Confirmed stale failure reconciliation requires both a normalized source URL and
  matching logical identity, so unrelated failures remain.
- Confirmed drawing-only enrichment rules, Tascomi authoritative-empty handling,
  non-document council accounting, and the original CSV write behavior were not
  changed.
- Confirmed the intentional deletion of `.superpowers/sdd/task-6-fix-report.md`
  remains present and no build or distribution output is included.

## Concerns

None.
