# Drawing-Only Enrichment and Document Recovery Design

## Problem

The 21 July enrichment run contains 85 captured applications. A row-by-row audit found:

- 17 rows with clearly invalid names, phone numbers, or addresses;
- 22 rows containing enrichment data even though no qualifying proposed or existing drawing was identified by its saved filename;
- 23 rows with no downloaded PDF, despite several councils currently publishing documents for those applications;
- two council search failures, Birmingham and Powys.

The enrichment code currently reads every PDF, copies agent names and addresses from application forms, accepts portal agent metadata, and treats statements, reports, letters, and surveys as professional-contact sources. Loose context and format checks then admit drawing coordinates as phone numbers, site or client addresses as company addresses, and copyright or revision text as company names.

Document discovery has a separate failure mode. Errors from individual source pages and document-list APIs are swallowed and converted into an empty document list. The application is therefore logged as having no listed documents and never reaches the final retry queue. Camden and Exeter also publish files through document services that are not present in their primary portal application records.

## Approved Behaviour

Architect and company enrichment may come only from proposed or existing plans, drawings, elevations, sections, or layouts. Existing-only drawing files are a narrow exception to the earlier rule that excludes files containing `existing`. Existing reports, statements, forms, photographs, surveys, and all other non-drawing documents remain excluded.

Application forms, portal agent metadata, statements, reports, assessments, surveys, letters, notices, certificates, schedules, and consultation documents must contribute no enrichment output. They must not be used to fill a field that is missing from an eligible drawing.

Each enrichment field remains independent. When an eligible drawing supplies only a company name and email address, those values are recorded and the phone and company-address fields remain `Failed`.

## Drawing Source Gate

Document eligibility is decided before contact extraction.

A PDF is eligible when its portal title, filename, or PDF title text establishes that it is a proposed or existing plan, drawing, elevation, section, or layout. Clear names such as `Proposed Elevations.pdf` and `Existing Plan.pdf` are accepted directly.

Ambiguous names such as `PROPOSED_CAR_PARK.pdf` or `Drawings.pdf` are accepted only when the PDF also contains drawing-sheet evidence, such as a drawing number, scale, revision block, drawn-by field, or comparable title-block structure. Narrative-document markers take priority: a statement or report is rejected even when its title mentions proposed plans.

Existing-only files are downloaded only when their available metadata identifies a drawing class. Other files containing `existing` continue to be excluded. Executables remain excluded.

## Contact Extraction

Only text from an eligible drawing is passed to the enrichment extractor. The application applicant and site address supplied by the search result may be used only as exclusion data; no value from those fields is copied into enrichment output.

Candidates are grouped into coherent title-block contact areas instead of being scored against a broad page window. A drawing author, architect, or design company may be recorded when it is identified by an architectural role, a prepared-by or drawn-by label, or a company contact block. A company merely named as the client, applicant, owner, contractor, consultant in notes, or project subject is rejected.

Validation rules include:

- reject copyright notices, all-rights-reserved text, revision fields, drawing-status text, scale/date/check/approval labels, prose, headings, and corrupted OCR fragments as names;
- reject client, applicant, owner, council, case-officer, and site-contact blocks;
- reject decimal coordinates, dimensions, dates, drawing numbers, repeated-zero fragments, and implausible UK prefixes as phone numbers;
- accept email addresses only when they are valid, non-government professional addresses in the same title-block contact area;
- accept a company address only when it contains a valid postcode, belongs to the same professional contact block, and does not substantially match the application site address;
- deduplicate spelling and OCR variants without merging genuinely different firms.

No company details are inferred from a website domain or from other applications.

## Document Discovery and Retry

Document discovery will report documents, successful sources, and transient source failures separately. A failed source must never be treated as proof that the council listed zero documents.

On the first document pass, all documents discovered successfully are downloaded. If any document source failed, the job is deferred after preserving successful files. The final pass re-runs discovery, merges newly found documents by stable URL or document identity, and downloads only files not already saved.

A genuine empty result is recorded only when at least one authoritative document source completed successfully and explicitly returned no files. The run log will distinguish:

- no documents currently published by the council;
- document discovery deferred for retry;
- document discovery failed after the final retry;
- partial document capture with unresolved sources.

Publisher and AJAX document-list failures must propagate into this retry state rather than returning an unqualified empty list.

Council-specific source resolution will include:

- Camden's `camdocs.camden.gov.uk/CMWebDrawer` record service, keyed by application reference;
- Exeter's related-documents service, keyed by application reference;
- Bath's Publisher document route on the active application host;
- Wandsworth's associated-document link when the application is publicly available;
- the existing portal-specific sources for Idox, Northgate, Tascomi, Arcus, Civica, and other supported families.

Search failures remain separate from document failures. Birmingham and Powys will receive bounded targeted verification. When their primary portal is responsive but automation is blocked, the existing public metadata fallback must be attempted without allowing a browser challenge or fallback request to hang the worker indefinitely.

## Recovery of the 21 July Run

After implementation, every one of the 85 rows will be processed again from a clean enrichment result. The recovery pass will:

1. rediscover and download currently available files for applications with missing or partial folders;
2. retain only permitted downloaded files under the existing file-exclusion rules plus the approved existing-drawing exception;
3. rerun drawing-only enrichment for every row;
4. preserve the original CSV and write a corrected spreadsheet beside it;
5. produce an audit showing the eligible source document for each populated field and the reason for every remaining `Failed` field.

A remaining `Failed` value is acceptable only when no qualifying drawing is published, the qualifying drawing is unreadable after bounded OCR, or the drawing does not contain that field. Junk values are not an acceptable substitute for missing data.

## Tests and Release Checks

Regression tests will prove that:

- application forms and portal agent metadata cannot populate any enrichment field;
- reports, statements, surveys, letters, and assessments cannot populate any enrichment field;
- proposed, existing-only, and combined existing/proposed drawings are eligible;
- ambiguous drawing names require title-block evidence;
- the `PL/26/05353/FA` coordinate-like values are rejected as phone numbers and its client is rejected as the architect;
- the XL Planning copyright sentence is rejected while a clean title-block company name remains eligible;
- client and site addresses are rejected even when they appear near professional details;
- partial valid enrichment preserves available fields and marks only missing fields `Failed`;
- Camden and Exeter produce their correct document-source URLs;
- transient and partial discovery failures enter the final document retry queue;
- a confirmed successful empty document list does not retry indefinitely;
- browser challenges and fallback requests have bounded completion times.

The focused tests must fail before implementation and pass afterward. The full automated suite, the corrected-run audit, and the packaged executable smoke test must pass before release.
