"""Resolve xpoint bounds onto document text and extract the text between them.

Both the CFI self-check (:mod:`xpoint_cfi.verify`) and the Readium locator layer
(:mod:`xpoint_cfi.locator`) need the same two operations: turn an :class:`XPoint` into a
concrete ``(element, odd_index, utf16_offset)`` bound inside its spine item, and read out
the document text lying between two such bounds. They live here so both layers extract
byte-identical text — a locator's quote and a CFI self-check must never disagree about
what a range says.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from lxml import etree

from .epub_map import cp_to_utf16
from .xpoint import normalize_xpath

if TYPE_CHECKING:
    from .epub_map import EpubMap, NodeMap
    from .xpoint import XPoint

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]

__all__ = ["extract_between", "resolve_bound"]


def resolve_bound(node: NodeMap, xpoint: XPoint, *, is_end: bool) -> tuple[_Element, int, int]:
    """Resolve an :class:`XPoint` to an ``(element, odd_index, utf16_offset)`` bound.

    A text-position xpoint resolves directly. An element-boundary xpoint resolves to the
    element's first gap (offset 0) as a start bound, or the end of its last gap as an end
    bound, so that an element boundary encloses the element's whole text.

    Raises:
        ResolutionError: if the xpath or the text position does not resolve.
    """
    elem = node.element_by_xpath(normalize_xpath(xpoint.xpath))
    if xpoint.has_text_position:
        location = node.text_position_to_cfi(elem, xpoint.text_node_index, xpoint.char_offset)
        if location is not None:
            odd_index, utf16_offset = location
            return (elem, odd_index, utf16_offset)
    chunks = node.chunks(elem)
    if is_end:
        last = chunks[-1]
        return (elem, last.odd_index, cp_to_utf16(last.text, len(last.text)))
    return (elem, chunks[0].odd_index, 0)


def extract_between(book: EpubMap, start: XPoint, end: XPoint) -> str:
    """Return the document text between two xpoints, spanning spine items when needed.

    A cross-resource range contributes the tail of the start document, the full text of
    every intermediate document, and the head of the end document.

    Raises:
        ResolutionError: if either end does not resolve against the book.
    """
    start_index = start.doc_fragment_index
    end_index = end.doc_fragment_index
    start_node = book.doc(start_index)
    end_node = book.doc(end_index)
    start_bound = resolve_bound(start_node, start, is_end=False)
    end_bound = resolve_bound(end_node, end, is_end=True)

    if start_index == end_index:
        return start_node.extract_text(start_bound, end_bound)

    parts = [start_node.extract_text(start_bound, None)]
    for index in range(start_index + 1, end_index):
        parts.append(book.doc(index).extract_text(None, None))
    parts.append(end_node.extract_text(None, end_bound))
    return "".join(parts)
