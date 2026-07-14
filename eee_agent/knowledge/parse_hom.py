"""HOM page and first-class method parser.

Pure and I/O-free: the only inputs are ``source_path`` and ``text``. Six page
types route to the stable entity kinds. Class ``::`` method blocks become
first-class ``HOM_METHOD`` entities; repeated same-name blocks aggregate
signatures in source order without overloading ordinals. Case is preserved
exactly (``hou.Node`` and ``hou.node`` are distinct). Cross-references stay
unresolved; only the structural ``declares_method`` edge targets a
parser-created method entity.
"""

from __future__ import annotations

import re

from eee_agent.knowledge.ids import make_entity_id, normalize_source_path
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    ParsedDocument,
)
from eee_agent.knowledge.parse_common import (
    clean_body,
    parse_metadata,
    parse_references,
    parse_sections,
    parse_summary,
    parse_title,
)

__all__ = ["parse_hom_document"]

_KIND_BY_TYPE = {
    "homclass": EntityKind.HOM_CLASS,
    "homfunction": EntityKind.HOM_FUNCTION,
    "hommodule": EntityKind.HOM_MODULE,
    "pypackage": EntityKind.HOM_PACKAGE,
    "hompackage": EntityKind.HOM_PACKAGE,
}

_NAMED_METADATA = {
    "type", "namespace", "class", "function", "module", "package",
    "cppname", "superclass",
}

_PRIORITY_QUALIFIED = 100
_PRIORITY_SHORT = 80
_PRIORITY_CASEFOLD = 10

_METHOD_HEADER_RE = re.compile(r"^::\s*(?P<name>[A-Za-z_]\w*)\s*$")
_HEADING_RE = re.compile(r"^={2,}\s")
_SIGNATURE_RE = re.compile(r"^:signature:\s*(?P<value>.*)$")
_RETURNS_RE = re.compile(r"^:returns:\s*(?P<value>.*)$")
_CPPNAME_RE = re.compile(r"^:cppname:\s*(?P<value>.*)$")

_EMPTY = ParsedDocument(entities=(), aliases=(), edges=())


def _qualified_name(metadata: dict[str, str], title: str) -> str:
    if title:
        return title
    namespace = metadata.get("namespace", "")
    for key in ("class", "function", "module", "package"):
        name = metadata.get(key, "")
        if name:
            return f"{namespace}.{name}" if namespace else name
    return ""


def _split_qualified(qualified: str) -> tuple[str, str]:
    if "." in qualified:
        namespace, short = qualified.rsplit(".", 1)
        return namespace, short
    return "", qualified


def _find_metadata_line(text: str, key: str) -> int:
    prefix = f"#{key}:"
    for index, line in enumerate(text.split("\n"), start=1):
        if line.startswith(prefix):
            return index
    return 1


def _build_aliases(entity_id: str, qualified: str, short: str) -> list[AliasDraft]:
    aliases = [
        AliasDraft(
            alias=qualified,
            entity_id=entity_id,
            alias_type="qualified_name",
            priority=_PRIORITY_QUALIFIED,
        ),
        AliasDraft(
            alias=short,
            entity_id=entity_id,
            alias_type="short_name",
            priority=_PRIORITY_SHORT,
        ),
    ]
    casefolded = qualified.casefold()
    if casefolded != qualified and casefolded != short:
        aliases.append(
            AliasDraft(
                alias=casefolded,
                entity_id=entity_id,
                alias_type="casefold_alias",
                priority=_PRIORITY_CASEFOLD,
            )
        )
    return aliases


def _reference_edges(
    entity_id: str, logical_path: str, references
) -> list[EdgeDraft]:
    edges: list[EdgeDraft] = []
    for ref in references:
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


def _parse_methods(
    text: str, logical_path: str, owner: str, class_id: str
) -> tuple[list[EntityDraft], list[EdgeDraft]]:
    lines = text.split("\n")
    methods: dict[str, dict] = {}
    order: list[str] = []

    index = 0
    while index < len(lines):
        header = _METHOD_HEADER_RE.match(lines[index])
        if not header:
            index += 1
            continue
        method_name = header.group("name")
        start_line = index + 1
        signatures: list[str] = []
        returns: str | None = None
        cppname: str | None = None
        body_lines: list[str] = []
        index += 1
        while index < len(lines):
            line = lines[index]
            if _METHOD_HEADER_RE.match(line) or _HEADING_RE.match(line):
                break
            sig = _SIGNATURE_RE.match(line)
            if sig:
                signatures.append(sig.group("value").strip())
                index += 1
                continue
            ret = _RETURNS_RE.match(line)
            if ret:
                returns = ret.group("value").strip()
                index += 1
                continue
            cpp = _CPPNAME_RE.match(line)
            if cpp:
                cppname = cpp.group("value").strip()
                index += 1
                continue
            body_lines.append(line)
            index += 1

        if method_name not in methods:
            methods[method_name] = {
                "signatures": [],
                "returns": None,
                "cppname": None,
                "body_lines": [],
                "start_line": start_line,
            }
            order.append(method_name)
        entry = methods[method_name]
        for signature in signatures:
            if signature not in entry["signatures"]:
                entry["signatures"].append(signature)
        if returns is not None and entry["returns"] is None:
            entry["returns"] = returns
        if cppname is not None and entry["cppname"] is None:
            entry["cppname"] = cppname
        entry["body_lines"].extend(body_lines)

    entities: list[EntityDraft] = []
    edges: list[EdgeDraft] = []
    for method_name in order:
        entry = methods[method_name]
        qualified = f"{owner}#{method_name}"
        method_id = make_entity_id(EntityKind.HOM_METHOD, qualified)
        body = clean_body("\n".join(entry["body_lines"]))
        summary = ""
        for line in entry["body_lines"]:
            if line.strip():
                summary = line.strip()
                break
        attributes: dict[str, object] = {
            "owner": owner,
            "name": method_name,
            "qualified_name": qualified,
            "signatures": tuple(entry["signatures"]),
            "returns": entry["returns"] or "",
            "cppname": entry["cppname"] or "",
            "source_anchor": method_name,
        }
        entities.append(
            EntityDraft(
                entity_id=method_id,
                kind=EntityKind.HOM_METHOD,
                subtype="",
                canonical_name=qualified,
                title=method_name,
                summary=summary,
                authority=Authority.OFFICIAL_HOUDINI_DOCS,
                source_path=logical_path,
                source_anchor=method_name,
                is_current=True,
                attributes=attributes,
                body=body,
                sections=(),
            )
        )
        edges.append(
            EdgeDraft(
                source_id=class_id,
                predicate="declares_method",
                target_id=method_id,
                target_raw=None,
                target_anchor=method_name,
                resolved=True,
                source_location=f"{logical_path}:{entry['start_line']}",
            )
        )
    return entities, edges


def parse_hom_document(source_path: str, text: str) -> ParsedDocument:
    """Parse a HOM page into a class/function/module/package entity and methods."""
    logical_path = normalize_source_path(source_path)
    metadata = parse_metadata(text)
    page_type = metadata.get("type", "")

    if page_type == "include" or page_type not in _KIND_BY_TYPE:
        return _EMPTY

    kind = _KIND_BY_TYPE[page_type]
    title = parse_title(text)
    qualified = _qualified_name(metadata, title)
    summary = parse_summary(text)
    namespace, short = _split_qualified(qualified)
    entity_id = make_entity_id(kind, qualified)

    attributes: dict[str, object] = {
        "namespace": namespace,
        "short_name": short,
        "qualified_name": qualified,
    }
    if "cppname" in metadata:
        attributes["cppname"] = metadata["cppname"]
    superclass = metadata.get("superclass", "")
    if superclass:
        attributes["superclass"] = superclass
    for key, value in metadata.items():
        if key not in _NAMED_METADATA:
            attributes.setdefault(key, value)

    entity = EntityDraft(
        entity_id=entity_id,
        kind=kind,
        subtype=page_type,
        canonical_name=qualified,
        title=title or qualified,
        summary=summary,
        authority=Authority.OFFICIAL_HOUDINI_DOCS,
        source_path=logical_path,
        source_anchor=None,
        is_current=True,
        attributes=attributes,
        body=clean_body(text),
        sections=parse_sections(text),
    )

    entities: list[EntityDraft] = [entity]
    aliases = _build_aliases(entity_id, qualified, short)
    edges: list[EdgeDraft] = []

    if superclass:
        edges.append(
            EdgeDraft(
                source_id=entity_id,
                predicate="inherits_from",
                target_id=None,
                target_raw=superclass,
                target_anchor=None,
                resolved=False,
                source_location=f"{logical_path}:{_find_metadata_line(text, 'superclass')}",
            )
        )

    if page_type == "homclass":
        method_entities, method_edges = _parse_methods(
            text, logical_path, qualified, entity_id
        )
        entities.extend(method_entities)
        edges.extend(method_edges)
        for method in method_entities:
            aliases.extend(
                _build_aliases(
                    method.entity_id,
                    method.attributes["qualified_name"],
                    method.attributes["name"],
                )
            )

    edges.extend(_reference_edges(entity_id, logical_path, parse_references(text)))

    return ParsedDocument(
        entities=tuple(entities),
        aliases=tuple(aliases),
        edges=tuple(edges),
    )
