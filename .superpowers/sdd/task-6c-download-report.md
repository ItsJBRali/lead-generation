# Task 6C: Rate-limit and duplicate download hardening

## Scope

Changed only the approved download/recovery implementation and tests. The
user's Downloads directory was not accessed, and no executable was built.

## Root-cause findings

1. HTTP 429 and 503 shared the same 5-15 second retry path, and the final 429
   did not leave a host cooldown for the next application.
2. Recovery compared saved filenames almost exactly, so response-header names
   with repeated extensions and generated collision suffixes were downloaded
   again.
3. Successful payloads were always written to a unique path without checking
   existing files in the same application folder.
4. Associated-document navigation labels passed the generic document-link
   test and were emitted as downloadable files.

## TDD evidence

Initial focused red run:

`11 failed, 4 passed, 146 deselected`

The failures reproduced the missing 60-second 429 default, 120-second bound,
retained final cooldown, cancelable host wait, same-folder payload
deduplication, repeated-extension/collision matching, and source-only
associated-document labels. The queue-fairness and cross-folder isolation
controls already passed.

Focused green run after the implementation:

`12 passed, 146 deselected, 3 subtests passed`

A compatibility review then found that the new canonical matcher no longer
accepted an exact extensionless filename. A regression test failed first:

`1 failed, 28 deselected`

After preserving that existing behavior, the canonical-matching group passed:

`4 passed, 25 deselected`

## Decisions

- A 429 without `Retry-After` waits 60 seconds; `Retry-After` is respected and
  bounded at 120 seconds.
- Generic 503 retries retain their existing shorter bounded delays.
- Intermediate 429 cooldowns are cleared after the explicit wait, while a
  final 429 leaves the host cooldown active for the next application.
- Download waits poll cancellation through the existing cancelable-delay
  helper; retries remain bounded at their existing attempt counts.
- Source-page and JSON discovery retries use the same 429-aware delay policy.
- A newly received payload is compared by size and SHA-256 only against files
  in its destination application folder. An identical existing payload counts
  as captured and no new copy is written.
- Recovery strips repeated known extensions and recognizes generated `-2` and
  later collision suffixes on saved files without stripping suffixes from the
  expected document identity.
- Associated-document labels remain discoverable as source pages but are not
  yielded as document files; direct documents found after following the source
  remain unchanged.

## Verification

- Lead and recovery suites:
  `158 passed, 51 subtests passed`
- Complete suite:
  `335 passed, 99 subtests passed`
- `git diff --check`: clean

## Commit

Message: `Complete rate-limited document recovery`

The final commit hash is reported in the task completion response. A commit
cannot contain its own literal final hash because changing this tracked report
changes that hash.
