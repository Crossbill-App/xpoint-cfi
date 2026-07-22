"""The converters: xpointer <-> CFI, using the document layer for DOM coordinates.

These are free functions rather than :class:`~xpoint_cfi.epub_map.EpubMap` methods so
that the document layer stays free of any dependency on the value-object layers. Each
converter takes the ``book`` (an :class:`EpubMap`) explicitly.

Two coordinate translations happen here, both delegated to a per-spine-item
:class:`~xpoint_cfi.epub_map.NodeMap`:

* the *package* level — spine index <-> the ``/6/2[idref]`` itemref steps, and
* the *document* level — an element xpath and text position <-> CFI element/text steps.

A full converted :class:`Cfi` therefore always has exactly two local paths: the package
path and, after the ``!`` indirection, the document path.

Ranges are produced by converting both ends to a full :class:`Cfi` and factoring their
longest common prefix into the :class:`CfiRange` parent, so that
``epubcfi(parent,start,end)`` reconstructs each end by string concatenation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .cfi import Cfi, CfiRange, CharOffset, LocalPath, Step, parse_cfi
from .exceptions import ResolutionError
from .xpoint import XPoint, XPointRange, normalize_xpath

if TYPE_CHECKING:
    from .epub_map import EpubMap

__all__ = [
    "cfi_range_to_xpoint_range",
    "cfi_to_xpoint",
    "cfi_to_xpoint_range_strings",
    "cfi_to_xpoint_string",
    "xpoint_range_to_cfi",
    "xpoint_range_to_cfi_string",
    "xpoint_to_cfi",
    "xpoint_to_cfi_string",
]


# --------------------------------------------------------------------------------------
# Single-point conversion
# --------------------------------------------------------------------------------------


def xpoint_to_cfi(book: EpubMap, xpoint: XPoint) -> Cfi:
    """Convert a single :class:`XPoint` to a full :class:`Cfi`.

    The result has a package local path ``(/spine_element_step, /itemref)`` and, after
    the ``!`` indirection, a document local path of element steps optionally terminated
    by an odd text step and a UTF-16 character offset.

    Raises:
        ResolutionError: if the spine index, element xpath, or text position does not
            resolve against the book.
    """
    package = LocalPath(
        steps=(Step(book.spine_element_step, None), book.spine_step(xpoint.doc_fragment_index)),
        offset=None,
    )
    node = book.doc(xpoint.doc_fragment_index)
    elem = node.element_by_xpath(normalize_xpath(xpoint.xpath))
    element_steps = node.cfi_steps_for_element(elem)
    if xpoint.has_text_position:
        odd_index, utf16_offset = node.text_position_to_cfi(
            elem, xpoint.text_node_index, xpoint.char_offset
        )
        document = LocalPath(
            steps=(*element_steps, Step(odd_index, None)),
            offset=CharOffset(utf16_offset, None),
        )
    else:
        document = LocalPath(steps=element_steps, offset=None)
    return Cfi(paths=(package, document))


def cfi_to_xpoint(book: EpubMap, cfi: Cfi) -> XPoint:
    """Convert a full two-local-path :class:`Cfi` back to an :class:`XPoint`.

    Raises:
        ResolutionError: if the CFI shape is unsupported (only a package path, more than
            one indirection, a malformed package path, or a character offset on an
            element step) or if any step does not resolve against the book.
    """
    if len(cfi.paths) == 1:
        raise ResolutionError(
            cfi.to_string(), "CFI addresses only the package level (no '!' document path)"
        )
    if len(cfi.paths) != 2:
        raise ResolutionError(
            cfi.to_string(), "nested indirection not supported (expected exactly one '!')"
        )

    package, document = cfi.paths
    if len(package.steps) < 2:
        raise ResolutionError(
            cfi.to_string(), "package path must have a spine element step and an itemref step"
        )
    if len(package.steps) > 2 or package.offset is not None:
        raise ResolutionError(cfi.to_string(), "unexpected extra steps in the package path")
    if package.steps[0].index != book.spine_element_step:
        raise ResolutionError(
            cfi.to_string(),
            f"package path does not start with the spine element step /{book.spine_element_step}",
        )
    spine_index = book.spine_index_for_step(package.steps[1])
    node = book.doc(spine_index)

    if not document.steps:
        raise ResolutionError(cfi.to_string(), "document path has no element steps")

    final = document.steps[-1]
    if final.index % 2 == 1:
        element_steps = document.steps[:-1]
        elem = node.element_by_cfi_steps(element_steps)
        utf16_offset = document.offset.value if document.offset is not None else 0
        text_node_index, char_offset = node.cfi_to_text_position(elem, final.index, utf16_offset)
        return XPoint(
            doc_fragment_index=spine_index,
            xpath=node.xpath_for_element(elem),
            text_node_index=text_node_index,
            char_offset=char_offset,
            has_text_position=True,
        )

    if document.offset is not None:
        raise ResolutionError(
            cfi.to_string(),
            "a character offset on an element step is not supported (offset without a text step)",
        )
    elem = node.element_by_cfi_steps(document.steps)
    return XPoint(
        doc_fragment_index=spine_index,
        xpath=node.xpath_for_element(elem),
        text_node_index=1,
        char_offset=0,
        has_text_position=False,
    )


# --------------------------------------------------------------------------------------
# Range conversion
# --------------------------------------------------------------------------------------


def xpoint_range_to_cfi(book: EpubMap, rng: XPointRange) -> CfiRange:
    """Convert an :class:`XPointRange` to a :class:`CfiRange`.

    Both ends are converted to a full :class:`Cfi`, then their longest common prefix is
    factored into the parent. The parent is always non-empty: at minimum it holds the
    shared spine element step. ``start`` and ``end`` carry the divergent remainder so
    that ``_rejoin(parent, start)`` reproduces the original start CFI exactly.
    """
    start_cfi = xpoint_to_cfi(book, rng.start)
    end_cfi = xpoint_to_cfi(book, rng.end)
    return _factor_range(start_cfi, end_cfi)


def cfi_range_to_xpoint_range(book: EpubMap, rng: CfiRange) -> XPointRange:
    """Convert a :class:`CfiRange` back to an :class:`XPointRange`.

    The parent is rejoined with each subpath to recover the two full CFIs, which are
    then converted independently.
    """
    start = cfi_to_xpoint(book, _rejoin(rng.parent, rng.start))
    end = cfi_to_xpoint(book, _rejoin(rng.parent, rng.end))
    return XPointRange(start=start, end=end)


def _rejoin(parent: Cfi, sub: Cfi) -> Cfi:
    """Reconstruct a full CFI from a range ``parent`` and one relative subpath.

    The parent's final local path is concatenated (steps then the subpath's offset) with
    the subpath's first local path; the subpath's remaining local paths follow as further
    indirection levels. This mirrors how EPUB CFI ranges reduce to a full path by string
    concatenation of the parent and each end.
    """
    merged = LocalPath(
        steps=parent.paths[-1].steps + sub.paths[0].steps,
        offset=sub.paths[0].offset,
    )
    return Cfi(paths=(*parent.paths[:-1], merged, *sub.paths[1:]))


def _factor_range(start_cfi: Cfi, end_cfi: Cfi) -> CfiRange:
    """Factor two full CFIs into ``epubcfi(parent,start,end)`` sharing the common prefix."""
    a = start_cfi.paths
    b = end_cfi.paths

    shared = 0
    while shared < min(len(a), len(b)) and a[shared] == b[shared]:
        shared += 1

    # When the two ends are identical (a zero-length range), split within the last local
    # path so the terminal step (or offset) stays in both start and end.
    div = shared - 1 if shared == len(a) == len(b) else shared

    common_full = a[:div]
    a_lp = a[div]
    b_lp = b[div]

    k = 0
    for step_a, step_b in zip(a_lp.steps, b_lp.steps, strict=False):
        if step_a == step_b:
            k += 1
        else:
            break

    a_following = a[div + 1 :]
    b_following = b[div + 1 :]

    def _remainder_empty(steps: tuple[Step, ...], offset: CharOffset | None, tail: int) -> bool:
        return not steps and offset is None and tail == 0

    while k > 1 and (
        _remainder_empty(a_lp.steps[k:], a_lp.offset, len(a_following))
        or _remainder_empty(b_lp.steps[k:], b_lp.offset, len(b_following))
    ):
        k -= 1

    common_steps = a_lp.steps[:k]
    parent_paths = (*common_full, LocalPath(steps=common_steps, offset=None))

    start_sub = Cfi(paths=(LocalPath(steps=a_lp.steps[k:], offset=a_lp.offset), *a_following))
    end_sub = Cfi(paths=(LocalPath(steps=b_lp.steps[k:], offset=b_lp.offset), *b_following))
    return CfiRange(parent=Cfi(paths=parent_paths), start=start_sub, end=end_sub)


# --------------------------------------------------------------------------------------
# String-in / string-out convenience wrappers
# --------------------------------------------------------------------------------------


def xpoint_to_cfi_string(book: EpubMap, xpoint_str: str) -> str:
    """Parse a KOReader xpointer string, convert it, and serialize the CFI."""
    return xpoint_to_cfi(book, XPoint.parse(xpoint_str)).to_string()


def cfi_to_xpoint_string(book: EpubMap, cfi_str: str) -> str:
    """Parse a CFI string, convert it, and serialize the KOReader xpointer.

    Raises:
        ResolutionError: if the string parses to a range CFI (use
            :func:`cfi_to_xpoint_range_strings` for ranges).
    """
    parsed = parse_cfi(cfi_str)
    if isinstance(parsed, CfiRange):
        raise ResolutionError(cfi_str, "expected a single CFI, got a range")
    return cfi_to_xpoint(book, parsed).to_string()


def xpoint_range_to_cfi_string(book: EpubMap, start_str: str, end_str: str) -> str:
    """Parse two xpointer strings, convert the range, and serialize the range CFI."""
    return xpoint_range_to_cfi(book, XPointRange.parse(start_str, end_str)).to_string()


def cfi_to_xpoint_range_strings(book: EpubMap, cfi_str: str) -> tuple[str, str]:
    """Parse a range CFI string, convert it, and serialize the two xpointer strings.

    Raises:
        ResolutionError: if the string does not parse to a range CFI.
    """
    parsed = parse_cfi(cfi_str)
    if not isinstance(parsed, CfiRange):
        raise ResolutionError(cfi_str, "expected a range CFI (epubcfi(parent,start,end))")
    rng = cfi_range_to_xpoint_range(book, parsed)
    return rng.start.to_string(), rng.end.to_string()
