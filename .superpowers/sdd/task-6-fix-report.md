# Task 6 Focused Remediation Report

## Scope

Implemented only the two live-run code defects from `task-6-report.md`:

1. Generic document-link discovery now skips missing or null derived titles, does not apply string operations to `None`, and continues to yield valid later links.
2. Management plans are classified as narrative documents. They cannot become eligible enrichment sources, including when their filename or text contains proposed/existing, plan, drawing number, scale, or revision markers.

The live Downloads output was not opened, changed, or rerun.

## Test-Driven Evidence

### RED

Before the implementation changes:

- `test_iter_document_links_skips_missing_derived_title_and_keeps_valid_link` failed because the iterator emitted `('/documents/download?id=missing-title', None)`.
- `test_management_plan_never_enriches_even_with_drawing_markers` failed because `SANG_LANDSCAPE_AND_ECOLOGICAL_MANAGEMENT_PLAN.pdf` was sent onward for text confirmation.

### GREEN

- Null-title links are discarded before classification or construction of a `PlanningDocument`; a following titled document is still discovered.
- `_is_document_link_text(None, ...)` is safe.
- Management plans are rejected during preclassification when named, and during classification when the management-plan marker occurs later in text.
- The enrichment-level regression proves all four fields remain `Failed`, no sources are recorded, and no management plan becomes eligible.

## Verification

- Focused null-title regression: `1 passed, 94 deselected`
- Focused management-plan regression: `1 passed, 25 deselected`
- Relevant suites (`test_leads.py`, `test_enrichment.py`): `121 passed, 41 subtests passed`
- Full suite: `231 passed, 89 subtests passed`
- `git diff --check`: passed

## Constraints Confirmed

- No third-party dependencies added.
- No live Downloads files accessed or modified.
- Changes are limited to the requested two source modules and their tests, plus this report.
