"""crengine whitespace-collapse semantics for xpointer offset fidelity.

KOReader stores highlight positions as offsets into **crengine's** DOM text, not into
the raw XHTML source. When crengine parses XHTML it runs ``PreProcessXmlString`` over
each text node, which — for normal (non ``white-space:pre``) content — replaces every
run of the four ASCII whitespace characters space, tab, carriage return and line feed
with a single space. It performs **no leading/trailing trim**, and it does **not** touch
other Unicode spaces (NBSP ``\\u00a0``, thin spaces, …) — those survive verbatim.

An xpointer ``text().OFFSET`` therefore indexes this *collapsed* text, whereas an EPUB
CFI character offset indexes the raw source text (in UTF-16 code units). This module is
the pure, lxml-free bridge between the two coordinate spaces. Its semantics are frozen:
they are validated char-for-char against a real-book highlight corpus and must not drift.

All functions operate on Python ``str`` (Unicode code points); UTF-16 conversion happens
separately in :mod:`xpoint_cfi.epub_map`.
"""

from __future__ import annotations

# The exact set of characters crengine's PreProcessXmlString collapses. Deliberately just
# the four ASCII whitespace characters — NBSP and other Unicode spaces are NOT collapsed.
COLLAPSIBLE = " \t\r\n"

__all__ = [
    "COLLAPSIBLE",
    "collapse",
    "collapse_with_map",
    "is_countable",
    "raw_to_collapsed",
]


def collapse(text: str) -> str:
    """Return ``text`` with every run of :data:`COLLAPSIBLE` characters folded to one space.

    No leading/trailing trim is performed and non-:data:`COLLAPSIBLE` characters (including
    NBSP and other Unicode spaces) are preserved verbatim. ``O(n)``.
    """
    out: list[str] = []
    in_run = False
    for ch in text:
        if ch in COLLAPSIBLE:
            if not in_run:
                out.append(" ")
                in_run = True
        else:
            out.append(ch)
            in_run = False
    return "".join(out)


def collapse_with_map(text: str) -> tuple[str, tuple[int, ...]]:
    """Collapse ``text`` and return ``(collapsed, pos)`` with a raw-index back-map.

    ``pos[i]`` is the raw (code-point) index of the character that begins collapsed
    position ``i``; a whitespace run maps to the raw index of its **first** character. A
    final sentinel ``pos[len(collapsed)] == len(text)`` is always appended so that every
    collapsed offset ``0..len(collapsed)`` has a raw counterpart.

    Example::

        collapse_with_map("a  b") == ("a b", (0, 1, 3, 4))

    ``O(n)``.
    """
    out: list[str] = []
    pos: list[int] = []
    in_run = False
    for i, ch in enumerate(text):
        if ch in COLLAPSIBLE:
            if not in_run:
                out.append(" ")
                pos.append(i)
                in_run = True
        else:
            out.append(ch)
            pos.append(i)
            in_run = False
    pos.append(len(text))
    return "".join(out), tuple(pos)


def raw_to_collapsed(text: str, raw_offset: int) -> int:
    """Return how many collapsed positions the raw prefix ``text[:raw_offset]`` produces.

    Equivalent to ``len(collapse(text[:raw_offset]))`` but computed in a single ``O(n)``
    pass. Consistent with :func:`collapse_with_map`: for every ``i`` in
    ``range(len(collapsed) + 1)``, ``raw_to_collapsed(text, pos[i]) == i``.
    """
    count = 0
    in_run = False
    limit = min(raw_offset, len(text))
    for ch in text[:limit]:
        if ch in COLLAPSIBLE:
            if not in_run:
                count += 1
                in_run = True
        else:
            count += 1
            in_run = False
    return count


def is_countable(text: str) -> bool:
    """Return ``True`` when ``text`` has at least one non-:data:`COLLAPSIBLE` character.

    crengine drops whitespace-only text nodes, so an xpointer ``text()[N]`` counts only
    chunks for which this predicate holds.
    """
    return any(ch not in COLLAPSIBLE for ch in text)
