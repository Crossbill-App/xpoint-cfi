"""Text normalization shared by every comparison the library makes.

Two engines render the same EPUB text slightly differently: crengine keeps soft hyphens
in its DOM but strips them from exported highlight text, whitespace is collapsed in
different places, and no-break spaces come and go. Every comparison in this library
therefore runs both sides through :func:`normalize_for_comparison` first.

:func:`normalize_with_map` performs the same folding while remembering where each output
character came from, which is what text anchoring needs to turn a match position back
into a document position.
"""

from __future__ import annotations

import unicodedata

__all__ = [
    "normalize_for_comparison",
    "normalize_whitespace",
    "normalize_with_map",
]

# Characters dropped entirely before comparison: soft hyphen and zero-width marks.
_ZERO_WIDTH = frozenset({"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"})
_NBSP = "\u00a0"


def normalize_whitespace(s: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return " ".join(s.split())


def normalize_for_comparison(s: str) -> str:
    """Normalize text so cosmetic engine differences don't cause mismatches.

    NFC-normalize, drop soft hyphens and zero-width characters (crengine keeps soft
    hyphens in its DOM text but strips them from exported highlight text), turn no-break
    spaces into ordinary spaces, then collapse whitespace runs and strip.
    """
    s = unicodedata.normalize("NFC", s)
    s = s.replace(_NBSP, " ")
    s = "".join(ch for ch in s if ch not in _ZERO_WIDTH)
    return normalize_whitespace(s)


def normalize_with_map(text: str) -> tuple[str, tuple[int, ...]]:
    """Normalize ``text`` and return ``(normalized, source)`` with a back-map.

    ``source[i]`` is the code-point index in ``text`` that produced normalized position
    ``i``; a collapsed whitespace run maps to the index of its **first** character. A
    sentinel ``source[len(normalized)] == len(text)`` is always appended so that every
    normalized offset ``0..len(normalized)`` has a source counterpart.

    The folding matches :func:`normalize_for_comparison` with two deliberate
    differences, both of which keep the map usable as a back-map:

    * **The ends are not stripped**, since dropping leading whitespace would silently
      shift every offset. Callers compare against a stripped needle, so a leading or
      trailing space in the haystack is harmless.
    * **NFC is applied one combining sequence at a time** rather than over the whole
      string, so that every output character has a source position to point at. A
      decomposed ``e`` + combining acute composes to ``é`` mapped at the ``e``; the
      offset *between* the two source characters is not expressible, which is correct —
      they are one grapheme, and no range boundary belongs inside it.

    Example::

        normalize_with_map("a  b") == ("a b", (0, 1, 3, 4))
    """
    out: list[str] = []
    source: list[int] = []
    in_run = False
    index = 0
    length = len(text)
    while index < length:
        raw = text[index]
        ch = " " if raw == _NBSP else raw
        if ch in _ZERO_WIDTH:
            index += 1
            continue
        if ch.isspace():
            if not in_run:
                out.append(" ")
                source.append(index)
                in_run = True
            index += 1
            continue
        in_run = False
        sequence, next_index = _combining_sequence(text, index, ch)
        for composed in unicodedata.normalize("NFC", sequence):
            out.append(composed)
            source.append(index)
        index = next_index
    source.append(length)
    return "".join(out), tuple(source)


def _combining_sequence(text: str, start: int, base: str) -> tuple[str, int]:
    """Return the combining sequence beginning at ``start`` and the index just past it.

    The sequence is ``base`` plus every following combining mark, with zero-width
    characters skipped as everywhere else. ``base`` is passed in already folded (a
    no-break space has become an ordinary space by the time this is called).
    """
    parts = [base]
    index = start + 1
    while index < len(text):
        ch = text[index]
        if ch in _ZERO_WIDTH:
            index += 1
            continue
        if not unicodedata.category(ch).startswith("M"):
            break
        parts.append(ch)
        index += 1
    return "".join(parts), index
