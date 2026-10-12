"""Thread-safe measurements shared by scraper, document and OCR workers."""
from __future__ import annotations

import csv
import threading
from contextlib import contextmanager
from pathlib import Path
from time import perf_counter

_CURRENT = threading.local()


def metric_event(name: str, amount: int = 1):
    active = getattr(_CURRENT, 'stage', None)
    if active:
        metrics, council, stage = active
        metrics.add(council, stage, name, amount)


class RunMetrics:
    def __init__(self, previous=(), persist=None):
        self.lock = threading.Lock()
        self.rows = {(row['council'], row['stage']): row for _, row in previous}
        self.persist = persist

    def add(self, council, stage, field, value):
        with self.lock:
            row = self.rows.setdefault((council, stage), {'council': council, 'stage': stage})
            row[field] = row.get(field, 0) + value
            if self.persist:
                self.persist(council, stage, dict(row))

    @contextmanager
    def bind(self, council, stage):
        previous = getattr(_CURRENT, 'stage', None)
        _CURRENT.stage = self, council, stage
        try:
            yield
        finally:
            _CURRENT.stage = previous

    @contextmanager
    def stage(self, council, stage):
        started = perf_counter()
        try:
            with self.bind(council, stage):
                yield
        finally:
            self.add(council, stage, 'seconds', perf_counter() - started)
            self.add(council, stage, 'attempts', 1)

    def export(self, path: Path):
        fields = ['council', 'stage', 'seconds', 'attempts', 'requests', 'browser_operations', 'retries', 'downloaded_documents', 'ocr_pages', 'pdf_cache_hits', 'document_cache_hits']
        with self.lock:
            rows = [dict(row) for _, row in sorted(self.rows.items())]
        with path.open('w', newline='', encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows({field: row.get(field, 0) for field in fields} for row in rows)
