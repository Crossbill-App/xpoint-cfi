"""Tests for :mod:`xpoint_cfi.crengine_text` — the whitespace-collapse bridge.

Semantics are frozen (validated against a real-book highlight corpus): runs of the four
ASCII whitespace characters collapse to a single space, with no trim; NBSP and other
Unicode spaces survive; the position map is code-point based, not UTF-16 based.
"""

from __future__ import annotations

import pytest

from xpoint_cfi.crengine_text import (
    collapse,
    collapse_with_map,
    is_countable,
    raw_to_collapsed,
)

EMOJI = "\U0001f600"  # one code point, two UTF-16 units
NBSP = "\u00a0"  # no-break space; not collapsible


# --------------------------------------------------------------------------------------
# collapse / collapse_with_map
# --------------------------------------------------------------------------------------


def test_collapse_no_whitespace_is_identity() -> None:
    collapsed, pos = collapse_with_map("abc")
    assert collapsed == "abc"
    assert pos == (0, 1, 2, 3)


def test_collapse_internal_space_run() -> None:
    assert collapse_with_map("a  b") == ("a b", (0, 1, 3, 4))


def test_collapse_mixed_whitespace_run() -> None:
    # space, tab, CR, LF all belong to one collapsible run -> a single space.
    collapsed, pos = collapse_with_map("a \t\r\n b")
    assert collapsed == "a b"
    # run starts at index 1; the run spans indices 1..5, then 'b' at index 6.
    assert pos == (0, 1, 6, 7)


def test_collapse_leading_run_is_not_trimmed() -> None:
    collapsed, pos = collapse_with_map("   abc")
    assert collapsed == " abc"
    assert pos == (0, 3, 4, 5, 6)


def test_collapse_trailing_run_is_not_trimmed() -> None:
    collapsed, pos = collapse_with_map("abc   ")
    assert collapsed == "abc "
    assert pos == (0, 1, 2, 3, 6)


def test_collapse_all_whitespace() -> None:
    collapsed, pos = collapse_with_map(" \t\n")
    assert collapsed == " "
    assert pos == (0, 3)


def test_collapse_empty_string() -> None:
    collapsed, pos = collapse_with_map("")
    assert collapsed == ""
    assert pos == (0,)


def test_nbsp_is_not_collapsed() -> None:
    text = f"a{NBSP}{NBSP}b"
    collapsed, pos = collapse_with_map(text)
    assert collapsed == text  # NBSP survives verbatim, both copies
    assert pos == (0, 1, 2, 3, 4)


def test_nbsp_between_ascii_spaces_survives_but_spaces_collapse() -> None:
    # "a" " " NBSP " " "b": each ASCII space is its own run around the untouched NBSP.
    text = f"a {NBSP} b"
    assert collapse(text) == f"a {NBSP} b"


def test_emoji_before_and_after_runs_is_codepoint_based() -> None:
    text = f"{EMOJI}  {EMOJI}"
    collapsed, pos = collapse_with_map(text)
    assert collapsed == f"{EMOJI} {EMOJI}"
    # Code-point indices: emoji at 0, run starts at 1, second emoji at 3.
    assert pos == (0, 1, 3, 4)


def test_map_exactness_examples() -> None:
    for text, expected in [
        ("a  b", ("a b", (0, 1, 3, 4))),
        ("x   y   z", ("x y z", (0, 1, 4, 5, 8, 9))),
    ]:
        assert collapse_with_map(text) == expected


# --------------------------------------------------------------------------------------
# raw_to_collapsed consistency
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "abc",
        "a  b",
        "   abc",
        "abc   ",
        "a \t\r\n b",
        " \t\n",
        "",
        f"a{NBSP}{NBSP}b",
        f"{EMOJI}  {EMOJI}",
        "one two  three   four",
        f"{EMOJI} \t x {NBSP} y",
    ],
)
def test_raw_to_collapsed_matches_map(text: str) -> None:
    collapsed, pos = collapse_with_map(text)
    for i in range(len(collapsed) + 1):
        assert raw_to_collapsed(text, pos[i]) == i


@pytest.mark.parametrize(
    "text",
    ["abc", "a  b", "   abc", "one two  three   four", f"{EMOJI}  {EMOJI}"],
)
def test_raw_to_collapsed_equals_len_collapse_prefix(text: str) -> None:
    for raw_offset in range(len(text) + 1):
        assert raw_to_collapsed(text, raw_offset) == len(collapse(text[:raw_offset]))


# --------------------------------------------------------------------------------------
# is_countable
# --------------------------------------------------------------------------------------


def test_is_countable_cases() -> None:
    assert is_countable("x")
    assert is_countable("  x  ")
    assert is_countable(f"{NBSP}")  # NBSP is not a collapsible char -> countable
    assert not is_countable("")
    assert not is_countable(" ")
    assert not is_countable(" \t\r\n")
