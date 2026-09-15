"""The document layer: map EPUB spine XHTML between xpointer and CFI coordinates.

:class:`EpubMap` parses an EPUB container once (with the stdlib :mod:`zipfile` and
lxml) and lazily exposes each spine item as a :class:`NodeMap`. A :class:`NodeMap`
wraps one parsed XHTML tree and implements both coordinate systems used by the
converters:

* **Element addressing** — xpointer per-tag-name XPath (``/body/div/p[3]``) and CFI
  even-index steps (``/4/2/6``), matched purely by *local name* because xpointer paths
  are namespace-free while XHTML carries a default namespace.
* **Text addressing** — the *chunk model*. Element children partition an element's
  character data into gaps numbered with CFI odd indices ``1, 3, 5, …``. Comments and
  processing instructions are invisible to CFI (they neither count as element children
  nor split a chunk) even though lxml attaches their following text to a ``.tail``.
  Crengine likewise drops comments and merges adjacent text, so xpointer ``text()[N]``
  is mapped onto the *N-th countable chunk* (a chunk with at least one non-whitespace
  character) treated as a single text node — crengine drops whitespace-only text nodes,
  so those are skipped when counting. Within a chunk, xpointer offsets index crengine's
  *collapsed* text (see :mod:`xpoint_cfi.crengine_text`) while CFI offsets index the raw
  source, so the two are bridged per chunk.

This module performs no string parsing of xpointers or CFIs; it consumes the value
objects produced by :mod:`xpoint_cfi.xpoint` and :mod:`xpoint_cfi.cfi`.
"""

from __future__ import annotations

import io
import posixpath
import urllib.parse
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from typing import TYPE_CHECKING

from lxml import etree

from .cfi import Step
from .crengine_text import collapse_with_map, is_countable, raw_to_collapsed
from .exceptions import EpubStructureError, ResolutionError

if TYPE_CHECKING:
    import os

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]

_CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"

# Assumed media type for a spine item whose manifest <item> omits @media-type.
_DEFAULT_MEDIA_TYPE = "application/xhtml+xml"

# Block-level HTML tags: text extraction inserts a "\n" separator when consecutive text
# nodes belong to different block containers (matching KOReader's highlight export).
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "body",
        "dd",
        "div",
        "dl",
        "dt",
        "fieldset",
        "figcaption",
        "figure",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)


def _flow_parent(node: _Element) -> _Element:
    """Return the element whose text flow a ``tail`` text belongs to."""
    parent = node.getparent()
    return parent if parent is not None else node


def _local(tag: str) -> str:
    """Return the local name of a possibly namespace-qualified ``{uri}name`` tag."""
    return tag.rsplit("}", 1)[-1]


def _is_element(node: _Element) -> bool:
    """Return ``True`` for real elements, ``False`` for lxml comments and PIs.

    lxml exposes comments and processing instructions as children whose ``tag`` is a
    callable factory rather than a string; only genuine elements have a string tag.
    """
    return not callable(node.tag)


def _local_name(node: _Element) -> str:
    """Return the local name of an element node."""
    return _local(node.tag)


def element_children(node: _Element) -> list[_Element]:
    """Return the real element children of ``node`` (comments and PIs excluded)."""
    return [child for child in node if _is_element(child)]


def _sibling_position(child: _Element, *, same_name: bool = False) -> int:
    """Return the 1-based position of ``child`` among its parent's element children.

    With ``same_name`` only siblings sharing the child's local name are counted
    (xpointer ``tag[N]`` semantics); otherwise all element siblings count (CFI
    even-step semantics).
    """
    name = _local_name(child) if same_name else None
    count = 0
    for sibling in child.itersiblings(preceding=True):
        if _is_element(sibling) and (name is None or _local_name(sibling) == name):
            count += 1
    return count + 1


def _nth_child(parent: _Element, index: int, name: str | None = None) -> _Element | None:
    """Return the ``index``-th (1-based) element child of ``parent``, or ``None``.

    With ``name`` only element children with that local name are counted (xpointer
    semantics); otherwise every element child counts (CFI semantics).
    """
    count = 0
    for child in parent:
        if _is_element(child) and (name is None or _local_name(child) == name):
            count += 1
            if count == index:
                return child
    return None


def cp_to_utf16(text: str, cp_offset: int) -> int:
    """Convert a code-point offset within ``text`` to a UTF-16 code-unit offset."""
    return len(text[:cp_offset].encode("utf-16-le")) // 2


def utf16_to_cp(text: str, utf16_offset: int) -> int:
    """Convert a UTF-16 code-unit offset within ``text`` to a code-point offset.

    Raises:
        ResolutionError: if the offset is negative, exceeds the text, or lands inside a
            surrogate pair (i.e. between the two code units of an astral character).
    """
    if utf16_offset < 0:
        raise ResolutionError(f"utf-16 offset {utf16_offset}", "offset must be >= 0")
    units = 0
    for cp_index, ch in enumerate(text):
        if units == utf16_offset:
            return cp_index
        units += 2 if ord(ch) > 0xFFFF else 1
        if units > utf16_offset:
            raise ResolutionError(
                f"utf-16 offset {utf16_offset}",
                "offset falls inside a surrogate pair",
            )
    if units == utf16_offset:
        return len(text)
    raise ResolutionError(
        f"utf-16 offset {utf16_offset}",
        f"offset exceeds text length ({units} utf-16 units)",
    )


@dataclass(frozen=True)
class Chunk:
    """The character-data gap at one CFI odd index within an element.

    ``odd_index`` is the CFI odd index (``1`` before the first element child, ``2k+1``
    after the k-th). ``text`` is the gap's full character data (pieces split by
    comments/PIs are already concatenated). ``anchor`` is the lxml location where that
    text begins — ``(node, "text")`` for an element's leading text or ``(node, "tail")``
    for text after a child — or ``None`` for a gap with no text at all.
    """

    odd_index: int
    text: str
    anchor: tuple[_Element, str] | None


@dataclass(frozen=True)
class _OffsetView:
    """Bridges crengine's collapsed offsets and raw code-point offsets for one chunk.

    ``pos`` is the :func:`~xpoint_cfi.crengine_text.collapse_with_map` back-map, or
    ``None`` for identity mapping (``<pre>`` content, where crengine keeps whitespace).
    """

    raw: str
    collapsed: str
    pos: tuple[int, ...] | None

    def raw_cp(self, collapsed_offset: int) -> int:
        """Return the raw code-point offset for a collapsed-space offset."""
        return collapsed_offset if self.pos is None else self.pos[collapsed_offset]

    def collapsed_cp(self, raw_cp: int) -> int:
        """Return the collapsed-space offset for a raw code-point offset."""
        return raw_cp if self.pos is None else raw_to_collapsed(self.raw, raw_cp)


@dataclass(frozen=True)
class _TextIndex:
    """Document-order text locations of one body, with absolute offsets precomputed.

    Built once per :class:`NodeMap` (the tree is immutable) and reused by every
    :meth:`NodeMap.extract_text` call.
    """

    locations: tuple[tuple[_Element, str, str], ...]
    cumulative: tuple[int, ...]
    starts: dict[tuple[_Element, str], int]
    full: str


class NodeMap:
    """Both coordinate systems for a single parsed spine XHTML document."""

    def __init__(self, root: _Element) -> None:
        self._root = root
        # Keyed by the element rather than its id: lxml frees an unreferenced element's proxy
        # and can hand that id to another element's.
        self._chunk_cache: dict[_Element, tuple[Chunk, ...]] = {}
        self._block_cache: dict[_Element, _Element] = {}
        self._text_index: _TextIndex | None = None

    # -- element addressing ------------------------------------------------------------

    @property
    def root(self) -> _Element:
        """Return the document element (``<html>``) of the parsed spine item."""
        return self._root

    @property
    def body(self) -> _Element:
        """Return the document's ``<body>`` element.

        Raises:
            ResolutionError: if the document element has no ``<body>`` child.
        """
        return self._find_body()

    def _find_body(self) -> _Element:
        body = _nth_child(self._root, 1, "body")
        if body is None:
            raise ResolutionError("<body>", "document element has no <body> child")
        return body

    def element_by_xpath(self, segments: tuple[tuple[str, int], ...]) -> _Element:
        """Resolve normalized ``(tag, index)`` xpath segments to an element.

        ``segments`` come from :func:`xpoint_cfi.xpoint.normalize_xpath` and begin at
        ``("body", N)``. Matching is by local name at every level.

        Raises:
            ResolutionError: if any segment cannot be matched.
        """
        if not segments:
            raise ResolutionError("<empty xpath>", "xpath has no segments")
        xpath = _segments_to_xpath(segments)
        node = self._root
        for tag, index in segments:
            child = _nth_child(node, index, tag)
            if child is None:
                raise ResolutionError(xpath, f"no <{tag}> #{index} under <{_local_name(node)}>")
            node = child
        return node

    def xpath_for_element(self, elem: _Element) -> str:
        """Return the ``/body/div[1]/p[3]`` xpath addressing ``elem``.

        Every segment but the terminal ``body`` carries an explicit ``[N]`` counting
        preceding same-local-name element siblings.

        Raises:
            ResolutionError: if ``elem`` is not contained in a ``<body>``.
        """
        parts: list[str] = []
        node = elem
        while True:
            if _local_name(node) == "body":
                parts.append("body")
                break
            parent = node.getparent()
            if parent is None:
                raise ResolutionError("<element>", "element is not contained in a <body>")
            parts.append(f"{_local_name(node)}[{_sibling_position(node, same_name=True)}]")
            node = parent
        parts.reverse()
        return "/" + "/".join(parts)

    def cfi_steps_for_element(self, elem: _Element) -> tuple[Step, ...]:
        """Return the CFI element steps from the document element down to ``elem``.

        The document element itself contributes no step; each step index is
        ``2 * (position among element children)`` and carries the child's ``id`` as its
        assertion when present.
        """
        chain: list[_Element] = []
        node = elem
        parent = node.getparent()
        while parent is not None:
            chain.append(node)
            node = parent
            parent = node.getparent()
        chain.reverse()
        steps: list[Step] = []
        for child in chain:
            index = 2 * _sibling_position(child)
            steps.append(Step(index=index, assertion=child.get("id")))
        return tuple(steps)

    def element_by_cfi_steps(self, steps: tuple[Step, ...]) -> _Element:
        """Descend the tree by CFI element steps, honouring ``id`` self-repair.

        Raises:
            ResolutionError: on an odd step index (text steps are not elements) or when
                a step cannot be resolved even after an ``id``-based document lookup.
        """
        node = self._root
        for step in steps:
            if step.index % 2 == 1:
                raise ResolutionError(
                    step.to_string(), "odd step index does not address an element"
                )
            child = _nth_child(node, step.index // 2)
            resolved = child
            if (
                resolved is not None
                and step.assertion is not None
                and resolved.get("id") != step.assertion
            ):
                resolved = None
            if resolved is None:
                if step.assertion is not None:
                    repaired = self._find_by_id(step.assertion)
                    if repaired is not None:
                        node = repaired
                        continue
                raise ResolutionError(step.to_string(), "step does not resolve to an element")
            node = resolved
        return node

    def _find_by_id(self, id_value: str) -> _Element | None:
        for el in self._root.iter():
            if _is_element(el) and el.get("id") == id_value:
                return el
        return None

    # -- text addressing (chunk model) ------------------------------------------------

    def chunks(self, elem: _Element) -> tuple[Chunk, ...]:
        """Return every character-data gap of ``elem``, including empty ones.

        Comments and PIs do not open a new gap; their ``.tail`` text is appended to the
        current gap's text. Results are cached per element (the tree is immutable).
        """
        cached = self._chunk_cache.get(elem)
        if cached is not None:
            return cached

        result: list[Chunk] = []
        seen_elements = 0
        pieces: list[str] = []
        anchor: tuple[_Element, str] | None = None
        if elem.text:
            pieces.append(elem.text)
            anchor = (elem, "text")

        def close_gap() -> None:
            result.append(
                Chunk(odd_index=2 * seen_elements + 1, text="".join(pieces), anchor=anchor)
            )

        for child in elem:
            if _is_element(child):
                close_gap()
                seen_elements += 1
                pieces = []
                anchor = None
            if child.tail:
                pieces.append(child.tail)
                if anchor is None:
                    anchor = (child, "tail")
        close_gap()

        chunks = tuple(result)
        self._chunk_cache[elem] = chunks
        return chunks

    @staticmethod
    def _crengine_counts(chunk: Chunk) -> bool:
        """Return ``True`` when crengine keeps this chunk as a text node.

        Corpus-verified rule: a **leading** whitespace-only text node (the gap before
        the first element child) is dropped by crengine, while **medial/trailing**
        whitespace-only text nodes between element children are kept. Truly empty gaps
        hold no text node at all and never count.
        """
        if not chunk.text:
            return False
        return is_countable(chunk.text) or chunk.odd_index > 1

    def countable_chunks(self, elem: _Element) -> tuple[Chunk, ...]:
        """Return the chunks crengine keeps as text nodes (see :meth:`_crengine_counts`).

        These are exactly the chunks an xpointer ``text()[N]`` can address, in order;
        every other chunk holds text crengine drops, which therefore has no xpointer
        coordinate at all.
        """
        return tuple(chunk for chunk in self.chunks(elem) if self._crengine_counts(chunk))

    @staticmethod
    def _in_pre(elem: _Element) -> bool:
        """Return ``True`` when ``elem`` or an ancestor is a ``<pre>`` element.

        crengine does not collapse whitespace inside ``white-space:pre`` content; a
        ``<pre>`` local-name ancestor is the structural (non-CSS) signal for it. CSS-driven
        ``white-space:pre`` on other elements is out of scope and not detected here.
        """
        node: _Element | None = elem
        while node is not None:
            if _is_element(node) and _local_name(node) == "pre":
                return True
            node = node.getparent()
        return False

    def _offset_view(self, elem: _Element, chunk: Chunk) -> _OffsetView:
        """Return the collapsed⇄raw offset bridge for ``chunk`` of ``elem``.

        Identity mapping inside ``<pre>`` content; the crengine collapse map otherwise.
        """
        if self._in_pre(elem):
            return _OffsetView(raw=chunk.text, collapsed=chunk.text, pos=None)
        collapsed, pos = collapse_with_map(chunk.text)
        return _OffsetView(raw=chunk.text, collapsed=collapsed, pos=pos)

    def text_position_to_cfi(
        self, elem: _Element, text_node_index: int, char_offset: int
    ) -> tuple[int, int] | None:
        """Map an xpointer ``text()[N].offset`` to a CFI ``(odd_index, utf16_offset)``.

        ``text_node_index`` counts crengine-countable chunks (see
        :meth:`_crengine_counts`). ``char_offset`` is a code-point offset in crengine's
        text-node coordinate (collapsed space, or raw inside ``<pre>``); it is bridged
        to a raw UTF-16 offset, which is what a CFI stores.

        Returns ``None`` for the default ``text()[1].0`` position on an element crengine
        keeps no text node for (e.g. KOReader's ``.../img.0``): such a point has no text
        location and is an element boundary.

        Raises:
            ResolutionError: if ``text_node_index`` exceeds the number of countable text
                chunks, or ``char_offset`` exceeds the chunk's crengine text length.
        """
        countable = self.countable_chunks(elem)
        if not countable and text_node_index == 1 and char_offset == 0:
            return None
        if text_node_index < 1 or text_node_index > len(countable):
            raise ResolutionError(
                f"text()[{text_node_index}]",
                f"element has {len(countable)} countable text chunk(s)",
            )
        chunk = countable[text_node_index - 1]
        view = self._offset_view(elem, chunk)
        if char_offset < 0 or char_offset > len(view.collapsed):
            raise ResolutionError(
                f"text()[{text_node_index}].{char_offset}",
                f"offset exceeds text-node length ({len(view.collapsed)} code points)",
            )
        return chunk.odd_index, cp_to_utf16(chunk.text, view.raw_cp(char_offset))

    def cfi_to_text_position(
        self, elem: _Element, odd_index: int, utf16_offset: int
    ) -> tuple[int, int]:
        """Map a CFI ``(odd_index, utf16_offset)`` to an xpointer ``(text_node, offset)``.

        The returned ``char_offset`` is measured in code points **in crengine's collapsed
        space** (the coordinate an xpointer uses), except inside a ``<pre>`` element where
        raw and collapsed coincide. Pointing at a whitespace-only (non-countable) chunk is
        permitted only with ``utf16_offset == 0`` (a best-effort element-edge location),
        yielding the count of countable chunks up to that gap, clamped to 1.

        Raises:
            ResolutionError: if ``odd_index`` is even or out of the gap range, if a
                whitespace-only chunk is addressed with a non-zero offset, or if
                ``utf16_offset`` is invalid for the chunk text.
        """
        if odd_index < 1 or odd_index % 2 == 0:
            raise ResolutionError(f"/{odd_index}", "text-node index must be odd and >= 1")
        all_chunks = self.chunks(elem)
        chunk = next((c for c in all_chunks if c.odd_index == odd_index), None)
        if chunk is None:
            raise ResolutionError(
                f"/{odd_index}", f"gap index out of range (element has {len(all_chunks)} gaps)"
            )
        preceding_countable = sum(
            1 for c in all_chunks if self._crengine_counts(c) and c.odd_index <= odd_index
        )
        if not self._crengine_counts(chunk):
            if utf16_offset != 0:
                raise ResolutionError(
                    f"/{odd_index}:{utf16_offset}",
                    "empty or leading whitespace-only text chunk addressed with a non-zero offset",
                )
            return max(preceding_countable, 1), 0
        raw_cp = utf16_to_cp(chunk.text, utf16_offset)
        return preceding_countable, self._offset_view(elem, chunk).collapsed_cp(raw_cp)

    # -- text extraction ---------------------------------------------------------------

    def extract_text(
        self,
        start: tuple[_Element, int, int] | None,
        end: tuple[_Element, int, int] | None,
    ) -> str:
        """Return the body text between two ``(element, odd_index, utf16_offset)`` bounds.

        A ``None`` bound denotes the document start / end. Positions are resolved via the
        chunk model onto concrete lxml text locations, then sliced out of the document's
        text index (built once per :class:`NodeMap` and cached).
        """
        index = self._get_text_index()
        start_index = 0 if start is None else self._absolute_offset(start, index)
        end_index = len(index.full) if end is None else self._absolute_offset(end, index)
        return index.full[start_index:end_index]

    def _get_text_index(self) -> _TextIndex:
        if self._text_index is None:
            locations = tuple(_iter_text_locations(self._find_body()))
            cumulative: list[int] = []
            pieces: list[str] = []
            running = 0
            previous_block: _Element | None = None
            for node, attr, text in locations:
                block = self.block_ancestor(node if attr == "text" else _flow_parent(node))
                if previous_block is not None and block is not previous_block:
                    # KOReader separates block elements with a newline when exporting
                    # highlight text; minified XHTML has no whitespace between blocks,
                    # so extraction must add the separator itself.
                    pieces.append("\n")
                    running += 1
                previous_block = block
                cumulative.append(running)
                pieces.append(text)
                running += len(text)
            self._text_index = _TextIndex(
                locations=locations,
                cumulative=tuple(cumulative),
                starts={
                    (node, attr): cum
                    for (node, attr, _), cum in zip(locations, cumulative, strict=True)
                },
                full="".join(pieces),
            )
        return self._text_index

    def block_ancestor(self, elem: _Element) -> _Element:
        """Return ``elem``'s nearest ancestor-or-self with a block-level tag.

        Cached per element; falls back to the document element when no block tag is
        found (foreign or fully-inline markup).
        """
        cached = self._block_cache.get(elem)
        if cached is not None:
            return cached
        node: _Element | None = elem
        result = self._root
        while node is not None:
            if _is_element(node) and _local_name(node) in _BLOCK_TAGS:
                result = node
                break
            node = node.getparent()
        self._block_cache[elem] = result
        return result

    def _absolute_offset(self, position: tuple[_Element, int, int], index: _TextIndex) -> int:
        elem, odd_index, utf16_offset = position
        all_chunks = self.chunks(elem)
        chunk = next((c for c in all_chunks if c.odd_index == odd_index), None)
        if chunk is None:
            raise ResolutionError(
                f"/{odd_index}", f"gap index out of range (element has {len(all_chunks)} gaps)"
            )
        if chunk.anchor is not None:
            node, attr = chunk.anchor
            return index.starts[(node, attr)] + utf16_to_cp(chunk.text, utf16_offset)
        return self._empty_gap_offset(elem, odd_index, index)

    def _empty_gap_offset(self, elem: _Element, odd_index: int, index: _TextIndex) -> int:
        children = element_children(elem)
        gap = (odd_index - 1) // 2
        if gap < len(children):
            following = set(children[gap].iter())
            for i, (node, _, _) in enumerate(index.locations):
                if node in following:
                    return index.cumulative[i]
        if gap > 0:
            preceding = set(children[gap - 1].iter())
            last = -1
            for i, (node, _, _) in enumerate(index.locations):
                if node in preceding:
                    last = i
            if last >= 0:
                return index.cumulative[last] + len(index.locations[last][2])
        subtree = set(elem.iter())
        for i, (node, _, _) in enumerate(index.locations):
            if node in subtree:
                return index.cumulative[i]
        return len(index.full)


def _iter_text_locations(node: _Element) -> Iterator[tuple[_Element, str, str]]:
    """Yield ``(node, attr, text)`` text locations under ``node`` in document order.

    ``attr`` is ``"text"`` for an element's leading text and ``"tail"`` for the trailing
    text of any child (element, comment, or PI). Comment/PI content is never yielded.
    """
    if node.text:
        yield (node, "text", node.text)
    for child in node:
        if _is_element(child):
            yield from _iter_text_locations(child)
        if child.tail:
            yield (child, "tail", child.tail)


def _segments_to_xpath(segments: tuple[tuple[str, int], ...]) -> str:
    return "/" + "/".join(f"{tag}[{index}]" for tag, index in segments)


@dataclass(frozen=True)
class _SpineItem:
    idref: str
    href: str
    itemref_id: str | None
    media_type: str


class EpubMap:
    """Parsed EPUB package exposing spine items as lazily-built :class:`NodeMap`\\ s."""

    def __init__(self, data: bytes) -> None:
        try:
            self._zip = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise EpubStructureError(f"not a valid zip archive: {exc}") from exc

        opf_path = self._read_container()
        opf_root = self._read_opf(opf_path)
        self._opf_dir = posixpath.dirname(opf_path)
        self._spine_element_step, self._spine = self._parse_package(opf_root)
        self._doc_cache: dict[int, NodeMap] = {}

    @classmethod
    def from_bytes(cls, data: bytes) -> EpubMap:
        """Build an :class:`EpubMap` from raw EPUB bytes."""
        return cls(data)

    @classmethod
    def from_path(cls, path: str | os.PathLike[str]) -> EpubMap:
        """Build an :class:`EpubMap` by reading the EPUB file at ``path``."""
        with open(path, "rb") as handle:
            return cls(handle.read())

    # -- construction helpers ----------------------------------------------------------

    def _read_container(self) -> str:
        try:
            data = self._zip.read("META-INF/container.xml")
        except KeyError as exc:
            raise EpubStructureError("missing META-INF/container.xml") from exc
        try:
            root = etree.fromstring(data, etree.XMLParser())
        except etree.XMLSyntaxError as exc:
            raise EpubStructureError(f"malformed container.xml: {exc}") from exc
        for el in root.iter():
            if _is_element(el) and _local_name(el) == "rootfile":
                full_path = el.get("full-path")
                if full_path:
                    return full_path
        raise EpubStructureError("container.xml has no rootfile with a full-path")

    def _read_opf(self, opf_path: str) -> _Element:
        try:
            data = self._zip.read(opf_path)
        except KeyError as exc:
            raise EpubStructureError(f"OPF package not found at {opf_path!r}") from exc
        try:
            return etree.fromstring(data, etree.XMLParser())
        except etree.XMLSyntaxError as exc:
            raise EpubStructureError(f"malformed OPF package: {exc}") from exc

    def _parse_package(self, package: _Element) -> tuple[int, list[_SpineItem]]:
        spine_el: _Element | None = None
        manifest_el: _Element | None = None
        spine_position = 0
        for position, child in enumerate(element_children(package), start=1):
            name = _local_name(child)
            if name == "spine" and spine_el is None:
                spine_el = child
                spine_position = position
            elif name == "manifest" and manifest_el is None:
                manifest_el = child
        if manifest_el is None:
            raise EpubStructureError("OPF package has no <manifest>")
        if spine_el is None:
            raise EpubStructureError("OPF package has no <spine>")

        manifest: dict[str, tuple[str, str]] = {}
        for item in manifest_el:
            if not _is_element(item) or _local_name(item) != "item":
                continue
            item_id = item.get("id")
            href = item.get("href")
            if item_id is not None and href is not None:
                manifest[item_id] = (href, item.get("media-type") or _DEFAULT_MEDIA_TYPE)

        spine: list[_SpineItem] = []
        for itemref in spine_el:
            if not _is_element(itemref) or _local_name(itemref) != "itemref":
                continue
            idref = itemref.get("idref")
            if idref is None:
                raise EpubStructureError("spine <itemref> has no idref")
            entry = manifest.get(idref)
            if entry is None:
                raise EpubStructureError(
                    f"spine itemref idref {idref!r} has no matching manifest <item>"
                )
            href, media_type = entry
            spine.append(_SpineItem(idref, self._resolve_href(href), itemref.get("id"), media_type))
        if not spine:
            raise EpubStructureError("spine has no itemrefs")
        return 2 * spine_position, spine

    def _resolve_href(self, href: str) -> str:
        joined = posixpath.join(self._opf_dir, href) if self._opf_dir else href
        return urllib.parse.unquote(posixpath.normpath(joined))

    # -- spine addressing --------------------------------------------------------------

    @property
    def spine_count(self) -> int:
        """Return the number of spine items."""
        return len(self._spine)

    @property
    def spine_element_step(self) -> int:
        """Return the CFI step index of the ``<spine>`` element within the package."""
        return self._spine_element_step

    def spine_href(self, spine_index: int) -> str:
        """Return the zip path of the spine item at 1-based ``spine_index``."""
        self._check_spine_index(spine_index)
        return self._spine[spine_index - 1].href

    def spine_media_type(self, spine_index: int) -> str:
        """Return the manifest media type of the spine item at 1-based ``spine_index``.

        Falls back to ``application/xhtml+xml`` when the manifest ``<item>`` carries no
        ``media-type`` attribute.
        """
        self._check_spine_index(spine_index)
        return self._spine[spine_index - 1].media_type

    def spine_idref(self, spine_index: int) -> str:
        """Return the manifest idref of the spine item at 1-based ``spine_index``."""
        self._check_spine_index(spine_index)
        return self._spine[spine_index - 1].idref

    def spine_step(self, spine_index: int) -> Step:
        """Return the CFI itemref step for the spine item at 1-based ``spine_index``.

        Per the CFI spec the ID assertion names the ``<itemref>``'s **own** ``id``
        attribute when it has one; the idref must not be used (spec-conformant
        resolvers repair via ``getElementById``, and the idref names the manifest
        ``<item>`` instead of the spine ``<itemref>``).
        """
        self._check_spine_index(spine_index)
        return Step(index=2 * spine_index, assertion=self._spine[spine_index - 1].itemref_id)

    def spine_index_for_step(self, step: Step) -> int:
        """Return the 1-based spine index a CFI itemref ``step`` addresses.

        An even index within range wins outright; otherwise the step's ``id`` assertion
        is matched against itemref ids, then against manifest idrefs (some producers
        put the idref in the assertion), as CFI self-repair.

        Raises:
            ResolutionError: if neither the index nor the assertion resolves.
        """
        if step.index % 2 == 0:
            spine_index = step.index // 2
            if 1 <= spine_index <= len(self._spine):
                return spine_index
        if step.assertion is not None:
            for i, item in enumerate(self._spine, start=1):
                if item.itemref_id == step.assertion:
                    return i
            for i, item in enumerate(self._spine, start=1):
                if item.idref == step.assertion:
                    return i
        raise ResolutionError(step.to_string(), "does not address any spine item")

    def _check_spine_index(self, spine_index: int) -> None:
        if not 1 <= spine_index <= len(self._spine):
            raise ResolutionError(
                f"spine index {spine_index}",
                f"out of range (book has {len(self._spine)} spine items)",
            )

    def doc(self, spine_index: int) -> NodeMap:
        """Return the :class:`NodeMap` for the spine item at 1-based ``spine_index``.

        The parsed tree is cached per index. Well-formed XML is parsed strictly; on a
        syntax error the parse is retried in recovery mode.

        Raises:
            EpubStructureError: if the spine item is missing or unrecoverable.
            ResolutionError: if ``spine_index`` is out of range.
        """
        self._check_spine_index(spine_index)
        cached = self._doc_cache.get(spine_index)
        if cached is not None:
            return cached
        href = self._spine[spine_index - 1].href
        try:
            raw = self._zip.read(href)
        except KeyError as exc:
            raise EpubStructureError(f"spine item {href!r} not found in archive") from exc
        root: _Element | None
        try:
            root = etree.fromstring(raw, etree.XMLParser())
        except etree.XMLSyntaxError:
            try:
                root = etree.fromstring(raw, etree.XMLParser(recover=True))
            except etree.XMLSyntaxError:
                root = None
        if root is None:
            raise EpubStructureError(f"spine item {href!r} could not be parsed")
        node_map = NodeMap(root)
        self._doc_cache[spine_index] = node_map
        return node_map
