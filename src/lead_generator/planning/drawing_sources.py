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
    "external material", "external materials", "material schedule",
    "materials schedule",
    "drawing register", "cemp", "wms", "method statement", "letter",
    "notice", "notices", "certificate", "certificates", "schedule",
    "schedules", "specification", "specifications", "consultation",
    "consultations",
    "photograph", "photographs", "photos", "heritage statement",
    "management plan", "design access statement", "planning application appendix",
    "environmental statement volume", "es vol", "es volume",
    "non technical summary", "desk study",
    "chapter", "figure", "figures", "summary", "study", "studies",
    "justification", "cover", "contents", "agenda", "minutes", "brochure",
    "invoice", "receipt", "das",
)
TITLE_NARRATIVE_PHRASES = (
    "application form", "planning statement", "design and access statement",
    "supporting statement", "report", "assessment", "strategy", "travel plan",
    "environmental statement", "landscape visual impact", "appendix",
    "presentation", "material schedule", "drawing register", "cemp", "wms",
    "method statement", "consultation", "heritage statement",
    "management plan", "planning application appendix", "non technical summary",
    "desk study", "chapter", "das",
)
DRAWING_EVIDENCE_PATTERNS = (
    re.compile(r"(?i)\bdrawing\s*(?:no|number)\b"),
    re.compile(r"(?i)\bscale\b"),
    re.compile(r"(?i)\brev(?:ision)?\b"),
    re.compile(r"(?i)\bdrawn\s+by\b"),
    re.compile(r"(?i)\bchecked\s+by\b"),
)
DRAWING_CODE_RE = re.compile(
    r"(?ix)"
    r"^(?=.{3,48}$)"
    r"(?=.*[a-z])"
    r"(?=.*\d)"
    r"(?=.*[-_.][a-z0-9]*\d[a-z0-9]*$)"
    r"(?!.*(?:^|[-_.])(?:document|report|statement)(?:[-_.]|$))"
    r"(?:"
    r"(?:[a-z]{1,3}|[0-9]{1,8}|"
    r"(?=[a-z0-9]{2,8}[-_.])(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)"
    r"[a-z0-9]{2,8})(?:[-_.][a-z0-9]{1,8}){1,3}"
    r"|"
    r"(?:[a-z]{1,3}|[0-9]{1,8}|"
    r"(?=[a-z0-9]{2,8}[-_.])(?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)"
    r"[a-z0-9]{2,8})(?:[-_.][a-z0-9]{1,8}){3,9}"
    r"[-_.](?=[a-z0-9]*[a-z])(?=[a-z0-9]*\d)[a-z0-9]{2,8}"
    r")$"
)
TITLE_PAGE_START_LINE_LIMIT = 30
TITLE_PAGE_END_LINE_LIMIT = 80
TITLE_HEADING_LINE_LIMIT = 12
TITLE_BLOCK_WINDOW_LINES = 10


def _phrase_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def _tokens(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def _has_narrative_marker(
    value: str,
    phrases: tuple[str, ...] = NARRATIVE_PHRASES,
) -> bool:
    phrase_text = _phrase_text(value)
    normalized = f" {phrase_text} "
    compact = phrase_text.replace(" ", "")
    for phrase in phrases:
        normalized_phrase = _phrase_text(phrase)
        if f" {normalized_phrase} " in normalized:
            return True
        if (
            " " in normalized_phrase
            and normalized_phrase.replace(" ", "") in compact
        ):
            return True
    return False


def _title_page_lines(text: str) -> list[str]:
    lines = [
        line.strip()
        for line in text.splitlines()
        if line.strip()
    ]
    if len(lines) <= TITLE_PAGE_START_LINE_LIMIT + TITLE_PAGE_END_LINE_LIMIT:
        return lines
    return [
        *lines[:TITLE_PAGE_START_LINE_LIMIT],
        *lines[-TITLE_PAGE_END_LINE_LIMIT:],
    ]


def _has_title_narrative_marker(text: str) -> bool:
    for line in text.splitlines():
        normalized_line = _phrase_text(line)
        line_words = normalized_line.split()
        compact_line = normalized_line.replace(" ", "")
        for phrase in TITLE_NARRATIVE_PHRASES:
            normalized_phrase = _phrase_text(phrase)
            phrase_words = normalized_phrase.split()
            is_short_title = len(line_words) <= len(phrase_words) + 4
            if normalized_line == normalized_phrase:
                return True
            if (
                is_short_title
                and f" {normalized_phrase} " in f" {normalized_line} "
            ):
                return True
            if (
                is_short_title
                and len(phrase_words) > 1
                and normalized_phrase.replace(" ", "") in compact_line
            ):
                return True
    return False


def _has_ambiguous_drawing_evidence(filename: str, title_lines: list[str]) -> bool:
    filename_tokens = _tokens(Path(filename).stem)
    filename_has_status = bool(filename_tokens & STATUS_TOKENS)
    filename_has_drawing_type = bool(filename_tokens & DRAWING_TOKENS)
    filename_has_drawing_code = bool(
        DRAWING_CODE_RE.fullmatch(Path(filename).stem.strip())
    )
    if not (
        filename_has_status
        or filename_has_drawing_type
        or filename_has_drawing_code
    ):
        return False
    for start in range(len(title_lines)):
        window = "\n".join(title_lines[start:start + TITLE_BLOCK_WINDOW_LINES])
        tokens = _tokens(window)
        evidence_count = sum(
            bool(pattern.search(window)) for pattern in DRAWING_EVIDENCE_PATTERNS
        )
        has_status = filename_has_status or bool(tokens & STATUS_TOKENS)
        has_drawing_type = (
            filename_has_drawing_type
            or bool(tokens & DRAWING_TOKENS)
            or bool(DRAWING_EVIDENCE_PATTERNS[0].search(window))
        )
        if (
            has_status
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
    heading_text = "\n".join(
        line.strip()
        for line in text.splitlines()[:TITLE_HEADING_LINE_LIMIT]
        if line.strip()
    )
    if _has_narrative_marker(Path(filename).stem) or _has_title_narrative_marker(
        heading_text
    ):
        return DrawingSourceDecision(False, False, "narrative document marker")
    filename_tokens = _tokens(Path(filename).stem)
    clear_filename = bool(filename_tokens & STATUS_TOKENS) and bool(
        filename_tokens & DRAWING_TOKENS
    )
    eligible = clear_filename or _has_ambiguous_drawing_evidence(
        filename, title_lines
    )
    reason = "eligible drawing" if eligible else "drawing status/type evidence incomplete"
    return DrawingSourceDecision(eligible, not eligible, reason)


def is_existing_only_drawing_metadata(value: str) -> bool:
    tokens = _tokens(value)
    return (
        "existing" in tokens
        and "proposed" not in tokens
        and bool(tokens & DRAWING_TOKENS)
        and not _has_narrative_marker(value)
    )
