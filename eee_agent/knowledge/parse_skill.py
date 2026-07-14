"""Repository project-skill parser.

Pure and I/O-free: the only inputs are ``source_path`` and ``text``. The skill
file is never read from disk; the SHA-256 is computed from the supplied text.
Project skills use YAML-style frontmatter and Markdown headings, so a minimal
frontmatter stripper and Markdown heading parser live here (the Stage 1 common
parser is Creole-oriented for Houdini docs). ``parse_references`` and
``clean_body`` are reused for typed links and body cleaning.
"""

from __future__ import annotations

import hashlib
import re

from eee_agent.knowledge.ids import make_entity_id, normalize_source_path
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    ParsedDocument,
    SectionDraft,
)
from eee_agent.knowledge.parse_common import clean_body, parse_references

__all__ = ["parse_skill_document"]

_PRIORITY_QUALIFIED = 100
_PRIORITY_TITLE = 60

_MD_TITLE_RE = re.compile(r"^#\s+(?P<title>.+?)\s*$")
_MD_SECTION_RE = re.compile(r"^(?P<marks>#{2,})\s+(?P<heading>.+?)\s*$")


def _skill_slug(logical_path: str) -> str:
    parts = logical_path.split("/")
    if "skills" in parts:
        index = parts.index("skills")
        if index + 1 < len(parts):
            return parts[index + 1]
    if len(parts) >= 2:
        return parts[-2]
    stem = parts[-1]
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return stem


def _parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}, text
    end = None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            end = index
            break
    if end is None:
        return {}, text
    frontmatter: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" in line:
            key, value = line.split(":", 1)
            frontmatter[key.strip()] = value.strip()
    body = "\n".join(lines[end + 1:])
    return frontmatter, body


def _markdown_title(body: str) -> str:
    for line in body.split("\n"):
        match = _MD_TITLE_RE.match(line)
        if match:
            return match.group("title")
    return ""


def _slugify(heading: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", heading.lower()).strip("-")
    return slug or "section"


def _parse_markdown_sections(body: str) -> tuple[SectionDraft, ...]:
    lines = body.split("\n")
    headings: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        match = _MD_SECTION_RE.match(line)
        if match:
            headings.append((index, match.group("heading")))

    sections: list[SectionDraft] = []
    used: set[str] = set()
    for ordinal, (index, heading) in enumerate(headings):
        key = _slugify(heading)
        if key in used:
            suffix = 2
            while f"{key}-{suffix}" in used:
                suffix += 1
            key = f"{key}-{suffix}"
        used.add(key)
        start = index + 1
        end = headings[ordinal + 1][0] if ordinal + 1 < len(headings) else len(lines)
        body_text = "\n".join(lines[start:end]).strip()
        sections.append(
            SectionDraft(key=key, heading=heading, body=body_text, ordinal=ordinal)
        )
    return tuple(sections)


def _build_edges(entity_id: str, logical_path: str, body: str) -> list[EdgeDraft]:
    edges: list[EdgeDraft] = []
    for ref in parse_references(body):
        if ref.target_kind == "Include":
            predicate = "includes"
            target_raw = ref.raw_target
        elif ref.target_kind == "Anchor":
            predicate = "references"
            target_raw = f"#{ref.anchor}" if ref.anchor else ""
        else:
            predicate = "references"
            target_raw = ref.raw_target
        edges.append(
            EdgeDraft(
                source_id=entity_id,
                predicate=predicate,
                target_id=None,
                target_raw=target_raw,
                target_anchor=ref.anchor,
                resolved=False,
                source_location=f"{logical_path}:{ref.source_line}",
            )
        )
    return edges


def parse_skill_document(source_path: str, text: str) -> ParsedDocument:
    """Parse a repository SKILL.md into a single skill_reference entity."""
    logical_path = normalize_source_path(source_path)
    frontmatter, body = _parse_frontmatter(text)
    slug = _skill_slug(logical_path)
    title = _markdown_title(body)
    summary = frontmatter.get("description", "")
    entity_id = make_entity_id(EntityKind.SKILL_REFERENCE, slug)
    sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    attributes: dict[str, object] = {"sha256": sha256, "slug": slug}
    for key, value in frontmatter.items():
        attributes.setdefault(key, value)

    entity = EntityDraft(
        entity_id=entity_id,
        kind=EntityKind.SKILL_REFERENCE,
        subtype="",
        canonical_name=slug,
        title=title,
        summary=summary,
        authority=Authority.PROJECT_VERIFIED_SKILL,
        source_path=logical_path,
        source_anchor=None,
        is_current=True,
        attributes=attributes,
        body=clean_body(body),
        sections=_parse_markdown_sections(body),
    )

    aliases = [
        AliasDraft(
            alias=slug,
            entity_id=entity_id,
            alias_type="qualified_name",
            priority=_PRIORITY_QUALIFIED,
        )
    ]
    if title:
        aliases.append(
            AliasDraft(
                alias=title,
                entity_id=entity_id,
                alias_type="title",
                priority=_PRIORITY_TITLE,
            )
        )

    edges = _build_edges(entity_id, logical_path, body)

    return ParsedDocument(
        entities=(entity,),
        aliases=tuple(aliases),
        edges=tuple(edges),
    )
