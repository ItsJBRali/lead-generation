"""Content-addressed PDF text cache, independent of application-specific exclusions."""
from __future__ import annotations
import json
import threading
from contextlib import contextmanager
from hashlib import sha256
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from uuid import uuid4

_CACHE = threading.local()


@contextmanager
def pdf_cache_directory(root: Path | None):
    previous = getattr(_CACHE, 'root', None)
    _CACHE.root = root
    try:
        yield
    finally:
        _CACHE.root = previous


def cache_path(path: Path, settings: tuple) -> Path | None:
    root = getattr(_CACHE, 'root', None)
    if root is None:
        return None
    versions = []
    for package in ('pypdf', 'pypdfium2', 'rapidocr', 'onnxruntime'):
        try:
            versions.append(version(package))
        except PackageNotFoundError:
            versions.append('unavailable')
    fingerprint = sha256(path.read_bytes() + json.dumps([1, settings, versions]).encode()).hexdigest()
    return Path(root) / 'pdf-text' / f'{fingerprint}.json'


def load_cached_text(path: Path | None):
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data['page_count'], int) or data['page_count'] < 1:
            return None
        data['page_text'] = {int(k): v for k, v in data['page_text'].items() if isinstance(v, str)}
        data['ocr_attempted'] = set(map(int, data['ocr_attempted']))
        return data
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_cached_text(path: Path | None, page_count, page_text, ocr_attempted):
    if path is None:
        return
    # Failed/empty reads are retried on the next run rather than cached as success.
    text = {k: v for k, v in page_text.items() if v.strip()}
    if not text:
        return
    temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_text(json.dumps({'page_count': page_count, 'page_text': text,
                                        'ocr_attempted': sorted(set(text) & ocr_attempted)}), encoding='utf-8')
        temporary.replace(path)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
