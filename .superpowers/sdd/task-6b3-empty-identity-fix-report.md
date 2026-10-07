# Task 6 B3 Empty Identity Fix Report

## Status

Implemented the focused empty fallback identity fix without adding dependencies or
accessing or modifying Downloads data.

## RED Evidence

Added the live-inspired Northgate regression before production edits and ran it
with the parent virtual environment interpreter and the worktree `src` directory
on `PYTHONPATH`.

- `Forms` failed because the candidate list began with the unrelated material
  schedule URL emitted from a `-` label, followed by the intended application
  form URL.
- `Additional Details` failed because the candidate list began with the unrelated
  application form URL emitted from a `-` label, followed by the intended material
  schedule URL.
- RED result: 2 failed subtests. Both failures showed that an empty normalized
  candidate identity entered the partial-match branch and placed the unrelated URL
  first.

## GREEN Evidence

- Focused regression: `1 passed, 2 subtests passed in 1.22s`.
- Prior B3 regressions: `12 passed, 2 subtests passed in 6.02s`.
- Complete touched test file: `116 passed, 45 subtests passed in 22.50s`.
- Full suite: `264 passed, 93 subtests passed in 29.40s`.
- `git diff --check`: rerun at the final gate after this report was written.

## Implementation

The non-generic fuzzy fallback branch now requires a non-empty candidate identity
before evaluating either containment expression. Exact non-generic title matching
is unchanged, and generic filename identity matching remains exact.

## Self-Review

- Confirmed punctuation-only labels such as `-` can no longer match any non-empty
  wanted identity through Python's empty-string containment behavior.
- Confirmed exact non-generic matches and non-empty partial matches remain enabled.
- Confirmed generic filename matching and session/TLS handling were not changed.
- Confirmed application forms remain downloadable while the existing enrichment
  exclusion policy is unchanged.
- Confirmed no dependencies, recovery behavior, generated outputs, or Downloads
  data were changed.

## Concerns

None.
