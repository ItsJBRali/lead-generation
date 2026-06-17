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

Attachment support currently returns document metadata and URLs. It does not
download or store the files themselves yet.

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

- choose a GeoJSON file containing council portal settings
- choose an output folder
- select a received-date range
- edit the default lead keywords
- run the search with council progress and a live log

Each GeoJSON feature should include a council name in `properties`, using keys
such as `name`, `council`, `area_name`, or `LAD23NM`. If portal fields such as
`portal_family`, `base_url`, and `listing_url` are also present, the app uses
those directly. If only a council name is present, the app falls back to public
planning metadata to discover received applications for that council and logs
that fallback in the run log.

To run from source:

```powershell
$env:PYTHONPATH = "$PWD\src"
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m lead_generator.planning.gui
```

To rebuild the executable:

```powershell
& "C:\Users\JBRal\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m PyInstaller --noconfirm --clean --name PlanningLeadGenerator --onefile --windowed --paths src src\lead_generator\planning\gui.py
```
