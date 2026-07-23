"""Unit tests for the Lua sidecar parser, using inline fixtures (no corpus needed)."""

from __future__ import annotations

from pathlib import Path

import pytest

from validation.sidecar import parse_sidecar

# Two highlights (entries 1 and 3) and one bookmark-only entry (2, page but no pos0/pos1).
# Entry 1's text exercises Lua string escapes: an escaped quote, a newline, and a
# ``\ddd`` decimal escape (``\116`` -> 't').
_LUA_WITH_HIGHLIGHTS = r"""
return {
    ["annotations"] = {
        [1] = {
            ["pos0"] = "/body/DocFragment[1]/body/p[1]/text().0",
            ["pos1"] = "/body/DocFragment[1]/body/p[1]/text().5",
            ["text"] = "quote:\" newline:\n dec:\116",
            ["chapter"] = "Chapter One",
            ["pageno"] = 3,
            ["datetime"] = "2026-01-01 12:00:00",
        },
        [2] = {
            ["page"] = "/body/DocFragment[1]/body/p[2]/text().0",
            ["pageno"] = 4,
        },
        [3] = {
            ["pos0"] = "/body/DocFragment[2]/body/p[1]/text().0",
            ["pos1"] = "/body/DocFragment[2]/body/p[1]/text().9",
            ["text"] = "second highlight",
        },
    },
    ["cre_dom_version"] = 20240114,
    ["doc_props"] = {
        ["title"] = "Fixture Title",
    },
}
"""

_LUA_NO_ANNOTATIONS = r"""
return {
    ["cre_dom_version"] = 20200223,
    ["highlight"] = {
        [1] = {
            ["0"] = { ["text"] = "old-format highlight" },
        },
    },
    ["doc_props"] = {
        ["title"] = "Legacy Book",
    },
}
"""


def _write(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "metadata.epub.lua"
    path.write_text(content, encoding="utf-8")
    return path


def test_parses_metadata_and_highlights(tmp_path: Path) -> None:
    data = parse_sidecar(_write(tmp_path, _LUA_WITH_HIGHLIGHTS))

    assert data.title == "Fixture Title"
    assert data.cre_dom_version == 20240114
    # The bookmark-only entry (2) is skipped; two highlights remain, in table order.
    assert len(data.annotations) == 2

    first, second = data.annotations
    assert first.pos0 == "/body/DocFragment[1]/body/p[1]/text().0"
    assert first.pos1 == "/body/DocFragment[1]/body/p[1]/text().5"
    assert first.chapter == "Chapter One"
    assert first.pageno == 3
    assert first.datetime == "2026-01-01 12:00:00"

    assert second.pos0 == "/body/DocFragment[2]/body/p[1]/text().0"
    assert second.text == "second highlight"
    # Optional fields absent on the second highlight default to None.
    assert second.chapter is None
    assert second.pageno is None
    assert second.datetime is None


def test_lua_string_escapes(tmp_path: Path) -> None:
    data = parse_sidecar(_write(tmp_path, _LUA_WITH_HIGHLIGHTS))
    assert data.annotations[0].text == 'quote:" newline:\n dec:t'


def test_missing_annotations_table_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="pre-2024"):
        parse_sidecar(_write(tmp_path, _LUA_NO_ANNOTATIONS))
