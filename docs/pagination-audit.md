# Planning adapter pagination audit

Audited source: `a713782ad407ced2b9cfcacf69e59ad10bf1f9ba`.

The reported run returned just over 2,000 applications across 210 councils with
blank keywords for a week. The estimated 3,500–5,000 is a statistical benchmark,
not an independently verified count of available records. This audit reproduced
pagination defects in offline tests; it did not rerun that council selection.

## Findings and changes

| Adapter / search path | Finding and resulting behaviour |
| --- | --- |
| Idox PublicAccess | Already discovers page links recursively. Its 100-page guard now raises an incomplete-search error instead of returning a silently truncated list. |
| Generic labelled HTML | Previously read one response. Now follows advertised Next, numbered pager links and ASP.NET postbacks recursively, with deduplication and explicit limits. |
| Atrium | Previously followed only links visible on the first page. Now discovers later pager windows, skips the numbered alias of the current/first page, and parses each page at its own URL. Existing page-size expansion remains. |
| Northgate | Retains recursive URL pagination. Reads reported totals from HTML fragments, fetches queued pages only when needed, and reports its 500-page safety ceiling as incomplete. |
| Civica JSON | Previously stopped on server-capped short pages and advanced by requested size. Now advances by returned row count, honours reported totals, continues to empty when no total is supplied, and reports repeated or prematurely empty pages. |
| Civica HTML; legacy Agile/APAS | Inherit the corrected generic HTML traversal. |
| Arcus | Previously abandoned date splitting if the first split added no IDs. Now continues until thresholds clear. An indivisible capped window reports incomplete; an explicit limit can be satisfied across disjoint date windows. |
| Ocella | Added advertised HTML pagination. Existing received-date cap splitting remains, and indivisible capped windows now report incomplete. Explicit limits can be satisfied across split windows. |
| Tascomi | Removed the silent 250-page stop. Continues page requests and overlapping results, rejects repeated responses, and reports the 1,000-page safety ceiling. Weekly-list responses also follow advertised controls. |
| EnterpriseStore; AppSearchServ; Astun | Previously consumed one result response. Now follow advertised HTML links/postbacks, applying date filtering before the explicit result limit. |
| Fastweb | Preserves Next traversal and adds shared cycle protection and numbered/postback handling. |
| CCED | Uses current form state and the exact page event argument. Handles numbered controls and forward ellipsis windows. Also fixes parsing result fragments without a body element. |
| StatMap | Previously requested only page zero. Now advances the existing page index with fixed request size until empty, continues overlapping rows and rejects repeated responses. |
| Socrata | Previously requested one batch, normally 100 records. Now uses raw-row offsets and stable ordering, filters before the result limit, and rejects repeated responses. |
| HtmlList; QueryForm; WebForms; NorthLincs | Follow advertised continuation. QueryForm's no-form fallback no longer imposes an implicit 100-result limit. |
| Taunton Deane | Follows pagination after ViewAll, or after the normal result response when ViewAll is absent. |
| Central Bedfordshire | Retains continuation and applies date filtering before counting the explicit limit. |
| Tandridge; East Sussex; Elmbridge | Follow continuation from their custom search responses. Elmbridge still checks its known busy-page response. |
| Colchester | Retains page number and paging-cookie protocol. Removes the silent 250-page ceiling and reports empty/repeated responses when MoreRecords remains true. |
| Telford | Existing per-day searches now follow advertised pagination within each day. A server cap with no continuation control remains unverified. |
| West Dunbartonshire | Follows continuation and parses each returned page's application forms. |
| Weekly CSV Atrium | Existing traversal covers the requested weeks and deduplicates applications. No page-navigation defect found in the CSV path. |
| Modern Agile JSON | One application/search request; available source and fixtures do not establish a continuation contract. Unchanged and not certified complete. |
| AchieveForms | One weekly lookup response followed by detail lookups. No evidenced paging contract; unchanged. Its current-week lookup is also not proof of arbitrary historical-week coverage. |
| Wiltshire | Custom Salesforce query returns a records collection. No evidenced paging contract; unchanged. |
| Bath; Stratford-on-Avon; Eastleigh; Kensington | Custom bulk responses inspected. No evidenced continuation protocol in the source/fixtures; unchanged and not certified complete. |
| Carmarthenshire | Custom Salesforce response overrides the normal Arcus fetch and reports no threshold. Its bulk-response cap/continuation contract remains unverified. |
| PlanIt fallback | Existing page loop, total handling, repeated-page detection and explicit safety error retained. Assumes the API supplies its total as expected; no live verification in this audit. |

Authority-specific subclasses inherit their family fixes unless listed separately:

| Family | Authority-specific adapters |
| --- | --- |
| Arcus | Ashford, Bromley, Shepway |
| Atrium | BCP, Wychavon, Worcestershire, Worcester, West Sussex, West Northamptonshire, Welwyn Hatfield, Surrey, Crawley, Devon, Somerset, Essex |
| Weekly CSV Atrium | Vale of White Horse, South Oxfordshire, Exmoor |
| Tascomi | Barking and Dagenham, Waltham Forest, Coventry, Gloucestershire |
| EnterpriseStore | Broxbourne |
| Fastweb | Wokingham |
| CCED | Dorset |
| Socrata | Camden |

## Verification and limits

Regression tests are in `tests/test_api_pagination.py`,
`tests/test_html_pagination.py`, `tests/test_legacy_pagination.py`, and
`tests/test_special_pagination.py`. Their synthetic HTTP responses exercise real
adapter parsing and request construction, including later pages, overlapping
records, rotating viewstate, server page-size caps, explicit limits, and stalls.
They are not captured live responses from every council.

Run the full suite with the project dependencies and pytest installed:

```sh
PYTHONPATH=src python -m pytest -q
```

The main lead-search routine supplies no per-council application limit. Final
saved counts can still differ because of council failures, received versus
validated date semantics, filtering, duplicate suppression and prior history.
An adapter's incomplete-search error enters the existing fallback/failure path;
if no fallback succeeds, inspect `search_failures.csv`.

The shared HTML traversal follows controls actually present in the response;
unrecognised JavaScript-only or custom button paging still needs live portal
evidence. Ocella's multi-year keyword fallback combines responses, so stateful
pagination across multiple year searches also needs a live check. No guarantee
is made that every council exposes every record through these interfaces.

To measure the effect, rerun the same 210 councils and exact dates with blank
keywords, use equivalent history settings, compare per-council counts and inspect
failures. If running the Windows executable, rebuild it from the changed source
first: `dist/PlanningLeadGenerator.exe` is not rebuilt by this source change.
