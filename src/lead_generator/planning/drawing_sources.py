from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class DrawingSourceDecision:
    eligible: bool
    needs_text: bool
    reason: str


STATUS_TOKENS = {"proposed", "existing"}
DRAWING_TOKENS = {
    "plan", "plans", "drawing", "drawings", "elevation", "elevations",
    "section", "sections", "layout", "layouts",
}
NARRATIVE_PHRASES = (
    "application form", "planning statement", "design and access statement",
    "supporting statement", "report", "assessment", "survey", "letter",
    "notice", "certificate", "schedule", "specification", "consultation",
    "photograph", "photos", "method statement", "heritage statement",
)
DRAWING_EVIDENCE_RE = re.compile(
    r"(?i)\b(?:drawing\s*(?:no|number)|scale|revision|rev|drawn\s+by|checked\s+by)\b"
)
DRAWING_CODE_RE = re.compile(r"(?i)^(?=.*\d)[a-z0-9][a-z0-9._ -]{2,}$")
CLASSIFICATION_TEXT_LIMIT = 12_000


def _phrase_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _has_narrative_marker(value: str) -> bool:
    normalized = _phrase_text(value)
    return any(phrase in normalized for phrase in NARRATIVE_PHRASES)


def preclassify_drawing_source(filename: str) -> DrawingSourceDecision:
    name = Path(filename).stem
    if _has_narrative_marker(name):
        return DrawingSourceDecision(False, False, "narrative document title")
    tokens = _tokens(name)
    has_status = bool(tokens & STATUS_TOKENS)
    has_drawing_type = bool(tokens & DRAWING_TOKENS)
    if has_status and has_drawing_type:
        return DrawingSourceDecision(True, False, "drawing status and type in title")
    if has_status or has_drawing_type or DRAWING_CODE_RE.fullmatch(name.strip()):
        return DrawingSourceDecision(False, True, "PDF title-block confirmation required")
    return DrawingSourceDecision(False, False, "not a proposed or existing drawing")


def classify_drawing_source(filename: str, text: str) -> DrawingSourceDecision:
    title_text = "\n".join(text.splitlines()[:30])[:3_000]
    bounded_text = "\n".join(
        (text[:CLASSIFICATION_TEXT_LIMIT], text[-CLASSIFICATION_TEXT_LIMIT:])
    )
    combined = f"{Path(filename).stem}\n{bounded_text}"
    if _has_narrative_marker(Path(filename).stem) or _has_narrative_marker(title_text):
        return DrawingSourceDecision(False, False, "narrative document marker")
    filename_tokens = _tokens(Path(filename).stem)
    tokens = _tokens(combined)
    has_status = bool(tokens & STATUS_TOKENS)
    clear_filename = bool(filename_tokens & STATUS_TOKENS) and bool(
        filename_tokens & DRAWING_TOKENS
    )
    evidence = {
        match.group(0).casefold()
        for match in DRAWING_EVIDENCE_RE.finditer(bounded_text)
    }
    eligible = clear_filename or (has_status and len(evidence) >= 2)
    reason = "eligible drawing" if eligible else "drawing status/type evidence incomplete"
    return DrawingSourceDecision(eligible, False, reason)


def is_existing_only_drawing_metadata(value: str) -> bool:
    tokens = _tokens(value)
    return (
        "existing" in tokens
        and "proposed" not in tokens
        and bool(tokens & DRAWING_TOKENS)
        and not _has_narrative_marker(value)
    )
