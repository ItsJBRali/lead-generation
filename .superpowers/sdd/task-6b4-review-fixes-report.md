# Task 6 B4 Final Review Fixes Report

## Status

Implemented all three final-review fixes in `leads.py` with focused regressions in
`test_leads.py`. No dependencies were added and no Downloads data was accessed or
modified.

## RED Evidence

Each regression was added and run with the parent virtual environment and the
worktree `src` directory on `PYTHONPATH` before its production change.

- Exact/fuzzy candidate selection: the later exact-match case returned the earlier
  fuzzy URL first, and the ambiguous case returned both fuzzy URLs. RED result:
  `2 failed, 1 passed, 1 subtest passed`.
- Nested Enterprise list TLS replacement: the source loaded, the nested list raised
  a certificate error, and fallback isolation converted it to an empty list. The
  downloader stopped without creating a replacement opener. RED result: `1 failed`.
- Element-backed chrome filtering: footer data attributes, onclick links, GET forms,
  iframe/embed/object links, and a navigation associated-source anchor were all
  emitted. RED result: `2 failed`.

## GREEN Evidence

- Candidate selection plus prior identity regressions: `3 passed, 5 subtests passed`.
- Nested TLS replacement plus prior session/Arcus regressions:
  `3 passed, 2 subtests passed`.
- Chrome regressions plus prior ordinary-anchor coverage: `3 passed`; the unchanged
  script-model regression separately passed.
- Complete B3/B4 focused regressions: `18 passed, 7 subtests passed in 5.16s`.
- Complete touched test file: `121 passed, 48 subtests passed in 21.61s`.
- Full suite: `269 passed, 96 subtests passed in 26.13s`.
- `git diff --check`: clean at the final gate.

## Implementation

- Candidate identities are computed once. Deduplicated exact URLs take precedence;
  non-generic fuzzy matching is used only when no exact match exists and all fuzzy
  labels resolve to one distinct URL. Empty and ambiguous fuzzy identities are
  rejected, while generic titles remain filename-exact.
- Optional document-list failures still return an empty list for ordinary parser and
  network errors. TLS certificate and compatibility conditions now propagate through
  wrapped exception chains so the downloader can replace its opener, rediscover the
  source/list under that session, and download the refreshed candidate.
- Page-chrome filtering now applies to data attributes, onclick links, GET forms,
  iframe/embed/object links, and associated-source anchors. Script-model documents
  remain independent of DOM ownership and are unchanged.

## Self-Review

- Confirmed exact matches may retain multiple exact URLs, while repeated fuzzy labels
  for one URL remain unambiguous and multiple fuzzy URLs return no fallback.
- Confirmed generic attachment identity still comes from filename/name URL values and
  empty candidate identities cannot match.
- Confirmed ordinary optional Publisher, Enterprise, and Arcus failures remain
  isolated, while certificate and compatibility errors can be found in either the
  wrapper text or its cause/context chain.
- Confirmed replacement attempts remain bounded and preserve prior Publisher,
  Northgate, Bath, Glamorgan, and Arcus session behavior.
- Confirmed every requested DOM-backed extraction path checks page chrome, genuine
  main-content links remain, and public-access script models remain covered.
- Confirmed recovery behavior, drawing-only enrichment rules, council failure
  accounting, dependencies, generated outputs, and Downloads data were not changed.

## Concerns

None.
