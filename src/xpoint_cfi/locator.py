"""Readium locators: build them from xpointers, and resolve them back to xpointers.

A web reader built on `@readium/navigator <https://github.com/readium/ts-toolkit>`_ does
not address positions the way KOReader does. It uses a `Readium Locator
<https://readium.org/architecture/models/locators/>`_: a resource ``href`` plus a CSS
selector for the enclosing element and a *text quote* — the highlighted text with the
characters that surround it. This module is the bridge, so the same highlight can be
made on a KOReader device and shown in a browser.

The two directions are deliberately asymmetric:

* **Out** (:func:`xpoint_to_locator`, :func:`xpoint_range_to_locator`) is exact. The
  xpointer resolves to a document position and the quote is read straight out of the
  document, through the same :func:`~xpoint_cfi.text_range.extract_between` that the CFI
  self-check uses — so a locator's ``text.highlight`` and a verified CFI always agree
  about what a range says.
* **Back** (:func:`locator_to_xpoint_range`) is a *search*. The CSS selector only
  narrows the scope; the quote is then found within it by text, because a reader may
  have been running against a DOM that was processed differently. The result therefore
  carries a :class:`~xpoint_cfi.text_anchor.MatchConfidence` so callers can reject weak
  matches rather than silently store a bad position.

Deliberate choices this module makes, none of which the Locator model settles:

* **A single position gets an empty highlight.** ``text.before`` and ``text.after``
  meet at the position and ``text.highlight`` is ``""`` — the Hypothesis-style way to
  express a caret rather than a selection, and what
  :func:`~xpoint_cfi.text_anchor.find_quote` anchors on when it reads the locator back.
* **Context windows are 40 characters** on each side, in the middle of the 30-50 that
  reliably disambiguates repeated phrases without bloating stored locators.
* **``locations.progression`` is character-based**: the position's character offset
  within the resource's extracted text, divided by that text's length. It is an
  estimate of reading progress, not of rendered layout.
* **A range that crosses spine items** takes the *start* resource's ``href``, quotes the
  full cross-resource text, and takes ``text.after`` from the end resource. Such a
  locator cannot be resolved back (no single resource contains the quote) — KOReader
  does not produce cross-fragment highlights in practice.
* **A block separator is optional when resolving.** Extraction puts a ``"\\n"`` between
  consecutive block elements, as KOReader's highlight export does, whether or not the
  markup has whitespace there. A DOM has no text between ``</p><p>``, so a browser's
  ``Range.toString()`` reads ``here.Second`` where extraction reads ``here.\\nSecond``. A
  match weaker than :attr:`~xpoint_cfi.text_anchor.MatchConfidence.BOTH_CONTEXTS` is
  therefore tried again over the text without those separators, and the stronger kept.
* **``position``, ``totalProgression`` and ``title`` are left unset**, since all three
  need publication-wide data (a positions list, a navigation document) that an
  :class:`~xpoint_cfi.epub_map.EpubMap` does not carry.

One thing a text anchor cannot recover: **whitespace at the very edges of a quote**.
Comparison strips it, so a range whose last character is a space comes back one
character shorter. The recovered range denotes the same words — which is what the corpus
validation asserts — but is not byte-identical to the original xpointers.
"""

from __future__ import annotations

import urllib.parse
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, cast

from lxml import etree

from .css_selector import resolve_selector, selector_for_element
from .epub_map import cp_to_utf16, element_children
from .exceptions import ResolutionError
from .text_anchor import MatchConfidence, QuoteMatch, find_quote
from .text_range import extract_between, resolve_bound
from .xpoint import XPoint, XPointRange

if TYPE_CHECKING:
    from typing import Self

    from .epub_map import Chunk, EpubMap, NodeMap

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]

__all__ = [
    "Locator",
    "LocatorLocations",
    "LocatorMatch",
    "LocatorText",
    "locator_to_xpoint_range",
    "xpoint_range_to_locator",
    "xpoint_to_locator",
]

# Characters of surrounding text kept on each side of a quote.
CONTEXT_CHARS = 40

# Assumed resource media type when a locator read from JSON does not state one.
_DEFAULT_MEDIA_TYPE = "application/xhtml+xml"


# --------------------------------------------------------------------------------------
# The locator model
# --------------------------------------------------------------------------------------


def _as_str(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _as_mapping(value: object) -> Mapping[str, object]:
    """Return ``value`` as a string-keyed mapping, or an empty one if it is not one."""
    if not isinstance(value, Mapping):
        return {}
    entries = cast("Mapping[object, object]", value)
    return {str(key): item for key, item in entries.items()}


@dataclass(frozen=True)
class LocatorText:
    """A Readium locator's ``text`` object: the quote and its surroundings.

    Attributes:
        before: Text immediately preceding the quote in the resource.
        highlight: The quoted text itself; ``""`` for a single position.
        after: Text immediately following the quote.
    """

    before: str | None = None
    highlight: str | None = None
    after: str | None = None

    def to_dict(self) -> dict[str, str]:
        """Serialize to the Readium JSON shape, omitting fields that are unset."""
        fields = (("before", self.before), ("highlight", self.highlight), ("after", self.after))
        return {name: value for name, value in fields if value is not None}

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        """Read a Readium ``text`` object, ignoring unknown and ill-typed fields."""
        return cls(
            before=_as_str(payload.get("before")),
            highlight=_as_str(payload.get("highlight")),
            after=_as_str(payload.get("after")),
        )


@dataclass(frozen=True)
class LocatorLocations:
    """A Readium locator's ``locations`` object.

    Only the two fields this library can compute are modelled: ``progression`` from the
    base model and ``cssSelector`` from the HTML extension. ``fragments``, ``position``
    and ``totalProgression`` are out of scope (see the module docstring).

    Attributes:
        progression: Progress through the resource, 0..1.
        css_selector: A ``querySelector``-resolvable selector for the enclosing element.
    """

    progression: float | None = None
    css_selector: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize to the Readium JSON shape, omitting fields that are unset."""
        out: dict[str, object] = {}
        if self.progression is not None:
            out["progression"] = self.progression
        if self.css_selector is not None:
            out["cssSelector"] = self.css_selector
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        """Read a Readium ``locations`` object, ignoring unknown and ill-typed fields."""
        progression = payload.get("progression")
        return cls(
            progression=float(progression) if isinstance(progression, (int, float)) else None,
            css_selector=_as_str(payload.get("cssSelector")),
        )


@dataclass(frozen=True)
class Locator:
    """A Readium Locator pointing into one resource of a publication.

    Attributes:
        href: The spine item's path inside the EPUB container, percent-encoded as a URI
            reference — :meth:`~xpoint_cfi.epub_map.EpubMap.spine_href` reports the same
            path decoded, for reading the archive.
        type: The resource's media type.
        title: Always ``None`` here; a navigation document would be needed to fill it.
        locations: Alternative expressions of the position.
        text: The textual anchor.
    """

    href: str
    type: str
    title: str | None = None
    locations: LocatorLocations = field(default_factory=LocatorLocations)
    text: LocatorText = field(default_factory=LocatorText)

    def to_dict(self) -> dict[str, object]:
        """Serialize to the Readium Locator JSON shape, omitting fields that are unset."""
        out: dict[str, object] = {"href": self.href, "type": self.type}
        if self.title is not None:
            out["title"] = self.title
        locations = self.locations.to_dict()
        if locations:
            out["locations"] = locations
        text = self.text.to_dict()
        if text:
            out["text"] = text
        return out

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> Self:
        """Read a Readium Locator, ignoring unknown and ill-typed fields.

        Raises:
            ResolutionError: if ``href`` is missing or is not a string; every other
                field has a usable default.
        """
        href = _as_str(payload.get("href"))
        if href is None:
            raise ResolutionError("<locator>", "locator has no 'href'")
        return cls(
            href=href,
            type=_as_str(payload.get("type")) or _DEFAULT_MEDIA_TYPE,
            title=_as_str(payload.get("title")),
            locations=LocatorLocations.from_dict(_as_mapping(payload.get("locations"))),
            text=LocatorText.from_dict(_as_mapping(payload.get("text"))),
        )


@dataclass(frozen=True)
class LocatorMatch:
    """The outcome of resolving a locator back to KOReader coordinates.

    Attributes:
        xpoint_range: The range the locator's quote was found at.
        confidence: How much evidence backed the match; compare against a
            :class:`~xpoint_cfi.text_anchor.MatchConfidence` member to reject weak ones.
    """

    xpoint_range: XPointRange
    confidence: MatchConfidence


# --------------------------------------------------------------------------------------
# xpointer -> locator
# --------------------------------------------------------------------------------------


def _as_xpoint(value: XPoint | str) -> XPoint:
    return XPoint.parse(value) if isinstance(value, str) else value


def xpoint_to_locator(book: EpubMap, xpoint: XPoint | str) -> Locator:
    """Build a Readium locator for a single KOReader position.

    The locator carries an empty ``text.highlight`` with context on either side, which
    is how a caret rather than a selection is expressed.

    Raises:
        XPointParseError: if ``xpoint`` is a string that does not parse.
        ResolutionError: if the position does not resolve against the book.
    """
    point = _as_xpoint(xpoint)
    return _build_locator(book, point, point, collapsed=True)


def xpoint_range_to_locator(book: EpubMap, start: XPoint | str, end: XPoint | str) -> Locator:
    """Build a Readium locator for a KOReader highlight range.

    ``text.highlight`` is the range's text, read out of the document exactly as
    :func:`~xpoint_cfi.verify.verify_range` reads it.

    Raises:
        XPointParseError: if either end is a string that does not parse, or the range is
            reversed.
        ResolutionError: if either end does not resolve against the book.
    """
    rng = XPointRange(start=_as_xpoint(start), end=_as_xpoint(end))
    return _build_locator(book, rng.start, rng.end, collapsed=False)


def _build_locator(book: EpubMap, start: XPoint, end: XPoint, *, collapsed: bool) -> Locator:
    spine_index = start.doc_fragment_index
    node = book.doc(spine_index)
    start_bound = resolve_bound(node, start, is_end=False)

    resource_text = node.extract_text(None, None)
    start_offset = len(node.extract_text(None, start_bound))

    if collapsed:
        end_bound = start_bound
        highlight = ""
        after = resource_text[start_offset : start_offset + CONTEXT_CHARS]
    elif end.doc_fragment_index == spine_index:
        end_bound = resolve_bound(node, end, is_end=True)
        end_offset = len(node.extract_text(None, end_bound))
        highlight = resource_text[start_offset:end_offset]
        after = resource_text[end_offset : end_offset + CONTEXT_CHARS]
    else:
        end_node = book.doc(end.doc_fragment_index)
        end_bound = resolve_bound(end_node, end, is_end=True)
        highlight = extract_between(book, start, end)
        end_text = end_node.extract_text(None, None)
        end_offset = len(end_node.extract_text(None, end_bound))
        after = end_text[end_offset : end_offset + CONTEXT_CHARS]

    before = resource_text[max(0, start_offset - CONTEXT_CHARS) : start_offset]
    enclosing = (
        _common_ancestor(start_bound[0], end_bound[0])
        if end.doc_fragment_index == spine_index
        else start_bound[0]
    )

    return Locator(
        href=_locator_href(book, spine_index),
        type=book.spine_media_type(spine_index),
        locations=LocatorLocations(
            progression=start_offset / len(resource_text) if resource_text else 0.0,
            css_selector=selector_for_element(node.root, enclosing),
        ),
        text=LocatorText(before=before, highlight=highlight, after=after),
    )


def _locator_href(book: EpubMap, spine_index: int) -> str:
    """Return the spine item's path as the URI a locator's ``href`` must be.

    :meth:`~xpoint_cfi.epub_map.EpubMap.spine_href` reports the *decoded* archive path,
    because that is what reads bytes out of the zip. A Readium ``href`` is a URI, so a
    name holding a space or a non-ASCII character has to be percent-encoded before it
    goes into a locator — ``OEBPS/ch 1.xhtml`` is not a URI reference, and a navigator
    resolving it against the publication's base would mangle it. Path separators stay
    literal.
    """
    return urllib.parse.quote(book.spine_href(spine_index), safe="/")


def _common_ancestor(first: _Element, second: _Element) -> _Element:
    """Return the deepest element containing both ``first`` and ``second``."""
    if first is second:
        return first
    chain: list[_Element] = []
    node: _Element | None = first
    while node is not None:
        chain.append(node)
        node = node.getparent()
    ancestors = {id(element) for element in chain}
    node = second
    while node is not None:
        if id(node) in ancestors:
            return node
        node = node.getparent()
    return chain[-1]


# --------------------------------------------------------------------------------------
# locator -> xpointer range
# --------------------------------------------------------------------------------------


def locator_to_xpoint_range(book: EpubMap, locator: Locator | Mapping[str, object]) -> LocatorMatch:
    """Resolve a Readium locator back to a KOReader :class:`~xpoint_cfi.xpoint.XPointRange`.

    ``locator`` may be a :class:`Locator` or the Readium JSON mapping. The
    ``cssSelector`` narrows *where the match may land* to one element's subtree; a
    selector that names nothing (or that uses syntax outside the supported subset)
    falls back to the whole ``<body>``, since a locator is still resolvable by its quote
    alone. The quote is then located by :func:`~xpoint_cfi.text_anchor.find_quote` and
    mapped back onto text nodes and code-point offsets.

    The search itself always runs over the whole resource, because ``text.before`` and
    ``text.after`` reach past the element the selector names — a selector that lands
    exactly on the quote's own paragraph would otherwise leave the contexts with nothing
    to confirm against.

    A match weaker than :attr:`~xpoint_cfi.text_anchor.MatchConfidence.BOTH_CONTEXTS` is
    searched for again over the resource text without its block separators, which is the
    text a browser's DOM holds, and the stronger of the two is kept.

    Returns:
        A :class:`LocatorMatch` holding the range and the confidence it was found with.

    Raises:
        ResolutionError: if ``href`` names no spine item, or the quote cannot be found
            in the scope even approximately.
    """
    parsed = locator if isinstance(locator, Locator) else Locator.from_dict(locator)
    spine_index = _spine_index_for_href(book, parsed.href)
    node = book.doc(spine_index)
    scope = _scope_element(node, parsed.locations.css_selector)

    segments, match = _best_match(_segments(node, node.body), scope, parsed.text)

    start = _xpoint_at(node, spine_index, segments, match.start, is_end=False)
    end = (
        start
        if match.end == match.start
        else _xpoint_at(node, spine_index, segments, match.end, is_end=True)
    )
    return LocatorMatch(xpoint_range=XPointRange(start=start, end=end), confidence=match.confidence)


def _best_match(
    segments: tuple[_Segment, ...], scope: _Element, text: LocatorText
) -> tuple[tuple[_Segment, ...], QuoteMatch]:
    """Search with the block separators, then without them unless that already settled it.

    Returns the segments the kept match's offsets index into. When neither search finds
    the quote, the error from the first — the text the library itself extracts — is raised.
    """
    unseparated = tuple(segment for segment in segments if segment.elem is not None)
    attempts = (segments,) if len(unseparated) == len(segments) else (segments, unseparated)
    best: tuple[tuple[_Segment, ...], QuoteMatch] | None = None
    failure: ResolutionError | None = None
    for attempt in attempts:
        try:
            match = _search(attempt, scope, text)
        except ResolutionError as exc:
            failure = failure or exc
            continue
        if best is None or match.confidence > best[1].confidence:
            best = (attempt, match)
        if match.confidence is MatchConfidence.BOTH_CONTEXTS:
            break
    if best is None:
        assert failure is not None
        raise failure
    return best


def _search(segments: tuple[_Segment, ...], scope: _Element, text: LocatorText) -> QuoteMatch:
    """Find a locator's quote in the text ``segments`` flatten to, within ``scope``."""
    return find_quote(
        "".join(segment.text for segment in segments),
        text.highlight or "",
        text.before or "",
        text.after or "",
        within=_scope_window(segments, scope),
    )


def _spine_index_for_href(book: EpubMap, href: str) -> int:
    """Return the 1-based spine index the locator's ``href`` names.

    Matching is progressively more forgiving — the exact container path first, then the
    path with any fragment, query and leading slash removed, then the bare filename —
    because a reader's manifest may express the same resource relative to a different
    base than :meth:`~xpoint_cfi.epub_map.EpubMap.spine_href` does. Each of those steps
    tries both the percent-decoded and the literal form of the href (see
    :func:`_href_forms`).

    Raises:
        ResolutionError: if no spine item matches.
    """
    candidates = [book.spine_href(index) for index in range(1, book.spine_count + 1)]
    if href in candidates:
        return candidates.index(href) + 1

    forms = _href_forms(href)
    for form in forms:
        for index, candidate in enumerate(candidates, start=1):
            if candidate == form or candidate.endswith("/" + form):
                return index

    for form in forms:
        name = form.rsplit("/", 1)[-1]
        matches = [
            index
            for index, candidate in enumerate(candidates, start=1)
            if candidate.rsplit("/", 1)[-1] == name
        ]
        if len(matches) == 1:
            return matches[0]
    raise ResolutionError(href, "href does not name exactly one spine item")


def _href_forms(href: str) -> list[str]:
    """Return the container paths ``href`` could name, percent-decoded first.

    A locator's ``href`` is a URI, so a spine file whose name holds a space or a
    non-ASCII character arrives percent-encoded (``chapter%201.xhtml``) while
    :meth:`~xpoint_cfi.epub_map.EpubMap.spine_href` reports the decoded archive path.
    Decoding is therefore tried first. The literal form is kept as a fallback for the
    lenient producer that sends an already-decoded path — and for the pathological
    archive whose entry name really does contain a percent sign.
    """
    trimmed = href.split("#", 1)[0].split("?", 1)[0].lstrip("/")
    decoded = urllib.parse.unquote(trimmed)
    return [decoded] if decoded == trimmed else [decoded, trimmed]


def _scope_element(node: NodeMap, selector: str | None) -> _Element:
    """Return the element a locator's selector narrows the search to, defaulting to body."""
    if selector:
        resolved = resolve_selector(node.root, selector)
        if resolved is not None:
            return resolved
    return node.body


def _scope_window(segments: tuple[_Segment, ...], scope: _Element) -> tuple[int, int]:
    """Return the ``(start, end)`` character span ``scope``'s subtree occupies.

    An empty subtree (an element holding no text at all) widens back to the whole
    resource: a selector that narrows to nothing is no better than no selector.
    """
    inside = {id(element) for element in scope.iter()}
    position = 0
    low: int | None = None
    high = 0
    for segment in segments:
        if segment.elem is not None and id(segment.elem) in inside:
            if low is None:
                low = position
            high = position + len(segment.text)
        position += len(segment.text)
    return (0, position) if low is None else (low, high)


@dataclass(frozen=True)
class _Segment:
    """One run of text in the flattened scope, with the chunk coordinate it came from.

    ``elem`` is ``None`` for the ``"\\n"`` separator that text extraction inserts between
    block elements — it belongs to no text node. ``addressable`` is ``False`` for that
    separator and for chunks crengine drops, which no xpointer can name.
    """

    text: str
    elem: _Element | None
    odd_index: int
    addressable: bool


def _segments(node: NodeMap, scope: _Element) -> tuple[_Segment, ...]:
    """Flatten ``scope``'s text into chunk-tagged runs, matching ``extract_text``.

    The concatenated segment texts reproduce what
    :meth:`~xpoint_cfi.epub_map.NodeMap.extract_text` yields for the same subtree,
    block separators included, so an offset found in this text means the same thing as
    an offset into an extracted quote.
    """
    segments: list[_Segment] = []
    countable: dict[int, frozenset[int]] = {}
    previous_block: _Element | None = None
    for elem, chunk in _iter_chunks(node, scope):
        block = node.block_ancestor(elem)
        if previous_block is not None and block is not previous_block:
            segments.append(_Segment(text="\n", elem=None, odd_index=0, addressable=False))
        previous_block = block
        indices = countable.get(id(elem))
        if indices is None:
            indices = frozenset(c.odd_index for c in node.countable_chunks(elem))
            countable[id(elem)] = indices
        segments.append(
            _Segment(
                text=chunk.text,
                elem=elem,
                odd_index=chunk.odd_index,
                addressable=chunk.odd_index in indices,
            )
        )
    return tuple(segments)


def _iter_chunks(node: NodeMap, elem: _Element) -> Iterator[tuple[_Element, Chunk]]:
    """Yield ``(element, chunk)`` for every non-empty chunk under ``elem``, in document order."""
    chunks = node.chunks(elem)
    children = element_children(elem)
    for position, chunk in enumerate(chunks):
        if chunk.text:
            yield elem, chunk
        if position < len(children):
            yield from _iter_chunks(node, children[position])


def _xpoint_at(
    node: NodeMap, spine_index: int, segments: tuple[_Segment, ...], offset: int, *, is_end: bool
) -> XPoint:
    """Map a code-point offset in the flattened scope text back to an :class:`XPoint`.

    A start bound takes the first addressable segment that reaches past ``offset``; an
    end bound takes the last one that begins before it. Offsets landing on a block
    separator or in text crengine drops therefore snap outwards to the nearest position
    an xpointer can actually name, in the direction that keeps the range enclosing.

    Raises:
        ResolutionError: if the scope holds no addressable text at all.
    """
    segment, within = _locate(segments, offset, is_end=is_end)
    elem = segment.elem
    if elem is None:  # pragma: no cover - _locate only returns addressable segments
        raise ResolutionError(f"offset {offset}", "no addressable text in the locator's scope")
    utf16_offset = cp_to_utf16(segment.text, within)
    text_node_index, char_offset = node.cfi_to_text_position(elem, segment.odd_index, utf16_offset)
    return XPoint(
        doc_fragment_index=spine_index,
        xpath=node.xpath_for_element(elem),
        text_node_index=text_node_index,
        char_offset=char_offset,
        has_text_position=True,
    )


def _locate(segments: tuple[_Segment, ...], offset: int, *, is_end: bool) -> tuple[_Segment, int]:
    """Return the addressable segment holding ``offset`` and the offset within it."""
    spans: list[tuple[int, _Segment]] = []
    position = 0
    for segment in segments:
        spans.append((position, segment))
        position += len(segment.text)

    if is_end:
        for start, segment in reversed(spans):
            if segment.addressable and start < offset:
                return segment, min(offset - start, len(segment.text))
        for _, segment in spans:
            if segment.addressable:
                return segment, 0
    else:
        for start, segment in spans:
            if segment.addressable and start + len(segment.text) > offset:
                return segment, max(offset - start, 0)
        for _, segment in reversed(spans):
            if segment.addressable:
                return segment, len(segment.text)

    raise ResolutionError(f"offset {offset}", "no addressable text in the locator's scope")
