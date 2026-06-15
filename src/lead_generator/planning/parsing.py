from __future__ import annotations

import re
from datetime import datetime
from html import unescape

WHITESPACE_RE = re.compile(r"\s+")
POSTCODE_RE = re.compile(
    r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\b",
    re.IGNORECASE,
)


def clean_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = WHITESPACE_RE.sub(" ", unescape(value)).strip()
    return cleaned or None


def normalize_label(value: str) -> str:
    value = clean_text(value) or ""
    value = value.lower().replace("&", "and")
    value = re.sub(r"[^a-z0-9]+", "_", value).strip("_")
    return value


def parse_council_date(value: str | None) -> str | None:
    cleaned = clean_text(value)
    if not cleaned:
        return None

    for fmt in (
        "%d/%m/%Y",
        "%d/%m/%y",
        "%d-%m-%Y",
        "%d-%m-%y",
        "%Y-%m-%d",
        "%d %B %Y",
        "%d %b %Y",
        "%a %d %b %Y",
        "%A %d %B %Y",
    ):
        try:
            return datetime.strptime(cleaned, fmt).date().isoformat()
        except ValueError:
            pass
    return cleaned


def extract_postcode(*values: str | None) -> str | None:
    for value in values:
        if not value:
            continue
        match = POSTCODE_RE.search(value)
        if match:
            compact = WHITESPACE_RE.sub("", match.group(1)).upper()
            return f"{compact[:-3]} {compact[-3:]}"
    return None
