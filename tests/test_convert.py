"""Hand-verified conversion tests on the ``simple_book`` fixture.

Every expected CFI string is derived by hand-counting the fixture DOM (see the comment
blocks). ``chap1``'s body is::

    <body>                                  (2nd element child of <html> -> CFI /4)
      <div id="intro">                      (1st child of body          -> /2[intro])
        <p>Hello <i>brave</i> new <b>world</b>.</p>   (child 1 -> /2)
        <p>Before<!--c-->after</p>                    (child 2 -> /4)
        <p>Emoji U+1F600 tail</p>                     (child 3 -> /6)
        <p></p>                                        (child 4 -> /8)
        <div><p>alpha</p><p>beta</p></div>             (child 5 -> /10)
      </div>
    </body>

The package path is ``/6`` (the spine element step) then ``/2[ref1]`` (spine item 1).
"""

from __future__ import annotations

import pytest
from conftest import build_epub, xhtml_doc

from xpoint_cfi import (
    EpubMap,
    ResolutionError,
    cfi_to_xpoint_range_strings,
    cfi_to_xpoint_string,
    parse_cfi,
    xpoint_range_to_cfi_string,
    xpoint_to_cfi_string,
)


@pytest.fixture
def book(simple_book: bytes) -> EpubMap:
    return EpubMap.from_bytes(simple_book)


# --------------------------------------------------------------------------------------
# Single point: xpoint -> CFI
# --------------------------------------------------------------------------------------


def test_text_position_maps_to_full_cfi(book: EpubMap) -> None:
    # p[1] gap_0 ("Hello ") offset 0: element steps /4/2[intro]/2, odd step /1, offset :0
    xp = "/body/DocFragment[1]/body/div/p[1]/text().0"
    assert xpoint_to_cfi_string(book, xp) == "epubcfi(/6/2[ref1]!/4/2[intro]/2/1:0)"


def test_second_text_node_maps_to_odd_step_three(book: EpubMap) -> None:
    # p[1] text()[2] is the second non-empty chunk (" new "), CFI gap /3.
    xp = "/body/DocFragment[1]/body/div/p[1]/text()[2].2"
    assert xpoint_to_cfi_string(book, xp) == "epubcfi(/6/2[ref1]!/4/2[intro]/2/3:2)"


def test_emoji_offset_is_utf16_not_code_points(book: EpubMap) -> None:
    # "Emoji U+1F600 tail": code-point offset 8 sits after the astral emoji (2 UTF-16
    # units), so the CFI character offset is 9, not 8.
    xp = "/body/DocFragment[1]/body/div/p[3]/text().8"
    cfi = xpoint_to_cfi_string(book, xp)
    assert cfi == "epubcfi(/6/2[ref1]!/4/2[intro]/6/1:9)"
    assert ":9)" in cfi and ":8)" not in cfi


def test_nested_element_steps(book: EpubMap) -> None:
    # inner div is child 5 (/10); its second <p> ("beta") is child 2 (/4).
    xp = "/body/DocFragment[1]/body/div/div/p[2]/text().4"
    assert xpoint_to_cfi_string(book, xp) == "epubcfi(/6/2[ref1]!/4/2[intro]/10/4/1:4)"


def test_element_boundary_has_no_odd_step_or_offset(book: EpubMap) -> None:
    # chap2: <h1 id="title"/> is body child 1, <p> is child 2 (/4); <a> is p child 1 (/2).
    # An element-boundary xpoint (no ".offset") produces element steps only.
    xp = "/body/DocFragment[2]/body/p/a"
    assert xpoint_to_cfi_string(book, xp) == "epubcfi(/6/4[ref2]!/4/4/2)"


# --------------------------------------------------------------------------------------
# Single point: CFI -> xpoint (both directions round-trip modulo [1] normalization)
# --------------------------------------------------------------------------------------


def test_cfi_to_xpoint_text_position(book: EpubMap) -> None:
    cfi = "epubcfi(/6/2[ref1]!/4/2[intro]/6/1:9)"
    assert cfi_to_xpoint_string(book, cfi) == "/body/DocFragment[1]/body/div[1]/p[3]/text().8"


def test_cfi_to_xpoint_element_boundary(book: EpubMap) -> None:
    # Final step is even with no offset -> element-boundary xpoint (no /text() or .off).
    cfi = "epubcfi(/6/4[ref2]!/4/4/2)"
    assert cfi_to_xpoint_string(book, cfi) == "/body/DocFragment[2]/body/p[1]/a[1]"


@pytest.mark.parametrize(
    "xp",
    [
        "/body/DocFragment[1]/body/div/p[1]/text().0",
        "/body/DocFragment[1]/body/div/p[3]/text().8",
        "/body/DocFragment[1]/body/div/p[1]/text()[3].1",
        "/body/DocFragment[1]/body/div/div/p[2]/text().4",
        "/body/DocFragment[2]/body/p/a",
    ],
)
def test_xpoint_cfi_xpoint_round_trip_examples(book: EpubMap, xp: str) -> None:
    from xpoint_cfi import XPoint, normalize_xpath

    cfi = xpoint_to_cfi_string(book, xp)
    original = XPoint.parse(xp)
    back = XPoint.parse(cfi_to_xpoint_string(book, cfi))
    # Round-trip is identity modulo [1] normalization of the element xpath.
    assert normalize_xpath(back.xpath) == normalize_xpath(original.xpath)
    assert back.doc_fragment_index == original.doc_fragment_index
    assert back.text_node_index == original.text_node_index
    assert back.char_offset == original.char_offset
    assert back.has_text_position == original.has_text_position


# --------------------------------------------------------------------------------------
# Error cases
# --------------------------------------------------------------------------------------


def test_nested_indirection_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="nested indirection"):
        cfi_to_xpoint_string(book, "epubcfi(/6/2[ref1]!/4/2[intro]/2!/2/1:0)")


def test_wrong_spine_element_step_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="spine element step"):
        cfi_to_xpoint_string(book, "epubcfi(/8/2[ref1]!/4/2[intro]/2/1:0)")


def test_package_only_cfi_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="package level"):
        cfi_to_xpoint_string(book, "epubcfi(/6/2[ref1])")


def test_offset_on_element_step_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="offset"):
        cfi_to_xpoint_string(book, "epubcfi(/6/2[ref1]!/4/2[intro]/2:0)")


def test_unresolvable_element_step_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError):
        cfi_to_xpoint_string(book, "epubcfi(/6/2[ref1]!/4/2[intro]/98/1:0)")


def test_unresolvable_xpath_is_rejected(book: EpubMap) -> None:
    with pytest.raises(ResolutionError):
        xpoint_to_cfi_string(book, "/body/DocFragment[1]/body/div/p[99]/text().0")


def test_range_string_rejected_by_single_cfi_helper(book: EpubMap) -> None:
    rng = "epubcfi(/6/2[ref1]!/4/2[intro]/2/1,:0,:5)"
    with pytest.raises(ResolutionError, match="range"):
        cfi_to_xpoint_string(book, rng)


def test_single_cfi_rejected_by_range_helper(book: EpubMap) -> None:
    with pytest.raises(ResolutionError, match="range"):
        cfi_to_xpoint_range_strings(book, "epubcfi(/6/2[ref1]!/4/2[intro]/2/1:0)")


# --------------------------------------------------------------------------------------
# Range factoring: exact serialized forms + parse round-trip
# --------------------------------------------------------------------------------------


def _assert_parseable(cfi_str: str) -> None:
    # Re-parsing and re-serializing must reproduce the string losslessly.
    assert parse_cfi(cfi_str).to_string() == cfi_str


def test_range_same_element_same_chunk(book: EpubMap) -> None:
    # Both ends in p[1] gap_0: only the offset differs, but the subpaths still keep the
    # final text step (bare-offset subpaths break common resolvers).
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/div/p[1]/text().0",
        "/body/DocFragment[1]/body/div/p[1]/text().5",
    )
    assert cfi == "epubcfi(/6/2[ref1]!/4/2[intro]/2,/1:0,/1:5)"
    _assert_parseable(cfi)


def test_range_same_doc_different_elements(book: EpubMap) -> None:
    # Ends in p[1] and p[3]: common prefix stops at div#intro; start/end carry /2 vs /6.
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/div/p[1]/text().0",
        "/body/DocFragment[1]/body/div/p[3]/text().2",
    )
    assert cfi == "epubcfi(/6/2[ref1]!/4/2[intro],/2/1:0,/6/1:2)"
    _assert_parseable(cfi)


def test_range_cross_spine_item_parent_is_spine_element_step_only(book: EpubMap) -> None:
    # Different spine items: the common prefix is only the /6 spine element step, so each
    # subpath carries its own itemref step and '!' document path.
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/div/p[1]/text().0",
        "/body/DocFragment[2]/body/p/a/text().1",
    )
    assert cfi == "epubcfi(/6,/2[ref1]!/4/2[intro]/2/1:0,/4[ref2]!/4/4/2/1:1)"
    _assert_parseable(cfi)


def test_range_zero_length_keeps_offset_in_both_ends(book: EpubMap) -> None:
    cfi = xpoint_range_to_cfi_string(
        book,
        "/body/DocFragment[1]/body/div/p[1]/text().2",
        "/body/DocFragment[1]/body/div/p[1]/text().2",
    )
    assert cfi == "epubcfi(/6/2[ref1]!/4/2[intro]/2,/1:2,/1:2)"
    _assert_parseable(cfi)


def test_range_round_trips_back_to_xpoints(book: EpubMap) -> None:
    start = "/body/DocFragment[1]/body/div/p[1]/text().0"
    end = "/body/DocFragment[2]/body/p/a/text().1"
    cfi = xpoint_range_to_cfi_string(book, start, end)
    assert cfi_to_xpoint_range_strings(book, cfi) == (
        "/body/DocFragment[1]/body/div[1]/p[1]/text().0",
        "/body/DocFragment[2]/body/p[1]/a[1]/text().1",
    )


def test_element_boundary_end_in_range(book: EpubMap) -> None:
    # An element-boundary end (no offset) factors correctly and survives a round-trip.
    start = "/body/DocFragment[2]/body/h1/text().0"
    end = "/body/DocFragment[2]/body/p/a"
    cfi = xpoint_range_to_cfi_string(book, start, end)
    _assert_parseable(cfi)
    back = cfi_to_xpoint_range_strings(book, cfi)
    assert back == (
        "/body/DocFragment[2]/body/h1[1]/text().0",
        "/body/DocFragment[2]/body/p[1]/a[1]",
    )


# --------------------------------------------------------------------------------------
# img.0 style endpoints: has_text_position but zero countable text -> element boundary
# --------------------------------------------------------------------------------------


@pytest.fixture
def img_book() -> EpubMap:
    # body: <p>text</p><p><img/></p> -> the img paragraph is body child 2 (/4), the <img>
    # is its child 1 (/2). An `.../p[2]/img.0` xpoint has a text position but the element
    # has no countable text node.
    body = '<p>Some text</p><p><img src="x.png"/></p>'
    return EpubMap.from_bytes(build_epub({"a.xhtml": xhtml_doc("A", body)}))


def test_img_endpoint_converts_to_element_boundary_cfi(img_book: EpubMap) -> None:
    xp = "/body/DocFragment[1]/body/p[2]/img.0"
    # Element steps only: /4 (body) /4 (p[2]) /2 (img). No text step, no offset.
    assert xpoint_to_cfi_string(img_book, xp) == "epubcfi(/6/2[ref1]!/4/4/2)"


def test_img_endpoint_reverse_yields_element_boundary_xpoint(img_book: EpubMap) -> None:
    cfi = "epubcfi(/6/2[ref1]!/4/4/2)"
    # Round-trips back as an element-boundary xpoint (has_text_position=False).
    assert cfi_to_xpoint_string(img_book, cfi) == "/body/DocFragment[1]/body/p[2]/img[1]"


def test_range_ending_on_img_endpoint_factors(img_book: EpubMap) -> None:
    start = "/body/DocFragment[1]/body/p[1]/text().0"
    end = "/body/DocFragment[1]/body/p[2]/img.0"
    cfi = xpoint_range_to_cfi_string(img_book, start, end)
    _assert_parseable(cfi)
    # The end is an element boundary (/4/2), the start a text position under p[1] (/2..).
    assert cfi == "epubcfi(/6/2[ref1]!/4,/2/1:0,/4/2)"
    back = cfi_to_xpoint_range_strings(img_book, cfi)
    assert back == (
        "/body/DocFragment[1]/body/p[1]/text().0",
        "/body/DocFragment[1]/body/p[2]/img[1]",
    )


def test_img_endpoint_with_nonzero_offset_still_errors(img_book: EpubMap) -> None:
    # Only the default text()[1].0 shape degrades; a non-zero offset on a textless
    # element remains an error.
    with pytest.raises(ResolutionError):
        xpoint_to_cfi_string(img_book, "/body/DocFragment[1]/body/p[2]/img.5")
