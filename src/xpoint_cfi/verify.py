"""Verification: does a converted range denote the text the caller expected?

Conversions between two crengine-vs-CFI coordinate systems can drift when a document's
structure is unusual. Callers (e.g. crossbill) store the highlighted text alongside the
xpointer, so :func:`verify_range` re-extracts the document text a CFI range denotes and
compares it — after whitespace normalization — against that stored text. A mismatch is a
signal that the conversion (or the stored range) is untrustworthy, not a hard error.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from lxml import etree

from .cfi import CfiRange, parse_cfi
from .convert import _rejoin, cfi_to_xpoint  # pyright: ignore[reportPrivateUsage]
from .epub_map import cp_to_utf16
from .exceptions import ResolutionError
from .xpoint import normalize_xpath

if TYPE_CHECKING:
    from .epub_map import EpubMap, NodeMap
    from .xpoint import XPoint

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]

__all__ = ["VerificationResult", "normalize_whitespace", "verify_range"]


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of :func:`verify_range`.

    Attributes:
        ok: Whether the extracted text matches the expected text after whitespace
            normalization.
        extracted_text: The raw text the range denotes, before normalization.
    """

    ok: bool
    extracted_text: str


def normalize_whitespace(s: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return " ".join(s.split())


def verify_range(book: EpubMap, rng: CfiRange | str, expected_text: str) -> VerificationResult:
    """Extract the text a CFI range denotes and compare it to ``expected_text``.

    ``rng`` may be a :class:`CfiRange` or a range-CFI string. Extraction spans spine
    items when the range crosses them: the tail of the start document, the full text of
    any intermediate documents, and the head of the end document.

    Raises:
        ResolutionError: if a range-CFI string parses to a non-range CFI, or if either
            end does not resolve against the book.
    """
    if isinstance(rng, str):
        parsed = parse_cfi(rng)
        if not isinstance(parsed, CfiRange):
            raise ResolutionError(rng, "expected a range CFI (epubcfi(parent,start,end))")
        rng = parsed

    start_xp = cfi_to_xpoint(book, _rejoin(rng.parent, rng.start))
    end_xp = cfi_to_xpoint(book, _rejoin(rng.parent, rng.end))

    extracted = _extract_between(book, start_xp, end_xp)
    ok = normalize_whitespace(extracted) == normalize_whitespace(expected_text)
    return VerificationResult(ok=ok, extracted_text=extracted)


def _extract_between(book: EpubMap, start_xp: XPoint, end_xp: XPoint) -> str:
    start_index = start_xp.doc_fragment_index
    end_index = end_xp.doc_fragment_index
    start_node = book.doc(start_index)
    end_node = book.doc(end_index)
    start_bound = _bound(start_node, start_xp, is_end=False)
    end_bound = _bound(end_node, end_xp, is_end=True)

    if start_index == end_index:
        return start_node.extract_text(start_bound, end_bound)

    parts = [start_node.extract_text(start_bound, None)]
    for index in range(start_index + 1, end_index):
        parts.append(book.doc(index).extract_text(None, None))
    parts.append(end_node.extract_text(None, end_bound))
    return "".join(parts)


def _bound(node: NodeMap, xpoint: XPoint, *, is_end: bool) -> tuple[_Element, int, int]:
    """Resolve an :class:`XPoint` to an ``(element, odd_index, utf16_offset)`` bound.

    A text-position xpoint resolves directly. An element-boundary xpoint resolves to the
    element's first gap (offset 0) as a start bound, or the end of its last gap as an end
    bound, so that an element boundary encloses the element's whole text.
    """
    elem = node.element_by_xpath(normalize_xpath(xpoint.xpath))
    if xpoint.has_text_position:
        odd_index, utf16_offset = node.text_position_to_cfi(
            elem, xpoint.text_node_index, xpoint.char_offset
        )
        return (elem, odd_index, utf16_offset)
    chunks = node.chunks(elem)
    if is_end:
        last = chunks[-1]
        return (elem, last.odd_index, cp_to_utf16(last.text, len(last.text)))
    return (elem, chunks[0].odd_index, 0)
