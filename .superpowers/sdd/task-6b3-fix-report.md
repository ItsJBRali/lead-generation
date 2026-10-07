# Task 6 B3 Focused Fix Report

## Status

Implemented the release-blocker fix for TLS fallback session preservation and
generic attachment identity. No dependencies were added and no Downloads data
was accessed or modified.

## RED Evidence

The focused regressions were added before production edits and run with
`C:\Users\AliBouhaddou-Robinso\OneDrive - Penchard\Documents\LeadGen\.venv\Scripts\python.exe`
and `PYTHONPATH` set to the worktree `src` directory.

- Publisher and Northgate session-bound subtests failed because the unverified
  opener retried the candidate discovered by the verified session and received
  HTTP 404 instead of rediscovering the source.
- Two generic `Plan` documents failed by saving as `Plan.pdf` and `Plan-2.pdf`
  instead of `SitePlan.pdf` and `FloorPlan.pdf`.
- Recovery failed by retrying both generic identities after `SitePlan.pdf`
  existed, leaving the SitePlan failure uncleared.
- RED result: 4 failures across 3 tests and 2 subtests.

## GREEN Evidence

- New focused regressions: `3 passed, 2 subtests passed in 1.46s`.
- Prior focused B3 regressions: `10 passed in 2.95s`.
- Complete touched tests: `140 passed, 43 subtests passed in 21.61s`.
- Full suite: `263 passed, 91 subtests passed in 25.45s`.
- `git diff --check`: clean before this report; rerun at the final gate.

## Implementation

- TLS compatibility and certificate replacement now remains owned by the
  downloader. A session-bound failure clears stale candidates, clears any
  source cache entry, and rediscovers through the replacement opener before
  downloading its refreshed candidate.
- Source discovery re-raises TLS replacement conditions instead of hiding a
  replacement opener in local state. Static and direct candidates retain their
  existing same-URL fallback behavior, and fallback transitions remain bounded.
- Generic labels use their URL filename/name identity for local filenames,
  logical matching, existing-file reconciliation, and stale-failure clearing.
  Non-generic display-title naming and matching remain unchanged.

## Self-Review

- Confirmed both Publisher `/publisher/docs/` and Northgate
  `/Document/Download` classifiers take the rediscovery path.
- Confirmed the stale verified-session candidate is never opened by the
  replacement session in the new regression.
- Confirmed distinct generic attachments create distinct meaningful files and
  an existing first identity does not satisfy or clear the second.
- Confirmed ordinary static/direct retries, bounded attempts, prior B3
  behavior, drawing-only constraints, and unrelated recovery accounting remain
  covered by passing tests.

## Concerns

None.
