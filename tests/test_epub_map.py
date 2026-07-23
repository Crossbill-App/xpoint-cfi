"""Tests for the document layer: EPUB parsing, element/text addressing, chunk model."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest
from conftest import build_epub, xhtml_doc

from xpoint_cfi.cfi import Step
from xpoint_cfi.epub_map import (
    EpubMap,
    NodeMap,
    _Element,  # pyright: ignore[reportPrivateUsage]
    cp_to_utf16,
    utf16_to_cp,
)
from xpoint_cfi.exceptions import EpubStructureError, ResolutionError
from xpoint_cfi.xpoint import normalize_xpath

EMOJI = "\U0001f600"  # U+1F600, one code point / two UTF-16 units


# --------------------------------------------------------------------------------------
# Container / OPF parsing
# --------------------------------------------------------------------------------------


def _require_text_pos(nm: NodeMap, elem: _Element, node_index: int, offset: int) -> tuple[int, int]:
    loc = nm.text_position_to_cfi(elem, node_index, offset)
    assert loc is not None
    return loc


def test_spine_count(simple_book: bytes) -> None:
    assert EpubMap.from_bytes(simple_book).spine_count == 3


def test_spine_order_and_hrefs(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.spine_href(1) == "OEBPS/chap1.xhtml"
    assert book.spine_href(2) == "OEBPS/chap2.xhtml"
    assert book.spine_href(3) == "OEBPS/chap3.xhtml"
    assert book.spine_idref(1) == "item1"


def test_spine_element_step_default_is_six(simple_book: bytes) -> None:
    assert EpubMap.from_bytes(simple_book).spine_element_step == 6


def test_spine_element_step_is_computed_with_padding() -> None:
    data = build_epub({"a.xhtml": xhtml_doc("A", "<p>x</p>")}, spine_step_padding=True)
    assert EpubMap.from_bytes(data).spine_element_step == 8


def test_href_with_percent_escape_is_decoded() -> None:
    data = build_epub({"ch 1.xhtml": xhtml_doc("A", "<p>hi</p>")})
    book = EpubMap.from_bytes(data)
    assert book.spine_href(1) == "OEBPS/ch 1.xhtml"
    assert book.doc(1).extract_text(None, None) == "hi"


def test_from_path_reads_file(simple_book: bytes, tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    path.write_bytes(simple_book)
    assert EpubMap.from_path(path).spine_count == 3


def test_missing_container_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("OEBPS/content.opf", "<package/>")
    with pytest.raises(EpubStructureError, match="container"):
        EpubMap.from_bytes(buffer.getvalue())


def test_not_a_zip_raises() -> None:
    with pytest.raises(EpubStructureError):
        EpubMap.from_bytes(b"this is not a zip")


def test_bad_idref_raises() -> None:
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<manifest><item id="item1" href="a.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest>"
        '<spine><itemref idref="does-not-exist"/></spine>'
        "</package>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr("OEBPS/content.opf", opf)
        archive.writestr("OEBPS/a.xhtml", xhtml_doc("A", "<p>x</p>"))
    with pytest.raises(EpubStructureError, match="no matching manifest"):
        EpubMap.from_bytes(buffer.getvalue())


def test_missing_spine_raises() -> None:
    opf = (
        '<?xml version="1.0"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<manifest><item id="item1" href="a.xhtml" media-type="application/xhtml+xml"/>'
        "</manifest></package>"
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr("OEBPS/content.opf", opf)
    with pytest.raises(EpubStructureError, match="spine"):
        EpubMap.from_bytes(buffer.getvalue())


def test_malformed_opf_raises() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        archive.writestr("OEBPS/content.opf", "<package><spine>")
    with pytest.raises(EpubStructureError):
        EpubMap.from_bytes(buffer.getvalue())


# --------------------------------------------------------------------------------------
# Spine step addressing
# --------------------------------------------------------------------------------------


def test_spine_step_uses_itemref_own_id(simple_book: bytes) -> None:
    assert EpubMap.from_bytes(simple_book).spine_step(2) == Step(index=4, assertion="ref2")


def test_spine_step_out_of_range(simple_book: bytes) -> None:
    with pytest.raises(ResolutionError):
        EpubMap.from_bytes(simple_book).spine_step(9)


def test_spine_index_for_step_round_trip(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    for index in range(1, book.spine_count + 1):
        assert book.spine_index_for_step(book.spine_step(index)) == index


def test_spine_index_for_step_even_in_range_wins(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.spine_index_for_step(Step(index=6, assertion="mismatch")) == 3


def test_spine_index_for_step_self_repair_via_itemref_id(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.spine_index_for_step(Step(index=999, assertion="ref2")) == 2


def test_spine_index_for_step_self_repair_via_idref_fallback(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.spine_index_for_step(Step(index=999, assertion="item2")) == 2


def test_spine_index_for_step_odd_falls_back_to_assertion(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.spine_index_for_step(Step(index=7, assertion="ref1")) == 1


def test_spine_index_for_step_unresolvable(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    with pytest.raises(ResolutionError):
        book.spine_index_for_step(Step(index=999, assertion="ghost"))


# --------------------------------------------------------------------------------------
# Element addressing: xpath
# --------------------------------------------------------------------------------------


@pytest.fixture
def chap1(simple_book: bytes) -> NodeMap:
    return EpubMap.from_bytes(simple_book).doc(1)


def test_element_by_xpath_finds_intro(chap1: NodeMap) -> None:
    intro = chap1.element_by_xpath(normalize_xpath("/body/div"))
    assert intro.get("id") == "intro"


def test_element_by_xpath_matches_local_name_despite_namespace(chap1: NodeMap) -> None:
    # XHTML lives in a default namespace; xpointer paths are namespace-free.
    body = chap1.element_by_xpath(normalize_xpath("/body"))
    assert body.tag.endswith("}body") or body.tag == "body"


def test_element_by_xpath_disambiguates_same_tag_siblings(chap1: NodeMap) -> None:
    first = chap1.element_by_xpath(normalize_xpath("/body/div/div/p[1]"))
    second = chap1.element_by_xpath(normalize_xpath("/body/div/div/p[2]"))
    assert chap1.chunks(first)[0].text == "alpha"
    assert chap1.chunks(second)[0].text == "beta"


def test_element_by_xpath_missing_segment_raises(chap1: NodeMap) -> None:
    with pytest.raises(ResolutionError):
        chap1.element_by_xpath(normalize_xpath("/body/div/p[99]"))


def test_xpath_for_element_round_trip(chap1: NodeMap) -> None:
    for path in ("/body/div[1]", "/body/div[1]/p[1]", "/body/div[1]/div[1]/p[2]"):
        elem = chap1.element_by_xpath(normalize_xpath(path))
        assert chap1.xpath_for_element(elem) == path


def test_xpath_for_body_is_plain(chap1: NodeMap) -> None:
    body = chap1.element_by_xpath(normalize_xpath("/body"))
    assert chap1.xpath_for_element(body) == "/body"


# --------------------------------------------------------------------------------------
# Element addressing: CFI steps
# --------------------------------------------------------------------------------------


def test_body_is_cfi_step_four(chap1: NodeMap) -> None:
    body = chap1.element_by_xpath(normalize_xpath("/body"))
    assert chap1.cfi_steps_for_element(body) == (Step(index=4, assertion=None),)


def test_cfi_steps_include_id_assertion(chap1: NodeMap) -> None:
    intro = chap1.element_by_xpath(normalize_xpath("/body/div"))
    assert chap1.cfi_steps_for_element(intro) == (
        Step(index=4, assertion=None),
        Step(index=2, assertion="intro"),
    )


def test_cfi_steps_round_trip(chap1: NodeMap) -> None:
    for path in ("/body/div[1]", "/body/div[1]/p[3]", "/body/div[1]/div[1]/p[2]"):
        elem = chap1.element_by_xpath(normalize_xpath(path))
        steps = chap1.cfi_steps_for_element(elem)
        assert chap1.element_by_cfi_steps(steps) is elem


def test_element_by_cfi_steps_self_repair(chap1: NodeMap) -> None:
    intro = chap1.element_by_xpath(normalize_xpath("/body/div"))
    # Wrong (out-of-range) even index but a correct id assertion self-repairs.
    steps = (Step(index=4, assertion=None), Step(index=98, assertion="intro"))
    assert chap1.element_by_cfi_steps(steps) is intro


def test_element_by_cfi_steps_rejects_odd_step(chap1: NodeMap) -> None:
    with pytest.raises(ResolutionError, match="odd"):
        chap1.element_by_cfi_steps((Step(index=3, assertion=None),))


def test_element_by_cfi_steps_unresolvable_raises(chap1: NodeMap) -> None:
    with pytest.raises(ResolutionError):
        chap1.element_by_cfi_steps((Step(index=4, assertion=None), Step(index=200, assertion=None)))


def test_document_element_gets_no_step(chap1: NodeMap) -> None:
    body = chap1.element_by_xpath(normalize_xpath("/body"))
    root = body.getparent()
    assert root is not None
    assert chap1.cfi_steps_for_element(root) == ()


# --------------------------------------------------------------------------------------
# Chunk model
# --------------------------------------------------------------------------------------


def test_comment_does_not_split_chunk(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[2]"))
    chunks = chap1.chunks(p)
    assert len(chunks) == 1
    assert chunks[0].odd_index == 1
    assert chunks[0].text == "Beforeafter"


def test_mixed_inline_gap_indices_and_text(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    chunks = chap1.chunks(p)
    assert [(c.odd_index, c.text) for c in chunks] == [
        (1, "Hello "),
        (3, " new "),
        (5, "."),
    ]


def test_mixed_inline_anchor_nodes(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    chunks = chap1.chunks(p)
    # gap_0 anchors at the element's own leading text; later gaps at a child's tail.
    assert chunks[0].anchor == (p, "text")
    anchor = chunks[1].anchor
    assert anchor is not None
    node, attr = anchor
    assert attr == "tail"
    assert node.tail == " new "


def test_empty_paragraph_has_one_empty_chunk(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[4]"))
    chunks = chap1.chunks(p)
    assert len(chunks) == 1
    assert chunks[0].text == ""
    assert chunks[0].anchor is None


def test_chunk_count_is_element_children_plus_one(chap1: NodeMap) -> None:
    # <p>Hello <i/> new <b/>.</p> has two element children -> three gaps.
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    assert len(chap1.chunks(p)) == 3


def test_comment_split_text_is_one_chunk_with_leading_anchor(chap1: NodeMap) -> None:
    # <p>Before<!--c-->after</p>: one chunk whose text spans the comment split, anchored
    # at the element's leading text.
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[2]"))
    chunks = chap1.chunks(p)
    assert chunks[0].text == "Beforeafter"
    assert chunks[0].anchor == (p, "text")


# --------------------------------------------------------------------------------------
# Text position <-> CFI, including UTF-16 vs code-point divergence
# --------------------------------------------------------------------------------------


def test_text_position_to_cfi_basic(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    assert chap1.text_position_to_cfi(p, 1, 0) == (1, 0)
    assert chap1.text_position_to_cfi(p, 2, 2) == (3, 2)
    assert chap1.text_position_to_cfi(p, 3, 1) == (5, 1)


def test_text_position_round_trip(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    for node_index, text in ((1, "Hello "), (2, " new "), (3, ".")):
        for offset in range(len(text) + 1):
            odd, utf16 = _require_text_pos(chap1, p, node_index, offset)
            assert chap1.cfi_to_text_position(p, odd, utf16) == (node_index, offset)


def test_emoji_utf16_diverges_from_code_points(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[3]"))
    # "Emoji 😀 tail": code-point offset 8 sits after the emoji, but the emoji is two
    # UTF-16 units, so the CFI offset is 9, not 8.
    odd, utf16 = _require_text_pos(chap1, p, 1, 8)
    assert (odd, utf16) == (1, 9)
    assert chap1.cfi_to_text_position(p, 1, 9) == (1, 8)


def test_text_position_index_too_large(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[3]"))
    with pytest.raises(ResolutionError, match="countable text chunk"):
        chap1.text_position_to_cfi(p, 5, 0)


def test_text_position_offset_too_large(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[3]"))
    with pytest.raises(ResolutionError, match="exceeds"):
        chap1.text_position_to_cfi(p, 1, 999)


def test_cfi_to_text_position_rejects_even_index(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    with pytest.raises(ResolutionError, match="odd"):
        chap1.cfi_to_text_position(p, 2, 0)


def test_cfi_to_text_position_out_of_range(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    with pytest.raises(ResolutionError, match="out of range"):
        chap1.cfi_to_text_position(p, 99, 0)


def test_cfi_to_text_position_surrogate_middle(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[3]"))
    with pytest.raises(ResolutionError, match="surrogate"):
        chap1.cfi_to_text_position(p, 1, 7)


def test_cfi_to_text_position_empty_chunk_offset_zero(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[4]"))
    assert chap1.cfi_to_text_position(p, 1, 0) == (1, 0)


def test_cfi_to_text_position_empty_chunk_nonzero_offset(chap1: NodeMap) -> None:
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[4]"))
    with pytest.raises(ResolutionError, match="empty"):
        chap1.cfi_to_text_position(p, 1, 1)


# --------------------------------------------------------------------------------------
# UTF-16 helpers
# --------------------------------------------------------------------------------------


def test_cp_to_utf16_with_emoji() -> None:
    text = f"a{EMOJI}b"
    assert cp_to_utf16(text, 0) == 0
    assert cp_to_utf16(text, 1) == 1
    assert cp_to_utf16(text, 2) == 3  # emoji spans two UTF-16 units
    assert cp_to_utf16(text, 3) == 4


def test_utf16_to_cp_with_emoji() -> None:
    text = f"a{EMOJI}b"
    assert utf16_to_cp(text, 0) == 0
    assert utf16_to_cp(text, 1) == 1
    assert utf16_to_cp(text, 3) == 2
    assert utf16_to_cp(text, 4) == 3


def test_utf16_to_cp_surrogate_middle_raises() -> None:
    with pytest.raises(ResolutionError, match="surrogate"):
        utf16_to_cp(f"a{EMOJI}b", 2)


def test_utf16_to_cp_beyond_end_raises() -> None:
    with pytest.raises(ResolutionError, match="exceeds"):
        utf16_to_cp("abc", 4)


def test_utf16_helpers_round_trip() -> None:
    text = f"pre {EMOJI} mid {EMOJI} post"
    for cp in range(len(text) + 1):
        assert utf16_to_cp(text, cp_to_utf16(text, cp)) == cp


# --------------------------------------------------------------------------------------
# extract_text
# --------------------------------------------------------------------------------------


@pytest.fixture
def two_para() -> NodeMap:
    data = build_epub({"a.xhtml": xhtml_doc("A", "<p>Hello world</p><p>Second para</p>")})
    return EpubMap.from_bytes(data).doc(1)


def _paras(nm: NodeMap):
    p1 = nm.element_by_xpath(normalize_xpath("/body/p[1]"))
    p2 = nm.element_by_xpath(normalize_xpath("/body/p[2]"))
    return p1, p2


def test_extract_within_one_element(two_para: NodeMap) -> None:
    p1, _ = _paras(two_para)
    assert two_para.extract_text((p1, 1, 0), (p1, 1, 5)) == "Hello"


def test_extract_across_elements(two_para: NodeMap) -> None:
    p1, p2 = _paras(two_para)
    assert two_para.extract_text((p1, 1, 6), (p2, 1, 6)) == "worldSecond"


def test_extract_none_start(two_para: NodeMap) -> None:
    p1, _ = _paras(two_para)
    assert two_para.extract_text(None, (p1, 1, 5)) == "Hello"


def test_extract_none_end(two_para: NodeMap) -> None:
    _, p2 = _paras(two_para)
    assert two_para.extract_text((p2, 1, 7), None) == "para"


def test_extract_none_both_is_whole_body(two_para: NodeMap) -> None:
    assert two_para.extract_text(None, None) == "Hello worldSecond para"


def test_extract_mid_text_offsets(two_para: NodeMap) -> None:
    p1, _ = _paras(two_para)
    assert two_para.extract_text((p1, 1, 2), (p1, 1, 8)) == "llo wo"


def test_extract_across_inline_elements(chap1: NodeMap) -> None:
    # Within <p>Hello <i>brave</i> new <b>world</b>.</p> the body text stream reads
    # "Hello brave new world." with the inline children interleaved.
    p = chap1.element_by_xpath(normalize_xpath("/body/div/p[1]"))
    # from gap_0 offset 0 to gap after <b> (odd 5) offset 0 -> everything up to the "."
    assert chap1.extract_text((p, 1, 0), (p, 5, 0)) == "Hello brave new world"


# --------------------------------------------------------------------------------------
# doc() caching and recovery
# --------------------------------------------------------------------------------------


def test_doc_is_cached(simple_book: bytes) -> None:
    book = EpubMap.from_bytes(simple_book)
    assert book.doc(1) is book.doc(1)


def test_doc_out_of_range_raises(simple_book: bytes) -> None:
    with pytest.raises(ResolutionError):
        EpubMap.from_bytes(simple_book).doc(0)


def test_doc_recovers_from_malformed_xhtml() -> None:
    # Unclosed <b> tag: strict XML fails, recovery parser succeeds.
    data = build_epub({"a.xhtml": xhtml_doc("A", "<p>broken <b>bold</p>")})
    nm = EpubMap.from_bytes(data).doc(1)
    assert "bold" in nm.extract_text(None, None)


# --------------------------------------------------------------------------------------
# crengine whitespace fidelity: countable chunks, collapse mapping, <pre>, best-effort
# --------------------------------------------------------------------------------------


def _doc_from_body(body: str) -> NodeMap:
    return EpubMap.from_bytes(build_epub({"a.xhtml": xhtml_doc("A", body)})).doc(1)


def test_leading_whitespace_only_chunk_is_not_counted() -> None:
    # gap_0 is "\n" (whitespace only -> dropped by crengine); the countable text is the
    # tail after <span>, which is text()[1] and lives at CFI odd index 3.
    nm = _doc_from_body("<p>\n<span>x</span>\n Keep reading</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    chunks = nm.chunks(p)
    assert [(c.odd_index, c.text) for c in chunks] == [
        (1, "\n"),
        (3, "\n Keep reading"),
    ]
    # text()[1] maps to odd_index 3 (the leading ws-only chunk is invisible).
    odd, _ = _require_text_pos(nm, p, 1, 0)
    assert odd == 3


def test_single_space_chunk_is_not_counted() -> None:
    # [(1, " "), (3, "long text")] -> text()[1] is the second chunk.
    nm = _doc_from_body("<p> <span>y</span> long text</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    assert [(c.odd_index, c.text) for c in nm.chunks(p)] == [(1, " "), (3, " long text")]
    odd, _ = _require_text_pos(nm, p, 1, 0)
    assert odd == 3


def test_medial_whitespace_only_chunks_are_counted() -> None:
    # Corpus-verified (DDD Distilled): crengine keeps whitespace-only text nodes BETWEEN
    # element children — only the leading one is dropped. Here gaps are:
    #   1: "intro " (countable), 3: "\n " (medial ws-only, KEPT), 5: " tail" (countable)
    # so crengine's text()[2] is the "\n " node and text()[3] is " tail" at odd index 5.
    nm = _doc_from_body("<p>intro <a>x</a>\n <em>y</em> tail</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    assert nm.text_position_to_cfi(p, 1, 0) == (1, 0)
    assert nm.text_position_to_cfi(p, 2, 0) == (3, 0)
    assert nm.text_position_to_cfi(p, 3, 1) == (5, 1)
    # Reverse counting matches: odd index 5 is the 3rd crengine text node.
    assert nm.cfi_to_text_position(p, 5, 1) == (3, 1)
    assert nm.cfi_to_text_position(p, 3, 0) == (2, 0)


def test_leading_and_medial_whitespace_rule_interaction() -> None:
    # Leading ws-only gap dropped, medial ws-only gap kept: text()[1] is the tail after
    # the first element, text()[2] the medial "\n", text()[3] the final tail.
    nm = _doc_from_body("<p>\n<span>a</span>mid<span>b</span>\n<span>c</span>end</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    assert nm.text_position_to_cfi(p, 1, 0) == (3, 0)  # "mid"
    assert nm.text_position_to_cfi(p, 2, 0) == (5, 0)  # medial "\n"
    assert nm.text_position_to_cfi(p, 3, 0) == (7, 0)  # "end"


def test_double_space_offset_maps_collapsed_to_raw_utf16() -> None:
    # Raw "a  b c" (double space after 'a'); collapsed is "a b c". A KOReader offset is
    # in collapsed space: collapsed index 2 is 'b', whose raw code-point index is 3, so
    # the CFI UTF-16 offset must be 3 (no astral chars -> utf16 == code points).
    nm = _doc_from_body("<p>a  b c</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    assert nm.chunks(p)[0].text == "a  b c"
    assert nm.text_position_to_cfi(p, 1, 0) == (1, 0)
    assert nm.text_position_to_cfi(p, 1, 2) == (1, 3)  # collapsed 'b' -> raw index 3
    assert nm.text_position_to_cfi(p, 1, 4) == (1, 5)  # collapsed 'c' -> raw index 5
    # Reverse: raw UTF-16 offset 3 -> collapsed code-point offset 2.
    assert nm.cfi_to_text_position(p, 1, 3) == (1, 2)
    assert nm.cfi_to_text_position(p, 1, 5) == (1, 4)


def test_collapsed_offset_round_trip_over_double_spaces() -> None:
    nm = _doc_from_body("<p>one  two   three</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    from xpoint_cfi.crengine_text import collapse

    collapsed = collapse(nm.chunks(p)[0].text)
    for offset in range(len(collapsed) + 1):
        odd, utf16 = _require_text_pos(nm, p, 1, offset)
        assert nm.cfi_to_text_position(p, odd, utf16) == (1, offset)


def test_pre_element_uses_identity_mapping() -> None:
    # Inside <pre> the double space is NOT collapsed, so the offset is a raw offset.
    nm = _doc_from_body("<pre>a  b c</pre>")
    pre = nm.element_by_xpath(normalize_xpath("/body/pre"))
    # collapsed index would move 'b' to 2, but in <pre> offset 3 is 'b' directly.
    assert nm.text_position_to_cfi(pre, 1, 3) == (1, 3)
    assert nm.cfi_to_text_position(pre, 1, 3) == (1, 3)
    # A full raw sweep round-trips as identity (no collapse).
    text = nm.chunks(pre)[0].text
    for offset in range(len(text) + 1):
        odd, utf16 = _require_text_pos(nm, pre, 1, offset)
        assert nm.cfi_to_text_position(pre, odd, utf16) == (1, offset)


def test_pre_detected_through_ancestor() -> None:
    nm = _doc_from_body("<pre><code>a  b</code></pre>")
    code = nm.element_by_xpath(normalize_xpath("/body/pre/code"))
    # 'b' is at raw index 3; identity mapping means the CFI offset is also 3.
    assert nm.text_position_to_cfi(code, 1, 3) == (1, 3)


def test_cfi_to_text_position_whitespace_only_chunk_offset_zero() -> None:
    # gap_0 is a lone " " (not countable); offset 0 is a best-effort element edge.
    nm = _doc_from_body("<p> <span>y</span>tail</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    assert nm.cfi_to_text_position(p, 1, 0) == (1, 0)


def test_cfi_to_text_position_whitespace_only_chunk_nonzero_offset_raises() -> None:
    nm = _doc_from_body("<p> <span>y</span>tail</p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    with pytest.raises(ResolutionError, match="whitespace-only"):
        nm.cfi_to_text_position(p, 1, 1)


def test_textless_element_default_position_is_element_boundary() -> None:
    # The default text()[1].0 on an element crengine keeps no text node for (an <img/>
    # child only, or leading whitespace only) has no text location: None, not an error.
    nm = _doc_from_body("<p>text</p><p><img/></p><p> </p>")
    p1 = nm.element_by_xpath(normalize_xpath("/body/p[1]"))
    p2 = nm.element_by_xpath(normalize_xpath("/body/p[2]"))
    p3 = nm.element_by_xpath(normalize_xpath("/body/p[3]"))
    assert nm.text_position_to_cfi(p1, 1, 0) == (1, 0)
    assert nm.text_position_to_cfi(p2, 1, 0) is None
    assert nm.text_position_to_cfi(p3, 1, 0) is None


def test_textless_element_nondefault_position_raises() -> None:
    nm = _doc_from_body("<p><img/></p>")
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    with pytest.raises(ResolutionError, match="countable"):
        nm.text_position_to_cfi(p, 1, 5)
    with pytest.raises(ResolutionError, match="countable"):
        nm.text_position_to_cfi(p, 2, 0)
