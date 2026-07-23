"""Tests for range verification: text extraction and whitespace-tolerant comparison."""

from __future__ import annotations

import pytest
from conftest import build_epub, xhtml_doc

from xpoint_cfi import (
    EpubMap,
    ResolutionError,
    normalize_whitespace,
    parse_cfi,
    verify_range,
    xpoint_range_to_cfi_string,
)
from xpoint_cfi.cfi import CfiRange

# chap1 p[1] renders "Hello brave new world." as one interleaved text stream; chap2 is
# "Second" (h1) then "A link here." (p). These give same-element, cross-element, and
# cross-spine ranges.
_CHAP1 = "<p>Hello <i>brave</i> new <b>world</b>.</p><p>Second para here.</p>"
_CHAP2 = '<h1 id="t">Chapter Two</h1><p>Body text.</p>'


@pytest.fixture
def book() -> EpubMap:
    data = build_epub(
        {
            "chap1.xhtml": xhtml_doc("C1", _CHAP1),
            "chap2.xhtml": xhtml_doc("C2", _CHAP2),
        }
    )
    return EpubMap.from_bytes(data)


def test_normalize_whitespace_collapses_and_strips() -> None:
    assert normalize_whitespace("  a\n\t b   c ") == "a b c"


def test_same_element_range_matches(book: EpubMap) -> None:
    # p[1] full text stream: gap_0 offset 0 .. gap_5 (".") offset 1.
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[1]/text()[3].1",
    )
    result = verify_range(book, cfi, "Hello brave new world.")
    assert result.ok
    assert result.extracted_text == "Hello brave new world."


def test_cross_element_range_matches(book: EpubMap) -> None:
    # From start of p[1] to "Second" (6 chars) of p[2]. The source has no whitespace
    # between the paragraphs; extraction inserts the block separator KOReader uses, so
    # the words stay separated exactly as KOReader's exported text has them.
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[2]/text().6",
    )
    result = verify_range(book, cfi, "Hello brave new world. Second")
    assert result.ok
    assert result.extracted_text == "Hello brave new world.\nSecond"


def test_cross_spine_range_matches(book: EpubMap) -> None:
    # Tail of chap1 p[2] + all of chap2's head up to "Chapter" (7 chars of the h1).
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[2]/text().12",
        "/body/DocFragment[2]/body/h1/text().7",
    )
    result = verify_range(book, cfi, "here.Chapter")
    assert result.ok


def test_whitespace_normalization_tolerance(book: EpubMap) -> None:
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[1]/text()[3].1",
    )
    # Extra/irregular whitespace in the expected text is tolerated.
    assert verify_range(book, cfi, "  Hello   brave\nnew  world. ").ok


def test_element_boundary_end_encloses_whole_element(book: EpubMap) -> None:
    # An element-boundary end at <b>world</b> must include "world".
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[1]/b",
    )
    assert verify_range(book, cfi, "Hello brave new world").ok


def test_accepts_cfi_range_object(book: EpubMap) -> None:
    cfi_str = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[1]/text().5",
    )
    parsed = parse_cfi(cfi_str)
    assert isinstance(parsed, CfiRange)
    assert verify_range(book, parsed, "Hello").ok


def test_mismatch_against_a_different_book_is_detected(book: EpubMap) -> None:
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[1]/text()[3].1",
    )
    # Rebuild the fixture with edited text but identical structure; the same CFI now
    # resolves to different characters, so verification fails.
    edited = build_epub(
        {
            "chap1.xhtml": xhtml_doc(
                "C1", "<p>Howdy <i>bold</i> old <b>earth</b>!</p><p>Second para here.</p>"
            ),
            "chap2.xhtml": xhtml_doc("C2", _CHAP2),
        }
    )
    other = EpubMap.from_bytes(edited)
    result = verify_range(other, cfi, "Hello brave new world.")
    assert not result.ok
    assert result.extracted_text == "Howdy bold old earth!"


def test_non_range_cfi_string_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="range"):
        verify_range(book, "epubcfi(/6/2[ref1]!/4/2[t]/2/1:0)", "x")
