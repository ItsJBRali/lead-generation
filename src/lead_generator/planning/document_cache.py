"""Reuse public document bytes only after the server validates them with HTTP 304."""
from __future__ import annotations
import json
import threading
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from urllib.parse import parse_qsl, urlsplit

_CACHE = threading.local()


@contextmanager
def document_cache_directory(root):
    previous = getattr(_CACHE, 'root', None)
    _CACHE.root = root
    try:
        yield
    finally:
        _CACHE.root = previous


def _index(url):
    root = getattr(_CACHE, 'root', None)
    if root is None or any(key.casefold() in {'sid', 'token', 'session', 'auth-session'} for key, _ in parse_qsl(urlsplit(url).query)):
        return None
    return Path(root) / 'documents' / (sha256(url.encode()).hexdigest() + '.json')


def revalidation(request, *, cacheable=True):
    index = _index(request.full_url) if cacheable else None
    if index is None:
        return None
    try:
        entry = json.loads(index.read_text(encoding='utf-8'))
        if not (entry.get('etag') or entry.get('modified')):
            return None
        digest = entry['digest']
        if len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
            return None
        payload = (index.parent / (digest + '.bin')).read_bytes()
        if sha256(payload).hexdigest() != digest:
            return None
        if entry.get('etag'):
            request.add_header('If-None-Match', entry['etag'])
        if entry.get('modified'):
            request.add_header('If-Modified-Since', entry['modified'])
        return {**entry, 'payload': payload}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def store_document(url, file, headers, *, cacheable=True):
    index = _index(url) if cacheable else None
    if index is None or not (headers.get('ETag') or headers.get('Last-Modified')):
        return
    digest = sha256(file.payload).hexdigest()
    temporary = index.with_suffix(f'.{uuid4().hex}.tmp')
    blob = index.parent / (digest + '.bin')
    blob_temporary = blob.with_suffix(f'.{uuid4().hex}.tmp')
    try:
        index.parent.mkdir(parents=True, exist_ok=True)
        blob_temporary.write_bytes(file.payload)
        blob_temporary.replace(blob)
        temporary.write_text(json.dumps({'digest': digest, 'etag': headers.get('ETag'),
                                        'modified': headers.get('Last-Modified'), 'final_url': file.final_url,
                                        'content_type': file.content_type, 'filename': file.filename}), encoding='utf-8')
        temporary.replace(index)
    except OSError:
        pass
    finally:
        temporary.unlink(missing_ok=True)
        blob_temporary.unlink(missing_ok=True)
