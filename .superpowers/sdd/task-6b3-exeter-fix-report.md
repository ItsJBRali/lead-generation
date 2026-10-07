# Task 6 B3 Exeter Fix Report

## Status

Implemented the focused resolved-navigation filtering fix without adding
dependencies or accessing or modifying Downloads data.

## RED Evidence

Added the live-shaped Exeter regression before production edits and ran it with
the parent virtual environment interpreter and the worktree `src` directory on
`PYTHONPATH`.

- The regression failed because `iter_document_links` returned both
  `("Related Documents", "MyExeter")` and the genuine proposed-plan PDF.
- The failure confirmed that the raw relative href passed filtering even though
  its resolved URL contained `/related-documents/Related Documents`.
- RED result: `1 failed in 2.09s`.

## GREEN Evidence

- Focused regression: `1 passed in 1.12s`.
- Complete touched test file: `117 passed, 45 subtests passed in 22.14s`.
- Full suite: `265 passed, 93 subtests passed in 29.14s`.
- `git diff --check`: rerun at the final gate after this report was written.

## Implementation

Generic-site and application-tab navigation checks now inspect the resolved
absolute URL for anchor, data-attribute, onclick, GET-form, and embedded document
link extraction paths. Document qualification and yielded hrefs retain their
existing raw-relative behavior, preserving genuine relative application files.

## Self-Review

- Confirmed the live-shaped trailing-slash Exeter source filters the relative
  `Related Documents` anchor titled `MyExeter` while retaining the relative PDF.
- Confirmed resolved URLs are used for exclusion only, so a document-list page
  cannot make an unrelated relative href look downloadable merely because the
  source URL contains `documents`.
- Confirmed explicit associated-source traversal remains separate and unchanged.
- Confirmed page-chrome filtering, session handling, document identity handling,
  recovery behavior, and adapter-specific extraction were not otherwise changed.
- Confirmed no dependencies, generated outputs, or Downloads data were changed.

## Concerns

None.
