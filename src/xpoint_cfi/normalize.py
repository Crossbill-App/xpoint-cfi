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
    differences, both of which keep the map one-to-one with source positions:

    * **The ends are not stripped**, since dropping leading whitespace would silently
      shift every offset. Callers compare against a stripped needle, so a leading or
      trailing space in the haystack is harmless.
    * **NFC is applied per character** rather than over the whole string. A combining
      sequence that spans several source characters therefore stays decomposed. Real
      EPUB text and the KOReader highlight text extracted from it come from the same
      source bytes and share a normalization form, so this only matters for text that
      arrived from elsewhere already decomposed — such a quote falls through to fuzzy
      matching instead of matching exactly.

    Example::

        normalize_with_map("a  b") == ("a b", (0, 1, 3, 4))
    """
    out: list[str] = []
    source: list[int] = []
    in_run = False
    for i, raw in enumerate(text):
        ch = " " if raw == _NBSP else raw
        if ch in _ZERO_WIDTH:
            continue
        if ch.isspace():
            if not in_run:
                out.append(" ")
                source.append(i)
                in_run = True
            continue
        in_run = False
        for composed in unicodedata.normalize("NFC", ch):
            out.append(composed)
            source.append(i)
    source.append(len(text))
    return "".join(out), tuple(source)
