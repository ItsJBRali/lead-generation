"""Durable checkpoints for a single search; CSV remains the user-facing export."""
from __future__ import annotations

import json
import sqlite3
import threading
import os
from contextlib import closing
from pathlib import Path

from lead_generator.planning.models import PlanningApplication, PlanningDocument


def read_run_settings(folder: Path) -> dict:
    path = folder / 'run.sqlite3'
    if not path.is_file():
        raise ValueError('Choose a saved search folder containing run.sqlite3.')
    try:
        with closing(sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True)) as connection:
            settings = dict((key, json.loads(value)) for key, value in connection.execute("SELECT kind, value FROM records WHERE key='run' AND kind IN ('settings','ui')"))
        return {**settings['settings'], **settings['ui']}
    except (sqlite3.Error, KeyError, ValueError) as exc:
        raise ValueError('The selected folder does not contain valid saved search settings.') from exc


def application_from_dict(value: dict) -> PlanningApplication:
    value = dict(value)
    value['documents'] = [PlanningDocument(**document) for document in value.get('documents', [])]
    return PlanningApplication(**value)


class RunStorage:
    def __init__(self, path: Path, settings: dict, *, resume: bool = False):
        if resume and not path.is_file():
            raise ValueError('Choose a search folder containing run.sqlite3 to resume.')
        self.lock = threading.RLock()
        self.closed = False
        self.ownership = path.with_suffix('.lock').open('a+b')
        try:
            if os.fstat(self.ownership.fileno()).st_size == 0:
                self.ownership.write(b'\0')
                self.ownership.flush()
            self.ownership.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.ownership.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.ownership.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.ownership.close()
            raise ValueError('This saved search is already running in another window. Wait for it to finish before resuming.') from exc
        try:
            self.connection = sqlite3.connect(path, check_same_thread=False)
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('CREATE TABLE IF NOT EXISTS records (kind TEXT, key TEXT, value TEXT, PRIMARY KEY (kind, key))')
            self.connection.commit()
        except Exception:
            self.ownership.close()
            raise
        previous = self.get('settings', 'run')
        if resume and previous != settings:
            self.close()
            raise ValueError('This search has different dates, keywords, boundary, councils or download settings. Restore its original settings to resume.')
        self.set('settings', 'run', settings)

    def get(self, kind: str, key: str):
        with self.lock:
            row = self.connection.execute('SELECT value FROM records WHERE kind=? AND key=?', (kind, key)).fetchone()
        return json.loads(row[0]) if row else None

    def items(self, kind: str):
        with self.lock:
            rows = self.connection.execute('SELECT key, value FROM records WHERE kind=? ORDER BY rowid', (kind,)).fetchall()
        return [(key, json.loads(value)) for key, value in rows]

    def set(self, kind: str, key: str, value) -> None:
        with self.lock, self.connection:
            self.connection.execute('INSERT INTO records VALUES (?, ?, ?) ON CONFLICT(kind,key) DO UPDATE SET value=excluded.value', (kind, key, json.dumps(value)))

    def close(self):
        with self.lock:
            if not self.closed:
                self.connection.close()
                self.ownership.close()
                self.closed = True
