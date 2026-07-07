# Lead Generator - Independence

This repository contains the first planning-data ingestion layer for the lead
generation platform.

The initial implementation targets UK council websites running Idox PublicAccess,
one of the common planning portal products used by local authorities. It follows
the same high-level workflow as `aspeakman/UKPlanning`:

1. discover planning application identifiers from a council listing page
2. fetch each application detail page
3. normalize the fields into lead-friendly records

The code here is a fresh Python 3 implementation with typed records, a small
HTTP boundary, offline parser tests, and no dependency on the legacy GPL source.

## Current Portal Coverage

| Portal family | Status | Notes |
| --- | --- | --- |
| Idox PublicAccess | Live-tested | Discovers applications, parses summary fields, and extracts document attachment metadata/URLs. |
| Ocella-style registers | Live-tested | Parses common listing/detail/document patterns and live-tested against Arun Ocella detail pages. |
| Civica / Authority Public Access | Live-tested | Parses common labelled listing/detail/document patterns and JavaScript-backed `details.html?refval=...` pages that expose a public planning-data API. Live-tested against Bath & North East Somerset. |
| Agile Applications / APAS | Implemented | Parses common APAS listing/detail/document patterns; fixture-covered. Current live council examples were not confirmed in the latest smoke test. |
| Northgate Planning Explorer | Live-tested | Parses common listing/detail/document patterns and live-tested against Wandsworth Northgate detail pages. ASP.NET search-form submission is not automated yet. |
| NEC/Assure OnlinePlanningSearch | Live-tested | Submits date-bounded `OnlinePlanningSearch` forms, parses `OnlinePlanningOverview` results, loads AJAX document tabs, and downloads browser-gated `DisplaySearchDocument` PDFs. Live-tested against Broxbourne. |
| FastWeb / PlanPortal | Live-tested | Submits received-date searches against `search.asp`, parses `detail.asp?AltRef=...` application pages, follows `View Plans & Documents` bridges, reads PlanPortal Ext Direct document rows, and downloads `view.aspx` PDFs. Live-tested against Rotherham. |
| Form-backed council registers | Implemented | Submits council date-search forms for BCP-style `/Search/Advanced`, Tascomi, and Uniform-style `index.html?fa=search` portals, then parses returned application links/details directly from the council website. |
| Arcus / Salesforce public registers | Implemented | Submits the Arcus advanced planning search through the council's Salesforce public-register endpoint, parses returned applications, and hands detail pages to the Arcus document downloader. |

The lead-search GUI downloads discovered document files into each saved
application folder. The downloader verifies that responses are real files,
follows viewer/redirect pages, accepts common document-disclaimer sessions, and
prefers links whose title matches the intended document when a portal returns
multiple PDFs from an intermediate page.
Users can untick the download option to produce only the application CSV. The
CSV includes each matched application's reference, address, application link,
proposal, received date, and council.

## Run Tests

```powershell
$env:PYTHONPATH = "$PWD\src"
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m unittest discover -s tests
```

## Example CLI

```powershell
$env:PYTHONPATH = "$PWD\src"
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m lead_generator.planning.cli idox `
  --authority "Example Council" `
  --base-url "https://planning.example.gov.uk" `
  --listing-url "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList" `
  --fetch-documents
```

The CLI also supports `ocella`, `civica`, `agile`, and `northgate` subcommands for
non-Idox portals that expose application data in labelled HTML pages.

For production use, pass a real council PublicAccess base URL and keep the
default request delay in place unless the council's terms explicitly allow a
higher rate.

## Lead Generator GUI

The Windows GUI is available as `dist/PlanningLeadGenerator.exe`. It lets a user:

- choose a GeoJSON file containing the search boundary
- choose an output folder
- select a received-date range
- choose whether to download application files as well as the CSV
- choose how many councils to search concurrently, from 1 to 8
- edit the default lead keywords
- run the search with council progress, captured-lead count, and a live log

The uploaded GeoJSON no longer needs council names or portal fields. The app
bundles `planning_authorities.geojson`, a stored catalogue of planning
authorities with council names, boundaries, portal families, and planning portal
URLs. At the start of a run it intersects the uploaded boundary with that
catalogue, saves the matched authorities to `selected_councils.geojson`, and
then searches only those authorities.

Application results are filtered by the selected received/validated date range
and proposal keywords. When a council portal provides application coordinates,
the app also checks that the point falls inside the uploaded GeoJSON boundary;
when the portal does not publish coordinates, the council-overlap selection is
used as the location filter.

Each GUI run appends a summary row to `search_history.csv` beside the executable
so past searches remain available across output folders.

To run from source:

```powershell
$env:PYTHONPATH = "$PWD\src"
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m lead_generator.planning.gui
```

To rebuild the executable:

```powershell
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m PyInstaller --noconfirm --clean PlanningLeadGenerator.spec
```
