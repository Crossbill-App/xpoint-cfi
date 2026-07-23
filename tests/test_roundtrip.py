"""Exhaustive xpoint <-> CFI <-> xpoint round-trip sweep over the fixture book.

For every spine item, every element under ``<body>``, every crengine-countable text
chunk, and a sample of **collapsed** code-point offsets (the space xpointers actually
index), ``xpoint -> cfi -> xpoint`` must be the identity modulo ``[1]`` xpath
normalization. Every element also round-trips as a bare element boundary. Finally, within
one spine item, ordering the generated positions by :meth:`Cfi.sort_key` must agree with
their true document order (their absolute offset in the body text).
"""

from __future__ import annotations

import pytest
from conftest import build_epub, xhtml_doc

from xpoint_cfi import (
    Cfi,
    EpubMap,
    NodeMap,
    XPoint,
    cfi_to_xpoint,
    normalize_xpath,
    xpoint_to_cfi,
)
from xpoint_cfi.crengine_text import collapse, is_countable
from xpoint_cfi.epub_map import _Element  # pyright: ignore[reportPrivateUsage]

EMOJI = "\U0001f600"


def _require_text_pos(nm: NodeMap, elem: _Element, node_index: int, offset: int) -> tuple[int, int]:
    loc = nm.text_position_to_cfi(elem, node_index, offset)
    assert loc is not None
    return loc


@pytest.fixture
def book(simple_book: bytes) -> EpubMap:
    return EpubMap.from_bytes(simple_book)


@pytest.fixture
def whitespace_book() -> EpubMap:
    # Multi-space runs, a leading whitespace-only chunk, tabs/newlines, and an inline
    # element whose surrounding text has collapsible runs — so the sweep exercises the
    # collapsed<->raw offset mapping, not just identity cases.
    body = "<p>one  two   three</p><p>\n <span>mid</span>  tail  end</p><p>a\t\tb\n\nc</p>"
    return EpubMap.from_bytes(build_epub({"w.xhtml": xhtml_doc("W", body)}))


def _elements_under_body(nm: NodeMap) -> list[_Element]:
    body = nm.element_by_xpath(normalize_xpath("/body"))
    return [el for el in body.iter() if not callable(el.tag)]


def _countable_chunks(nm: NodeMap, elem: _Element):
    return [c for c in nm.chunks(elem) if is_countable(c.text)]


def _sample_offsets(length: int) -> list[int]:
    # First, last, and a few interior offsets (kept small to bound runtime).
    candidates = {0, length, 1, length - 1, length // 2}
    return sorted(o for o in candidates if 0 <= o <= length)


def _assert_same_position(a: XPoint, b: XPoint) -> None:
    assert normalize_xpath(a.xpath) == normalize_xpath(b.xpath)
    assert a.doc_fragment_index == b.doc_fragment_index
    assert a.text_node_index == b.text_node_index
    assert a.char_offset == b.char_offset
    assert a.has_text_position == b.has_text_position


def _sweep_text_positions(book: EpubMap) -> int:
    checked = 0
    for spine_index in range(1, book.spine_count + 1):
        nm = book.doc(spine_index)
        for elem in _elements_under_body(nm):
            xpath = nm.xpath_for_element(elem)
            for node_index, chunk in enumerate(_countable_chunks(nm, elem), start=1):
                collapsed = collapse(chunk.text)
                for offset in _sample_offsets(len(collapsed)):
                    original = XPoint(
                        doc_fragment_index=spine_index,
                        xpath=xpath,
                        text_node_index=node_index,
                        char_offset=offset,
                        has_text_position=True,
                    )
                    back = cfi_to_xpoint(book, xpoint_to_cfi(book, original))
                    _assert_same_position(original, back)
                    checked += 1
    return checked


def test_text_position_round_trip_is_identity(book: EpubMap) -> None:
    assert _sweep_text_positions(book) > 0


def test_text_position_round_trip_over_whitespace_runs(whitespace_book: EpubMap) -> None:
    assert _sweep_text_positions(whitespace_book) > 0


def test_element_boundary_round_trip_is_identity(book: EpubMap) -> None:
    checked = 0
    for spine_index in range(1, book.spine_count + 1):
        nm = book.doc(spine_index)
        for elem in _elements_under_body(nm):
            xpath = nm.xpath_for_element(elem)
            original = XPoint(
                doc_fragment_index=spine_index,
                xpath=xpath,
                text_node_index=1,
                char_offset=0,
                has_text_position=False,
            )
            back = cfi_to_xpoint(book, xpoint_to_cfi(book, original))
            _assert_same_position(original, back)
            checked += 1
    assert checked > 0


def test_cfi_sort_key_agrees_with_document_order(whitespace_book: EpubMap) -> None:
    book = whitespace_book
    for spine_index in range(1, book.spine_count + 1):
        nm = book.doc(spine_index)
        positions: list[tuple[Cfi, int]] = []
        for elem in _elements_under_body(nm):
            xpath = nm.xpath_for_element(elem)
            for node_index, chunk in enumerate(_countable_chunks(nm, elem), start=1):
                collapsed = collapse(chunk.text)
                for offset in _sample_offsets(len(collapsed)):
                    xp = XPoint(spine_index, xpath, node_index, offset, has_text_position=True)
                    cfi = xpoint_to_cfi(book, xp)
                    odd, utf16 = _require_text_pos(nm, elem, node_index, offset)
                    absolute = len(nm.extract_text(None, (elem, odd, utf16)))
                    positions.append((cfi, absolute))
        by_key = sorted(positions, key=lambda pair: pair[0].sort_key())
        offsets = [absolute for _, absolute in by_key]
        assert offsets == sorted(offsets)


def test_round_trip_with_astral_offsets() -> None:
    # A dedicated document whose text is dense with surrogate-pair characters.
    body = f"<p>{EMOJI}a{EMOJI}b{EMOJI}</p>"
    book = EpubMap.from_bytes(build_epub({"a.xhtml": xhtml_doc("A", body)}))
    nm = book.doc(1)
    p = nm.element_by_xpath(normalize_xpath("/body/p"))
    text = nm.chunks(p)[0].text
    for offset in range(len(text) + 1):
        original = XPoint(1, "/body/p", 1, offset, has_text_position=True)
        back = cfi_to_xpoint(book, xpoint_to_cfi(book, original))
        _assert_same_position(original, back)
