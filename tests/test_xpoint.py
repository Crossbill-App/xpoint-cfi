"""Tests for KOReader xpointer value objects."""

from __future__ import annotations

import pytest

from xpoint_cfi.exceptions import XPointParseError
from xpoint_cfi.xpoint import XPoint, XPointRange, normalize_xpath


def test_parse_full_text_offset() -> None:
    xp = XPoint.parse("/body/DocFragment[12]/body/div/p[88]/text().223")
    assert xp.doc_fragment_index == 12
    assert xp.xpath == "/body/div/p[88]"
    assert xp.text_node_index == 1
    assert xp.char_offset == 223
    assert xp.has_text_position is True


def test_parse_element_boundary() -> None:
    xp = XPoint.parse("/body/DocFragment[14]/body/a")
    assert xp.doc_fragment_index == 14
    assert xp.xpath == "/body/a"
    assert xp.text_node_index == 1
    assert xp.char_offset == 0
    assert xp.has_text_position is False


def test_parse_img_offset_is_text_position() -> None:
    xp = XPoint.parse("/body/DocFragment[20]/body/div/p[1]/img.0")
    assert xp.xpath == "/body/div/p[1]/img"
    assert xp.text_node_index == 1
    assert xp.char_offset == 0
    assert xp.has_text_position is True


def test_parse_missing_doc_fragment_defaults_to_one() -> None:
    xp = XPoint.parse("/body/div/p/text().42")
    assert xp.doc_fragment_index == 1
    assert xp.xpath == "/body/div/p"
    assert xp.char_offset == 42
    assert xp.has_text_position is True


def test_parse_text_node_index() -> None:
    xp = XPoint.parse("/body/DocFragment[1]/body/div/p/text()[2].5")
    assert xp.text_node_index == 2
    assert xp.char_offset == 5
    assert xp.has_text_position is True


def test_parse_text_without_index() -> None:
    xp = XPoint.parse("/body/DocFragment[1]/body/div/p/text().5")
    assert xp.text_node_index == 1
    assert xp.char_offset == 5
    assert xp.has_text_position is True


EXACT_ROUND_TRIP = [
    "/body/DocFragment[12]/body/div/p[88]/text().223",
    "/body/DocFragment[14]/body/a",
    "/body/DocFragment[1]/body/div/p/text()[2].5",
    "/body/DocFragment[1]/body/div/p/text().5",
]


@pytest.mark.parametrize("raw", EXACT_ROUND_TRIP)
def test_exact_string_round_trip(raw: str) -> None:
    assert XPoint.parse(raw).to_string() == raw


SEMANTIC_ROUND_TRIP = [
    "/body/DocFragment[12]/body/div/p[88]/text().223",
    "/body/DocFragment[14]/body/a",
    "/body/DocFragment[20]/body/div/p[1]/img.0",
    "/body/div/p/text().42",
    "/body/DocFragment[1]/body/div/p/text()[2].5",
    "/body/DocFragment[1]/body/div/p/text().5",
]


@pytest.mark.parametrize("raw", SEMANTIC_ROUND_TRIP)
def test_semantic_round_trip(raw: str) -> None:
    xp = XPoint.parse(raw)
    assert XPoint.parse(xp.to_string()) == xp


def test_element_boundary_round_trip_fidelity() -> None:
    raw = "/body/DocFragment[14]/body/a"
    xp = XPoint.parse(raw)
    assert xp.has_text_position is False
    assert xp.to_string() == raw


def test_img_offset_serializes_with_text_and_reparses() -> None:
    xp = XPoint.parse("/body/DocFragment[20]/body/div/p[1]/img.0")
    assert xp.to_string() == "/body/DocFragment[20]/body/div/p[1]/img/text().0"
    assert XPoint.parse(xp.to_string()) == xp


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "not-an-xpoint",
        "/body/div/p/text()",
        "body/div/p",
        "/head/title",
        "/body/div/p/text()[1]",
        "/body/div/p.",
    ],
)
def test_parse_rejects_bad_strings(raw: str) -> None:
    with pytest.raises(XPointParseError):
        XPoint.parse(raw)


def test_parse_rejects_zero_doc_fragment() -> None:
    with pytest.raises(XPointParseError):
        XPoint.parse("/body/DocFragment[0]/body/a")


def test_parse_rejects_zero_text_node_index() -> None:
    with pytest.raises(XPointParseError):
        XPoint.parse("/body/div/p/text()[0].5")


@pytest.mark.parametrize("boxing", ["autoBoxing", "floatBox", "inlineBox", "tabularBox"])
def test_parse_rejects_boxing_elements(boxing: str) -> None:
    with pytest.raises(XPointParseError, match="boxing element"):
        XPoint.parse(f"/body/DocFragment[1]/body/div/{boxing}/p[1]/text().0")


def test_boxing_rejection_is_case_sensitive() -> None:
    xp = XPoint.parse("/body/DocFragment[1]/body/div/autobox/p[1]/text().0")
    assert xp.xpath == "/body/div/autobox/p[1]"


def test_normalize_xpath_defaults_missing_index() -> None:
    assert normalize_xpath("/body/div/p[88]") == (("body", 1), ("div", 1), ("p", 88))


def test_normalize_xpath_all_explicit() -> None:
    assert normalize_xpath("/body[1]/div[2]/p[3]") == (("body", 1), ("div", 2), ("p", 3))


def test_normalize_xpath_single_segment() -> None:
    assert normalize_xpath("/body") == (("body", 1),)


@pytest.mark.parametrize(
    "xpath",
    [
        "/body/p[-1]",
        "/body/p[0]",
        "/body/p[abc]",
        "/body/text().0",
        "/body/1p",
    ],
)
def test_normalize_xpath_rejects_bad_segments(xpath: str) -> None:
    with pytest.raises(XPointParseError):
        normalize_xpath(xpath)


def test_range_same_element_ascending_offsets_ok() -> None:
    rng = XPointRange.parse(
        "/body/DocFragment[1]/body/p/text().5",
        "/body/DocFragment[1]/body/p/text().10",
    )
    assert rng.start.char_offset == 5
    assert rng.end.char_offset == 10


def test_range_same_element_equal_offsets_ok() -> None:
    rng = XPointRange.parse(
        "/body/DocFragment[1]/body/p/text().5",
        "/body/DocFragment[1]/body/p/text().5",
    )
    assert rng.start == rng.end


def test_range_same_element_reversed_offsets_rejected() -> None:
    with pytest.raises(XPointParseError):
        XPointRange.parse(
            "/body/DocFragment[1]/body/p/text().10",
            "/body/DocFragment[1]/body/p/text().5",
        )


def test_range_same_element_reversed_text_nodes_rejected() -> None:
    with pytest.raises(XPointParseError):
        XPointRange.parse(
            "/body/DocFragment[1]/body/p/text()[2].0",
            "/body/DocFragment[1]/body/p/text()[1].0",
        )


def test_range_cross_fragment_ok() -> None:
    rng = XPointRange.parse(
        "/body/DocFragment[1]/body/p/text().10",
        "/body/DocFragment[2]/body/p/text().5",
    )
    assert rng.start.doc_fragment_index == 1
    assert rng.end.doc_fragment_index == 2


def test_range_cross_fragment_reversed_rejected() -> None:
    with pytest.raises(XPointParseError):
        XPointRange.parse(
            "/body/DocFragment[3]/body/p/text().0",
            "/body/DocFragment[1]/body/p/text().0",
        )


def test_range_different_element_same_fragment_not_offset_validated() -> None:
    rng = XPointRange.parse(
        "/body/DocFragment[1]/body/p[2]/text().10",
        "/body/DocFragment[1]/body/p[1]/text().0",
    )
    assert rng.start.xpath != rng.end.xpath
