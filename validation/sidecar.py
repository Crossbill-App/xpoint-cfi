"""Parse a KOReader ``metadata.epub.lua`` sidecar into typed annotation data.

The sidecar is a single ``return {...}`` table of pure data. We execute it with an
embedded Lua interpreter (:mod:`lupa`) — the files are trusted local fixtures — and walk
the returned table through a small typed conversion boundary. Highlights carry both a
``pos0`` and ``pos1`` crengine xpointer plus the recorded ``text``; entries with only a
page position are bookmarks and are skipped.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import cast

from lupa import LuaRuntime, lua_type  # pyright: ignore[reportMissingTypeStubs]

__all__ = ["Annotation", "SidecarData", "parse_sidecar"]


@dataclass(frozen=True)
class Annotation:
    """One KOReader highlight: a text range plus the text it covered."""

    pos0: str
    pos1: str
    text: str
    chapter: str | None = None
    pageno: int | None = None
    datetime: str | None = None


@dataclass(frozen=True)
class SidecarData:
    """The parsed contents of one sidecar relevant to validation."""

    title: str | None
    cre_dom_version: int | None
    doc_path: str | None
    annotations: list[Annotation]


def _lua_to_py(value: object) -> object:
    """Recursively convert a Lua value to plain Python.

    Lua tables become a ``list`` when their keys are exactly ``1..N`` (a sequence) and a
    ``dict`` otherwise. Scalars (``str``/``int``/``float``/``bool``) and ``nil`` pass
    through unchanged. This is the single boundary where the untyped :mod:`lupa` surface
    is narrowed back to typed Python.
    """
    if lua_type(value) != "table":  # pyright: ignore[reportUnknownArgumentType]
        return value
    keys: list[object] = list(value.keys())  # pyright: ignore[reportAttributeAccessIssue, reportUnknownArgumentType, reportUnknownMemberType]
    int_keys = sorted(k for k in keys if isinstance(k, int))
    is_sequence = (
        bool(int_keys)
        and len(int_keys) == len(keys)
        and int_keys == list(range(1, len(int_keys) + 1))
    )
    if is_sequence:
        return [_lua_to_py(value[k]) for k in int_keys]  # pyright: ignore[reportIndexIssue, reportUnknownArgumentType]
    return {k: _lua_to_py(value[k]) for k in keys}  # pyright: ignore[reportIndexIssue, reportUnknownArgumentType]


def _as_dict(value: object) -> dict[object, object]:
    return cast("dict[object, object]", value) if isinstance(value, dict) else {}


def _opt_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _opt_int(value: object) -> int | None:
    return value if isinstance(value, int) else None


def parse_sidecar(path: Path) -> SidecarData:
    """Parse ``path`` (a ``metadata.epub.lua``) into a :class:`SidecarData`.

    Raises:
        ValueError: if the sidecar has no ``annotations`` table — the pre-2024 KOReader
            ``highlight`` format stores ranges differently and is unsupported here.
    """
    content = path.read_text(encoding="utf-8")
    lua = LuaRuntime(unpack_returned_tuples=False)  # pyright: ignore[reportUnknownVariableType]
    root = _as_dict(_lua_to_py(lua.execute(content)))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]

    raw_annotations = root.get("annotations")
    if not isinstance(raw_annotations, list):
        raise ValueError(
            f"{path}: no 'annotations' table found. This looks like the pre-2024 "
            "KOReader 'highlight' sidecar format, which is not supported."
        )

    annotations: list[Annotation] = []
    for entry in cast("list[object]", raw_annotations):
        fields = _as_dict(entry)
        pos0 = fields.get("pos0")
        pos1 = fields.get("pos1")
        if not isinstance(pos0, str) or not isinstance(pos1, str):
            continue  # bookmark (page-only entry), not a highlight range
        annotations.append(
            Annotation(
                pos0=pos0,
                pos1=pos1,
                text=_opt_str(fields.get("text")) or "",
                chapter=_opt_str(fields.get("chapter")),
                pageno=_opt_int(fields.get("pageno")),
                datetime=_opt_str(fields.get("datetime")),
            )
        )

    doc_props = _as_dict(root.get("doc_props"))
    return SidecarData(
        title=_opt_str(doc_props.get("title")),
        cre_dom_version=_opt_int(root.get("cre_dom_version")),
        doc_path=_opt_str(root.get("doc_path")),
        annotations=annotations,
    )
