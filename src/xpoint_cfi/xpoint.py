"""KOReader (crengine) xpointer value objects.

An xpointer is KOReader's position string for a location inside an EPUB, of the
form ``/body/DocFragment[N]/body/.../text()[K].offset``. This module parses such
strings into immutable :class:`XPoint` value objects and serializes them back.

The value objects carry no dependency on any EPUB document; resolving an xpointer
against real XHTML happens elsewhere.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from xpoint_cfi.exceptions import XPointParseError

if TYPE_CHECKING:
    from typing import Self

_XPOINT_PATTERN = re.compile(
    r"^"
    r"(?:/body/DocFragment\[(\d+)\])?"  # optional DocFragment[N] - group 1
    r"(/body(?:/[^/.\s()]+)*)"  # xpath: /body followed by /element segments - group 2
    r"(?:"  # optional offset section
    r"(?:/text\(\)(?:\[(\d+)\])?)?"  # optional /text() with optional [N] - group 3
    r"\.(\d+)"  # .offset - group 4
    r")?"
    r"$"
)

_SEGMENT_PATTERN = re.compile(r"^([A-Za-z][A-Za-z0-9-]*)(?:\[(\d+)\])?$")

_BOXING_ELEMENTS = ("autoBoxing", "floatBox", "inlineBox", "tabularBox")


def _tag_of(segment: str) -> str:
    """Return the tag name of an xpath segment, stripping any ``[N]`` predicate."""
    return segment.split("[", 1)[0]


def normalize_xpath(xpath: str) -> tuple[tuple[str, int], ...]:
    """Split an element xpath into normalized ``(tag, index)`` segments.

    Each segment of ``xpath`` must have the form ``tag`` or ``tag[N]`` where ``N``
    is a positive integer. A missing predicate defaults to index ``1``.

    Args:
        xpath: An element path such as ``/body/div/p[88]`` (no ``text()`` part).

    Returns:
        A tuple of ``(tag, index_1based)`` pairs, one per path segment.

    Raises:
        XPointParseError: If any segment is empty or does not match ``tag[N]`` form,
            or carries a non-positive index.
    """
    segments: list[tuple[str, int]] = []
    for raw in xpath.split("/"):
        if raw == "":
            continue
        match = _SEGMENT_PATTERN.match(raw)
        if match is None:
            raise XPointParseError(xpath, f"segment {raw!r} is not of the form tag[N]")
        tag, index_str = match.groups()
        index = int(index_str) if index_str is not None else 1
        if index < 1:
            raise XPointParseError(xpath, f"segment {raw!r} has a non-positive index")
        segments.append((tag, index))
    if not segments:
        raise XPointParseError(xpath, "xpath has no segments")
    return tuple(segments)


@dataclass(frozen=True)
class XPoint:
    """A single parsed KOReader xpointer position.

    Attributes:
        doc_fragment_index: 1-based EPUB spine index (the ``DocFragment[N]`` value).
        xpath: Element path starting at ``/body`` with no ``text()`` part.
        text_node_index: 1-based ``text()`` node index within the element.
        char_offset: 0-based code-point offset within the text node.
        has_text_position: Whether the original string carried a ``.offset`` suffix.
            ``False`` denotes a bare element-boundary xpointer such as
            ``/body/DocFragment[14]/body/a``; ``True`` denotes any xpointer with an
            explicit offset, including ``.../img.0`` and ``.../text().223``.
    """

    doc_fragment_index: int
    xpath: str
    text_node_index: int
    char_offset: int
    has_text_position: bool

    @classmethod
    def parse(cls, xpoint: str) -> Self:
        """Parse a KOReader xpointer string into an :class:`XPoint`.

        Accepted forms include ``/body/DocFragment[12]/body/div/p[88]/text().223``,
        ``/body/DocFragment[14]/body/a`` (element boundary), ``.../img.0`` (offset on
        a non-text element), a missing ``DocFragment`` prefix, and ``text()`` with or
        without a ``[N]`` predicate.

        Args:
            xpoint: The xpointer string to parse.

        Returns:
            The parsed :class:`XPoint`.

        Raises:
            XPointParseError: If the string does not match the xpointer grammar, has
                out-of-range indices, or contains a crengine boxing element.
        """
        match = _XPOINT_PATTERN.match(xpoint)
        if match is None:
            raise XPointParseError(xpoint, "does not match expected xpoint format")

        doc_fragment_str, xpath, text_node_str, offset_str = match.groups()

        for segment in xpath.split("/"):
            if _tag_of(segment) in _BOXING_ELEMENTS:
                raise XPointParseError(
                    xpoint,
                    "contains crengine boxing element "
                    f"{_tag_of(segment)!r}; this is an un-normalized pre-2020 KOReader "
                    "xpointer (DOM version < 20200223) and cannot be resolved",
                )

        doc_fragment_index = int(doc_fragment_str) if doc_fragment_str else 1
        text_node_index = int(text_node_str) if text_node_str else 1
        has_text_position = offset_str is not None
        char_offset = int(offset_str) if offset_str is not None else 0

        if doc_fragment_index < 1:
            raise XPointParseError(xpoint, "DocFragment index must be >= 1")
        if text_node_index < 1:
            raise XPointParseError(xpoint, "text node index must be >= 1")
        if char_offset < 0:
            raise XPointParseError(xpoint, "character offset must be >= 0")

        return cls(
            doc_fragment_index=doc_fragment_index,
            xpath=xpath,
            text_node_index=text_node_index,
            char_offset=char_offset,
            has_text_position=has_text_position,
        )

    def to_string(self) -> str:
        """Serialize back to KOReader xpointer string format.

        Element-boundary xpoints (``has_text_position`` is ``False``) emit neither
        ``/text()`` nor ``.offset``. Xpoints with a text position always emit the
        offset, preceded by ``/text()`` (or ``/text()[N]`` when the node index
        exceeds 1). Re-parsing the result yields an equal :class:`XPoint`.
        """
        parts = [f"/body/DocFragment[{self.doc_fragment_index}]", self.xpath]
        if self.has_text_position:
            if self.text_node_index > 1:
                parts.append(f"/text()[{self.text_node_index}]")
            else:
                parts.append("/text()")
            parts.append(f".{self.char_offset}")
        return "".join(parts)


@dataclass(frozen=True)
class XPointRange:
    """A start/end pair of xpoints delimiting a highlighted region.

    Attributes:
        start: The start position (must not come after ``end``).
        end: The end position.
    """

    start: XPoint
    end: XPoint

    def __post_init__(self) -> None:
        """Validate that ``start`` does not come after ``end`` in document order.

        Raises:
            XPointParseError: If ``start`` is positioned after ``end``.
        """
        start_frag = self.start.doc_fragment_index
        end_frag = self.end.doc_fragment_index

        if start_frag > end_frag:
            raise XPointParseError(
                self.start.to_string(),
                "start doc fragment comes after end doc fragment",
            )

        if start_frag == end_frag and self.start.xpath == self.end.xpath:
            if self.start.text_node_index > self.end.text_node_index:
                raise XPointParseError(
                    self.start.to_string(),
                    "start text node comes after end text node",
                )
            if (
                self.start.text_node_index == self.end.text_node_index
                and self.start.char_offset > self.end.char_offset
            ):
                raise XPointParseError(
                    self.start.to_string(),
                    "start offset comes after end offset within the same text node",
                )

    @classmethod
    def parse(cls, start_xpoint_str: str, end_xpoint_str: str) -> Self:
        """Parse a range from two xpointer strings.

        Args:
            start_xpoint_str: The start xpointer string.
            end_xpoint_str: The end xpointer string.

        Returns:
            The parsed :class:`XPointRange`.

        Raises:
            XPointParseError: If either string is invalid or the range is reversed.
        """
        return cls(
            start=XPoint.parse(start_xpoint_str),
            end=XPoint.parse(end_xpoint_str),
        )
