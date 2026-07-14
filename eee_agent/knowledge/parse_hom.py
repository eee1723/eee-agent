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

# Method headers look like ``::`call(self, arg)` -> [Hom:Type]:`` with many
# markup variants around the backticks, colons and return separators, and may
# span multiple indented lines. A method is recognized only when the
# backtick-quoted call contains ``(``; ``:: Name`` enum/constant declarations
# (no backtick call) are excluded. Recognition lives in ``_try_method_header``.
_HEADING_RE = re.compile(r"^={2,}\s")
_CPPNAME_RE = re.compile(r"^\s*#cppname:\s*(?P<value>.+?)\s*$")
_METHOD_NAME_RE = re.compile(r"^[A-Za-z_]\w*$")
_RETURN_SEP_RE = re.compile(r"^[:\s>-]+")

_EMPTY = ParsedDocument(entities=(), aliases=(), edges=())


def _qualified_from_path(logical_path: str) -> str:
    """Derive a stable qualified name from a normalized HOM source path.

    ``hou/Node.txt`` -> ``hou.Node``; ``hou/qt/ColorField.txt`` ->
    ``hou.qt.ColorField``. Used only when neither the title nor explicit
    metadata provide a name, so the entity id is never built from an empty key.
    """
    path = logical_path
    basename = path.rsplit("/", 1)[-1]
    if "." in basename:
        path = path.rsplit(".", 1)[0]
    return path.replace("/", ".")


def _qualified_name(metadata: dict[str, str], title: str, logical_path: str) -> str:
    if title:
        return title
    namespace = metadata.get("namespace", "")
    for key in ("class", "function", "module", "package"):
        name = metadata.get(key, "")
        if name:
            return f"{namespace}.{name}" if namespace else name
    return _qualified_from_path(logical_path)


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
    entity_id: str,
    logical_path: str,
    references,
    line_owner: dict[int, str] | None = None,
) -> list[EdgeDraft]:
    edges: list[EdgeDraft] = []
    seen: set[tuple[str, str, str, str | None, int]] = set()
    owner_map = line_owner or {}
    for ref in references:
        # A reference inside a method block (header return type or body) is
        # sourced from the owning method entity, not the class.
        source_id = owner_map.get(ref.source_line, entity_id)
        if ref.target_kind == "Include":
            predicate = "includes"
            target_raw = ref.raw_target
        elif ref.target_kind == "Anchor":
            predicate = "references"
            target_raw = f"#{ref.anchor}" if ref.anchor else ""
        else:
            predicate = "references"
            target_raw = f"{ref.target_kind}:{ref.raw_target}"
        # Two identical bracket occurrences on the same source line (e.g.
        # "a [Hom:hou.Parm] or tuple of [Hom:hou.Parm]") yield one edge.
        key = (source_id, predicate, target_raw, ref.anchor, ref.source_line)
        if key in seen:
            continue
        seen.add(key)
        edges.append(
            EdgeDraft(
                source_id=source_id,
                predicate=predicate,
                target_id=None,
                target_raw=target_raw,
                target_anchor=ref.anchor,
                resolved=False,
                source_location=f"{logical_path}:{ref.source_line}",
            )
        )
    return edges


def _extract_return(after: str) -> str:
    """Extract the return expression from the text after the call's backticks."""
    text = after.strip().rstrip(":").strip()
    match = _RETURN_SEP_RE.match(text)
    if match:
        return text[match.end():].strip()
    return text


def _signature(call: str, returns: str | None) -> str:
    """Build a stable signature preserving the call and return expression."""
    signature = f"`{call}`"
    if returns:
        signature = f"{signature} -> {returns}"
    return signature


def _try_method_header(
    lines: list[str], index: int
) -> tuple[str, str, str, str, int] | None:
    """If ``lines[index]`` starts a method header, return
    ``(method_name, call, returns, signature, next_index)``; else ``None``.

    ``next_index`` is the 0-based index of the first line after the header.
    Tolerates markup variants (``-``, ``- >``, ``: ->`` separators, trailing
    ``:``) and headers whose backtick call spans multiple indented lines.
    """
    if not lines[index].startswith("::`"):
        return None
    parts = [lines[index]]
    end = index
    while "\n".join(parts).count("`") < 2:
        end += 1
        if end >= len(lines):
            return None
        parts.append(lines[end])
    joined = "\n".join(parts)
    open_pos = joined.index("`")
    close_pos = joined.index("`", open_pos + 1)
    call = joined[open_pos + 1:close_pos]
    if "(" not in call:
        return None  # property / non-call -> not a method
    paren = call.find("(")
    method_name = call[:paren].strip()
    if not method_name or not _METHOD_NAME_RE.match(method_name):
        return None
    returns = _extract_return(joined[close_pos + 1:])
    normalized_call = re.sub(r"\s+", " ", call).strip()
    signature = _signature(normalized_call, returns)
    return method_name, call, returns, signature, end + 1


def _parse_methods(
    text: str, logical_path: str, owner: str, class_id: str
) -> tuple[list[EntityDraft], list[EdgeDraft], dict[int, str]]:
    lines = text.split("\n")
    methods: dict[str, dict] = {}
    order: list[str] = []
    # 1-based source line -> owning method entity id (for method-scoped refs).
    line_owner: dict[int, str] = {}

    index = 0
    while index < len(lines):
        header = _try_method_header(lines, index)
        if header is None:
            index += 1
            continue
        method_name, _call, returns, signature, next_index = header
        method_id = make_entity_id(EntityKind.HOM_METHOD, f"{owner}#{method_name}")
        start_line = index + 1
        for line_number in range(index + 1, next_index + 1):
            line_owner[line_number] = method_id
        cppname: str | None = None
        body_lines: list[str] = []
        index = next_index
        while index < len(lines):
            line = lines[index]
            if line.startswith("::`") or _HEADING_RE.match(line):
                break
            line_owner[index + 1] = method_id
            cpp = _CPPNAME_RE.match(line)
            if cpp:
                cppname = cpp.group("value").strip()
                index += 1
                continue
            stripped = line.strip()
            if stripped:
                body_lines.append(stripped)
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
        if signature not in entry["signatures"]:
            entry["signatures"].append(signature)
        if returns and entry["returns"] is None:
            entry["returns"] = returns
        if cppname and entry["cppname"] is None:
            entry["cppname"] = cppname
        entry["body_lines"].extend(body_lines)

    entities: list[EntityDraft] = []
    edges: list[EdgeDraft] = []
    for method_name in order:
        entry = methods[method_name]
        qualified = f"{owner}#{method_name}"
        method_id = make_entity_id(EntityKind.HOM_METHOD, qualified)
        body = clean_body("\n".join(entry["body_lines"]))
        summary = entry["body_lines"][0] if entry["body_lines"] else ""
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
    return entities, edges, line_owner


def parse_hom_document(source_path: str, text: str) -> ParsedDocument:
    """Parse a HOM page into a class/function/module/package entity and methods."""
    logical_path = normalize_source_path(source_path)
    metadata = parse_metadata(text)
    page_type = metadata.get("type", "")

    if page_type == "include" or page_type not in _KIND_BY_TYPE:
        return _EMPTY

    kind = _KIND_BY_TYPE[page_type]
    title = parse_title(text)
    qualified = _qualified_name(metadata, title, logical_path)
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

    line_owner: dict[int, str] = {}
    if page_type == "homclass":
        method_entities, method_edges, line_owner = _parse_methods(
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

    edges.extend(
        _reference_edges(
            entity_id, logical_path, parse_references(text), line_owner
        )
    )

    return ParsedDocument(
        entities=tuple(entities),
        aliases=tuple(aliases),
        edges=tuple(edges),
    )
