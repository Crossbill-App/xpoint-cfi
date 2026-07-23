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


def _element_children(node: _Element) -> list[_Element]:
    """Return the real element children of ``node`` (comments and PIs excluded)."""
    return [child for child in node if _is_element(child)]


def _child_position(child: _Element) -> int:
    """Return the 1-based position of ``child`` among its parent's element children."""
    count = 0
    for sibling in child.itersiblings(preceding=True):
        if _is_element(sibling):
            count += 1
    return count + 1


def _same_name_position(child: _Element) -> int:
    """Return the 1-based position of ``child`` among same-local-name element siblings."""
    name = _local_name(child)
    count = 0
    for sibling in child.itersiblings(preceding=True):
        if _is_element(sibling) and _local_name(sibling) == name:
            count += 1
    return count + 1


def _nth_named_child(parent: _Element, name: str, index: int) -> _Element | None:
    """Return the ``index``-th (1-based) element child of ``parent`` with local ``name``."""
    count = 0
    for child in parent:
        if _is_element(child) and _local_name(child) == name:
            count += 1
            if count == index:
                return child
    return None


def _nth_element_child(parent: _Element, index: int) -> _Element | None:
    """Return the ``index``-th (1-based) element child of ``parent`` (any local name)."""
    count = 0
    for child in parent:
        if _is_element(child):
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
class TextPart:
    """One contiguous piece of a chunk's text.

    ``node`` is ``None`` when the text is the owning element's ``.text``; otherwise it is
    the child node (an element, comment, or PI) whose ``.tail`` supplies the text.
    """

    node: _Element | None
    text: str


@dataclass(frozen=True)
class Chunk:
    """The character-data gap at one CFI odd index within an element.

    ``odd_index`` is the CFI odd index (``1`` before the first element child, ``2k+1``
    after the k-th). ``parts`` holds only the non-empty pieces, in document order, but a
    chunk with no text still exists (empty ``parts``).
    """

    odd_index: int
    parts: tuple[TextPart, ...]

    @property
    def text(self) -> str:
        """Return the concatenation of all part texts."""
        return "".join(part.text for part in self.parts)


class NodeMap:
    """Both coordinate systems for a single parsed spine XHTML document."""

    def __init__(self, root: _Element) -> None:
        self._root = root

    # -- element addressing ------------------------------------------------------------

    def _find_body(self) -> _Element:
        body = _nth_named_child(self._root, "body", 1)
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
            child = _nth_named_child(node, tag, index)
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
            parts.append(f"{_local_name(node)}[{_same_name_position(node)}]")
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
            index = 2 * _child_position(child)
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
            child = _nth_element_child(node, step.index // 2)
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
        current gap. Only non-empty pieces are kept as :class:`TextPart`\\ s, but a gap
        with no text is still emitted as an empty :class:`Chunk`.
        """
        result: list[Chunk] = []
        seen_elements = 0
        current: list[TextPart] = []
        if elem.text:
            current.append(TextPart(None, elem.text))
        for child in elem:
            if _is_element(child):
                result.append(Chunk(odd_index=2 * seen_elements + 1, parts=tuple(current)))
                seen_elements += 1
                current = []
                if child.tail:
                    current.append(TextPart(child, child.tail))
            elif child.tail:
                current.append(TextPart(child, child.tail))
        result.append(Chunk(odd_index=2 * seen_elements + 1, parts=tuple(current)))
        return tuple(result)

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

    def _countable_chunks(self, elem: _Element) -> tuple[Chunk, ...]:
        """Return the chunks crengine keeps as text nodes (see :meth:`_crengine_counts`)."""
        return tuple(chunk for chunk in self.chunks(elem) if self._crengine_counts(chunk))

    def has_countable_text(self, elem: _Element) -> bool:
        """Return ``True`` when ``elem`` has at least one crengine-countable text chunk.

        An element with none (e.g. ``<p><img/></p>``) has no ``text()`` node crengine
        can address; an xpointer's ``text()``/``.offset`` on such an element degrades to
        an element boundary. Public so converters need not reach into chunk internals.
        """
        return any(self._crengine_counts(chunk) for chunk in self.chunks(elem))

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

    def text_position_to_cfi(
        self, elem: _Element, text_node_index: int, char_offset: int
    ) -> tuple[int, int]:
        """Map an xpointer ``text()[N].offset`` to a CFI ``(odd_index, utf16_offset)``.

        ``text_node_index`` counts crengine-countable chunks (whitespace-only chunks are
        invisible). ``char_offset`` is a code-point offset into crengine's *collapsed*
        text; it is translated to the raw source position (via
        :func:`~xpoint_cfi.crengine_text.collapse_with_map`) and then to a UTF-16 offset,
        which is what a CFI stores. Inside a ``<pre>`` element no collapsing happens and
        the offset is used against the raw text directly.

        Raises:
            ResolutionError: if ``text_node_index`` exceeds the number of countable text
                chunks, or ``char_offset`` exceeds the chunk's (collapsed) length.
        """
        countable = self._countable_chunks(elem)
        if text_node_index < 1 or text_node_index > len(countable):
            raise ResolutionError(
                f"text()[{text_node_index}]",
                f"element has {len(countable)} countable text chunk(s)",
            )
        chunk = countable[text_node_index - 1]
        text = chunk.text
        if self._in_pre(elem):
            if char_offset < 0 or char_offset > len(text):
                raise ResolutionError(
                    f"text()[{text_node_index}].{char_offset}",
                    f"offset exceeds text-node length ({len(text)} code points)",
                )
            return chunk.odd_index, cp_to_utf16(text, char_offset)
        collapsed, pos = collapse_with_map(text)
        if char_offset < 0 or char_offset > len(collapsed):
            raise ResolutionError(
                f"text()[{text_node_index}].{char_offset}",
                f"offset exceeds collapsed text-node length ({len(collapsed)} code points)",
            )
        raw_cp = pos[char_offset]
        return chunk.odd_index, cp_to_utf16(text, raw_cp)

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
        char_offset = raw_cp if self._in_pre(elem) else raw_to_collapsed(chunk.text, raw_cp)
        return preceding_countable, char_offset

    # -- text extraction ---------------------------------------------------------------

    def extract_text(
        self,
        start: tuple[_Element, int, int] | None,
        end: tuple[_Element, int, int] | None,
    ) -> str:
        """Return the body text between two ``(element, odd_index, utf16_offset)`` bounds.

        A ``None`` bound denotes the document start / end. Positions are resolved via the
        chunk model onto concrete lxml text locations, then the body's text is walked in
        document order and sliced between the two absolute offsets.
        """
        body = self._find_body()
        locations = list(_iter_text_locations(body))
        cumulative: list[int] = []
        running = 0
        for _, _, text in locations:
            cumulative.append(running)
            running += len(text)
        total = running
        location_start = {
            (id(node), attr): cum
            for (node, attr, _), cum in zip(locations, cumulative, strict=True)
        }

        start_index = (
            0
            if start is None
            else self._absolute_offset(start, locations, cumulative, location_start, total)
        )
        end_index = (
            total
            if end is None
            else self._absolute_offset(end, locations, cumulative, location_start, total)
        )
        full = "".join(text for _, _, text in locations)
        return full[start_index:end_index]

    def _absolute_offset(
        self,
        position: tuple[_Element, int, int],
        locations: list[tuple[_Element, str, str]],
        cumulative: list[int],
        location_start: dict[tuple[int, str], int],
        total: int,
    ) -> int:
        elem, odd_index, utf16_offset = position
        all_chunks = self.chunks(elem)
        chunk = next((c for c in all_chunks if c.odd_index == odd_index), None)
        if chunk is None:
            raise ResolutionError(
                f"/{odd_index}", f"gap index out of range (element has {len(all_chunks)} gaps)"
            )
        cp = utf16_to_cp(chunk.text, utf16_offset)
        if chunk.parts:
            first = chunk.parts[0]
            node = elem if first.node is None else first.node
            attr = "text" if first.node is None else "tail"
            base = location_start[(id(node), attr)]
            return base + cp
        return self._empty_gap_offset(elem, odd_index, locations, cumulative, total)

    def _empty_gap_offset(
        self,
        elem: _Element,
        odd_index: int,
        locations: list[tuple[_Element, str, str]],
        cumulative: list[int],
        total: int,
    ) -> int:
        children = _element_children(elem)
        gap = (odd_index - 1) // 2
        if gap < len(children):
            following = {id(node) for node in children[gap].iter()}
            for i, (node, _, _) in enumerate(locations):
                if id(node) in following:
                    return cumulative[i]
        if gap > 0:
            preceding = {id(node) for node in children[gap - 1].iter()}
            last = -1
            for i, (node, _, _) in enumerate(locations):
                if id(node) in preceding:
                    last = i
            if last >= 0:
                return cumulative[last] + len(locations[last][2])
        subtree = {id(node) for node in elem.iter()}
        for i, (node, _, _) in enumerate(locations):
            if id(node) in subtree:
                return cumulative[i]
        return total


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
        for position, child in enumerate(_element_children(package), start=1):
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

        manifest: dict[str, str] = {}
        for item in manifest_el:
            if not _is_element(item) or _local_name(item) != "item":
                continue
            item_id = item.get("id")
            href = item.get("href")
            if item_id is not None and href is not None:
                manifest[item_id] = href

        spine: list[_SpineItem] = []
        for itemref in spine_el:
            if not _is_element(itemref) or _local_name(itemref) != "itemref":
                continue
            idref = itemref.get("idref")
            if idref is None:
                raise EpubStructureError("spine <itemref> has no idref")
            href = manifest.get(idref)
            if href is None:
                raise EpubStructureError(
                    f"spine itemref idref {idref!r} has no matching manifest <item>"
                )
            spine.append(_SpineItem(idref, self._resolve_href(href), itemref.get("id")))
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
