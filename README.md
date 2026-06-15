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
  --listing-url "https://planning.example.gov.uk/online-applications/search.do?action=weeklyList"
```

For production use, pass a real council PublicAccess base URL and keep the
default request delay in place unless the council's terms explicitly allow a
higher rate.
