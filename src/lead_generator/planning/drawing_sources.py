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
    "supporting statement", "report", "reports", "statement", "statements",
    "assessment", "assessments", "survey", "surveys", "strategy",
    "strategies", "travel plan", "environmental statement",
    "landscape visual impact", "appendix", "appendices", "review", "reviews",
    "presentation", "presentations",
    "external materials", "material schedule", "materials schedule",
    "drawing register", "cemp", "wms", "method statement", "letter",
    "notice", "notices", "certificate", "certificates", "schedule",
    "schedules", "specification", "specifications", "consultation",
    "consultations",
    "photograph", "photographs", "photos", "heritage statement",
    "management plan", "design access statement", "das",
)
DRAWING_EVIDENCE_PATTERNS = (
    re.compile(r"(?i)\bdrawing\s*(?:no|number)\b"),
    re.compile(r"(?i)\bscale\b"),
    re.compile(r"(?i)\brev(?:ision)?\b"),
    re.compile(r"(?i)\bdrawn\s+by\b"),
    re.compile(r"(?i)\bchecked\s+by\b"),
)
DRAWING_CODE_RE = re.compile(r"(?i)^(?=.*\d)[a-z0-9][a-z0-9._ -]{2,}$")
TITLE_PAGE_LINE_LIMIT = 30
TITLE_PAGE_TEXT_LIMIT = 3_000
TITLE_BLOCK_WINDOW_LINES = 10


def _phrase_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _has_narrative_marker(value: str) -> bool:
    normalized = f" {_phrase_text(value)} "
    return any(f" {_phrase_text(phrase)} " in normalized for phrase in NARRATIVE_PHRASES)


def _title_page_lines(text: str) -> list[str]:
    bounded = "\n".join(text.splitlines()[:TITLE_PAGE_LINE_LIMIT])
    return [
        line.strip()
        for line in bounded[:TITLE_PAGE_TEXT_LIMIT].splitlines()
        if line.strip()
    ]


def _has_ambiguous_drawing_evidence(filename: str, title_lines: list[str]) -> bool:
    lines = [Path(filename).stem, *title_lines]
    for start in range(len(lines)):
        window = "\n".join(lines[start:start + TITLE_BLOCK_WINDOW_LINES])
        tokens = _tokens(window)
        evidence_count = sum(
            bool(pattern.search(window)) for pattern in DRAWING_EVIDENCE_PATTERNS
        )
        has_drawing_type = bool(tokens & DRAWING_TOKENS) or bool(
            DRAWING_EVIDENCE_PATTERNS[0].search(window)
        )
        if (
            bool(tokens & STATUS_TOKENS)
            and has_drawing_type
            and evidence_count >= 2
        ):
            return True
    return False


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
    title_lines = _title_page_lines(text)
    title_text = "\n".join(title_lines)
    if _has_narrative_marker(Path(filename).stem) or _has_narrative_marker(title_text):
        return DrawingSourceDecision(False, False, "narrative document marker")
    filename_tokens = _tokens(Path(filename).stem)
    clear_filename = bool(filename_tokens & STATUS_TOKENS) and bool(
        filename_tokens & DRAWING_TOKENS
    )
    eligible = clear_filename or _has_ambiguous_drawing_evidence(
        filename, title_lines
    )
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
