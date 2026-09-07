"""Tests for the comparison normalizers and the position-preserving back-map."""

from __future__ import annotations

import unicodedata
from itertools import pairwise

import pytest

from xpoint_cfi.normalize import (
    normalize_for_comparison,
    normalize_whitespace,
    normalize_with_map,
)

_SOFT_HYPHEN = "\u00ad"
_ZWSP = "\u200b"
_NBSP = "\u00a0"
_BOM = "\ufeff"
_EMOJI = "\U0001f600"


def test_normalize_whitespace_collapses_and_strips() -> None:
    assert normalize_whitespace("  a\n\t b   c ") == "a b c"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (f"soft{_SOFT_HYPHEN}hyphen", "softhyphen"),
        (f"zero{_ZWSP}width", "zerowidth"),
        (f"no{_NBSP}break", "no break"),
        (f"{_BOM}bom", "bom"),
        ("  spread   out  ", "spread out"),
    ],
)
def test_normalize_for_comparison(raw: str, expected: str) -> None:
    assert normalize_for_comparison(raw) == expected


def test_map_example_from_the_docstring() -> None:
    assert normalize_with_map("a  b") == ("a b", (0, 1, 3, 4))


@pytest.mark.parametrize(
    "raw",
    [
        "plain text",
        "  leading and trailing  ",
        "a\n\n\nb\tc",
        f"soft{_SOFT_HYPHEN}hyphenated word",
        f"emoji {_EMOJI} tail",
        f"no{_NBSP}break{_NBSP}spaces",
        "décomposed accents: à ê ö",
        "märchen und hýphens",
        "",
        "   ",
    ],
)
def test_map_agrees_with_normalize_for_comparison(raw: str) -> None:
    # The two differ only in the strip, so stripping the mapped form recovers the other.
    normalized, _ = normalize_with_map(raw)
    assert normalized.strip() == normalize_for_comparison(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "plain text",
        "a  b   c",
        f"soft{_SOFT_HYPHEN}hyphen {_ZWSP}marks",
        f"emoji {_EMOJI} tail",
        f"{_NBSP}nbsp{_NBSP}runs{_NBSP}",
    ],
)
def test_map_has_one_source_index_per_output_position_plus_a_sentinel(raw: str) -> None:
    normalized, source = normalize_with_map(raw)
    assert len(source) == len(normalized) + 1
    assert source[-1] == len(raw)
    assert all(a <= b for a, b in pairwise(source))


def test_map_points_at_the_source_character_that_produced_each_position() -> None:
    # The soft hyphen vanishes, so the characters after it keep pointing past it.
    normalized, source = normalize_with_map(f"ab{_SOFT_HYPHEN}cd")
    assert normalized == "abcd"
    assert source == (0, 1, 3, 4, 5)


def test_astral_characters_occupy_exactly_one_source_position() -> None:
    # Python indexes code points, so the emoji is one position, not the two UTF-16 units
    # a CFI offset would count.
    normalized, source = normalize_with_map(f"a{_EMOJI}b")
    assert normalized == f"a{_EMOJI}b"
    assert source == (0, 1, 2, 3)


def test_whitespace_run_maps_to_its_first_character() -> None:
    normalized, source = normalize_with_map("a \t\n b")
    assert normalized == "a b"
    assert source == (0, 1, 5, 6)


def test_decomposed_text_composes_and_stays_mappable() -> None:
    # A file stored in NFD must still match a quote in NFC, and every output character
    # must keep a source position: the accent composes onto its base and maps at it.
    raw = unicodedata.normalize("NFD", "café")
    assert len(raw) == 5  # c a f e + combining acute
    normalized, source = normalize_with_map(raw)
    assert normalized == "café"
    assert source == (0, 1, 2, 3, 5)


def test_composed_and_decomposed_sources_normalize_alike() -> None:
    composed = normalize_with_map(unicodedata.normalize("NFC", "naïve"))[0]
    decomposed = normalize_with_map(unicodedata.normalize("NFD", "naïve"))[0]
    assert composed == decomposed == "naïve"
