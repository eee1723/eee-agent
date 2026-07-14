"""Common Houdini Creole document grammar parsing.

All functions here are pure and I/O-free: they operate on already-decoded
``str`` input and produce immutable drafts. Domain-specific parsing (SOP,
HOM, VEX, skill) builds on top of these primitives in later stages.
"""

from __future__ import annotations

import re

from eee_agent.knowledge.models import ReferenceDraft, SectionDraft

__all__ = [
    "clean_body",
    "decode_source",
    "parse_metadata",
    "parse_references",
    "parse_sections",
    "parse_summary",
    "parse_title",
]

# --- decoding -------------------------------------------------------------

def decode_source(data: bytes) -> str:
    """Decode raw document bytes using UTF-8 (BOM-stripping).

    Only ``utf-8-sig`` is used. A leading BOM is removed; invalid UTF-8
    raises ``UnicodeDecodeError`` unchanged.
    """
    return data.decode("utf-8-sig")


# --- metadata -------------------------------------------------------------

_METADATA_RE = re.compile(r"^#(?P<key>[A-Za-z_][\w.-]*):\s*(?P<value>.*)$")


def parse_metadata(text: str) -> dict[str, str]:
    """Parse ``#key: value`` directives with indented continuation lines.

    A directive's value may be continued by subsequent indented lines; each
    continuation line is stripped and joined with ``\\n``. Continuation stops
    at the next unindented line.
    """
    result: dict[str, str] = {}
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        match = _METADATA_RE.match(lines[index])
        if not match:
            index += 1
            continue
        key = match.group("key")
        value = match.group("value").rstrip()
        index += 1
        continuation: list[str] = []
        while index < len(lines) and (
            lines[index].startswith(" ") or lines[index].startswith("\t")
        ):
            continuation.append(lines[index].strip())
            index += 1
        if continuation:
            joined = "\n".join(continuation)
            value = f"{value}\n{joined}" if value else joined
        result[key] = value
    return result


# --- title ----------------------------------------------------------------

_TITLE_RE = re.compile(r"^=\s+(?P<title>.+?)\s+=\s*$", re.MULTILINE)


def parse_title(text: str) -> str:
    """Return the single-``=`` page title, or ``""`` if absent.

    Section headings (``==`` or more) are not matched.
    """
    match = _TITLE_RE.search(text)
    return match.group("title") if match else ""


# --- summary --------------------------------------------------------------

def parse_summary(text: str) -> str:
    """Return the first ``\"\"\"...\"\"\"`` summary block, whitespace-collapsed.

    Internal whitespace (including newlines) is collapsed to single spaces.
    Returns ``""`` when no summary block is present.
    """
    start = text.find('"""')
    if start == -1:
        return ""
    end = text.find('"""', start + 3)
    if end == -1:
        return ""
    return " ".join(text[start + 3:end].split())


# --- sections -------------------------------------------------------------

_SECTION_RE = re.compile(
    r"^(?P<marks>={2,})\s+(?P<heading>.+?)\s+(?P=marks)"
    r"(?:\s*\((?P<anchor>[^)]+)\))?\s*$"
)


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def parse_sections(text: str) -> tuple[SectionDraft, ...]:
    """Parse ``== Heading == (anchor)`` sections in document order.

    A section key is the explicit parenthesized anchor when present, otherwise
    the slugified heading. Duplicate keys are disambiguated with stable
    ``-2``, ``-3`` suffixes. A section body spans from the heading line to the
    next heading (or end of document).
    """
    lines = text.split("\n")
    headings: list[tuple[int, str, str | None]] = []
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if match:
            headings.append((index, match.group("heading"), match.group("anchor")))

    sections: list[SectionDraft] = []
    used: set[str] = set()
    for ordinal, (index, heading, anchor) in enumerate(headings):
        key = anchor if anchor is not None else _slugify(heading)
        if key in used:
            suffix = 2
            while f"{key}-{suffix}" in used:
                suffix += 1
            key = f"{key}-{suffix}"
        used.add(key)
        start = index + 1
        end = headings[ordinal + 1][0] if ordinal + 1 < len(headings) else len(lines)
        body = "\n".join(lines[start:end]).strip()
        sections.append(
            SectionDraft(key=key, heading=heading, body=body, ordinal=ordinal)
        )
    return tuple(sections)


# --- references -----------------------------------------------------------

_INCLUDE_RE = re.compile(r"^\s*:include\s+(?P<spec>.+):\s*$")
_BRACKET_RE = re.compile(r"\[(?P<content>[^\[\]]+)\]")
_LABELED_RE = re.compile(
    r"^(?P<display>[^|]+)\|(?P<kind>[A-Za-z][\w]*):(?P<rest>.+)$"
)
_DIRECT_RE = re.compile(r"^(?P<kind>[A-Za-z][\w]*):(?P<rest>.+)$")
_LOCAL_RE = re.compile(r"^(?P<display>[^|]+)\|#(?P<anchor>.+)$")


def _split_target(rest: str) -> tuple[str, str | None]:
    """Split a raw target into ``(target, anchor)`` on the first ``#``."""
    if "#" in rest:
        target, anchor = rest.split("#", 1)
        return target, anchor
    return rest, None


def _classify_reference(content: str, line_number: int) -> ReferenceDraft | None:
    labeled = _LABELED_RE.match(content)
    if labeled:
        target, anchor = _split_target(labeled.group("rest"))
        return ReferenceDraft(
            display_text=labeled.group("display").strip(),
            target_kind=labeled.group("kind"),
            raw_target=labeled.group("rest"),
            normalized_target=target,
            anchor=anchor,
            source_line=line_number,
        )
    direct = _DIRECT_RE.match(content)
    if direct:
        target, anchor = _split_target(direct.group("rest"))
        return ReferenceDraft(
            display_text=None,
            target_kind=direct.group("kind"),
            raw_target=direct.group("rest"),
            normalized_target=target,
            anchor=anchor,
            source_line=line_number,
        )
    local = _LOCAL_RE.match(content)
    if local:
        # A local anchor ``[display|#anchor]`` references an anchor on the
        # current page; it has no cross-entity target.
        return ReferenceDraft(
            display_text=local.group("display").strip(),
            target_kind="Anchor",
            raw_target="",
            normalized_target="",
            anchor=local.group("anchor").strip(),
            source_line=line_number,
        )
    return None


def parse_references(text: str) -> tuple[ReferenceDraft, ...]:
    """Parse typed, labeled, local-anchor and include references.

    Labeled references (``[Label|Kind:target]``) are classified before direct
    references so the inner ``Kind:target`` is never emitted a second time.
    Include directives (``:include target#anchor:``) are parsed per line,
    including those indented with leading spaces or tabs.
    One-based source line numbers are recorded.
    """
    refs: list[ReferenceDraft] = []
    for line_number, line in enumerate(text.split("\n"), start=1):
        include = _INCLUDE_RE.match(line)
        if include:
            spec = include.group("spec")
            target, anchor = _split_target(spec)
            refs.append(
                ReferenceDraft(
                    display_text=None,
                    target_kind="Include",
                    raw_target=spec,
                    normalized_target=target,
                    anchor=anchor,
                    source_line=line_number,
                )
            )
        for match in _BRACKET_RE.finditer(line):
            ref = _classify_reference(match.group("content"), line_number)
            if ref is not None:
                refs.append(ref)
    return tuple(refs)


# --- body cleaning --------------------------------------------------------

_DIRECTIVE_LINE_RE = re.compile(
    r"^\s*:(?:fig|image|video|vimeo|media|movie):", re.IGNORECASE
)


def clean_body(text: str) -> str:
    """Return body text with figure/image/video directives and blank runs removed.

    Whole-line ``:fig:``/``:image:``/``:video:``/``:vimeo:``/``:media:``/``:movie:``
    directives are dropped, repeated blank lines collapse to one, and
    leading/trailing blank lines are stripped. Headings, signatures,
    arguments and prose are preserved.
    """
    kept = [line.rstrip() for line in text.split("\n") if not _DIRECTIVE_LINE_RE.match(line)]
    collapsed: list[str] = []
    blank = False
    for line in kept:
        if line == "":
            if blank:
                continue
            blank = True
        else:
            blank = False
        collapsed.append(line)
    while collapsed and collapsed[0] == "":
        collapsed.pop(0)
    while collapsed and collapsed[-1] == "":
        collapsed.pop()
    return "\n".join(collapsed)
