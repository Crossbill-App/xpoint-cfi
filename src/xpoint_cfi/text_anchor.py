"""Locate a quote inside a body of text, Hypothesis-style, with a confidence grade.

A Readium locator anchors a position by *what the text says* rather than by where it
sat in the DOM: ``text.highlight`` is the quote and ``text.before`` / ``text.after`` are
the surrounding context that disambiguates it when the same phrase occurs more than
once. This module is the pure string half of resolving such an anchor — it knows
nothing about EPUBs, elements or offsets, and returns code-point offsets into the text
it was given.

Every comparison runs through :mod:`xpoint_cfi.normalize`, so hyphenation, zero-width
marks and whitespace differences between crengine's DOM text and a reader's DOM text
never decide a match. :func:`normalize_with_map`'s back-map is what lets a match found
in normalized space be reported in source coordinates.

:class:`MatchConfidence` grades how much evidence backed the match. It is an
:class:`~enum.IntEnum` so callers can set a floor — ``if match.confidence <
MatchConfidence.HIGHLIGHT_ONLY: ...`` — rather than enumerate the cases.
"""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher
from enum import IntEnum

from .exceptions import ResolutionError
from .normalize import normalize_for_comparison, normalize_with_map

__all__ = [
    "MatchConfidence",
    "QuoteMatch",
    "find_quote",
]

# A quote occurring more often than this is treated as unanchorable noise rather than
# scored occurrence by occurrence; it only happens for degenerate quotes (a single
# space, one letter) that no amount of context makes trustworthy anyway.
_MAX_OCCURRENCES = 1000

# How much of a fuzzy candidate window must survive a diff against the quote before the
# match is offered at all. Below this the quote is treated as absent.
_MIN_SIMILARITY = 0.75


class MatchConfidence(IntEnum):
    """How much evidence backed a quote match, weakest first.

    Ordered so that callers can reject weak matches with a single comparison:

    * :attr:`FUZZY` — the quote was not found verbatim; the offsets come from the best
      approximate window and may be off by characters at either end.
    * :attr:`AMBIGUOUS` — the quote occurs several times and neither context settled
      which one; the first occurrence was taken.
    * :attr:`HIGHLIGHT_ONLY` — the quote occurs exactly once, but no context confirmed
      it (none was supplied, or what was supplied did not fit).
    * :attr:`ONE_CONTEXT` — the quote matched with either ``before`` or ``after``
      abutting it.
    * :attr:`BOTH_CONTEXTS` — the quote matched with both contexts abutting it. This is
      what a locator produced from the same document should always come back as.
    """

    FUZZY = 0
    AMBIGUOUS = 1
    HIGHLIGHT_ONLY = 2
    ONE_CONTEXT = 3
    BOTH_CONTEXTS = 4


@dataclass(frozen=True)
class QuoteMatch:
    """Where a quote was found, in code-point offsets into the searched text.

    Attributes:
        start: Offset of the first character of the match.
        end: Offset just past the last character; equal to ``start`` for a point anchor
            (a locator with no highlight).
        confidence: How much evidence backed the match.
    """

    start: int
    end: int
    confidence: MatchConfidence


def find_quote(text: str, highlight: str, before: str = "", after: str = "") -> QuoteMatch:
    """Find ``highlight`` in ``text``, using ``before``/``after`` to disambiguate.

    An empty ``highlight`` anchors a zero-length point at the boundary between the two
    contexts, which is how a Readium locator expresses a position rather than a
    selection.

    Args:
        text: The text to search, in its raw document form.
        highlight: The quote to find.
        before: Text that immediately precedes the quote in the source document.
        after: Text that immediately follows it.

    Returns:
        A :class:`QuoteMatch` with offsets into ``text`` and a confidence grade.

    Raises:
        ResolutionError: if the quote is absent and no approximate window is similar
            enough, or if a point anchor has no context to anchor to.
    """
    haystack, source = normalize_with_map(text)
    needle = normalize_for_comparison(highlight)
    lead = normalize_for_comparison(before)
    trail = normalize_for_comparison(after)

    if not needle:
        return _anchor_point(haystack, source, lead, trail)

    occurrences = _occurrences(haystack, needle)
    if occurrences:
        index, confidence = _best_occurrence(haystack, occurrences, len(needle), lead, trail)
        return QuoteMatch(source[index], source[index + len(needle)], confidence)
    return _fuzzy_match(haystack, source, needle)


# --------------------------------------------------------------------------------------
# Exact matching
# --------------------------------------------------------------------------------------


def _occurrences(haystack: str, needle: str) -> list[int]:
    """Return the start offsets of every occurrence of ``needle``, capped for sanity."""
    found: list[int] = []
    index = haystack.find(needle)
    while index != -1 and len(found) < _MAX_OCCURRENCES:
        found.append(index)
        index = haystack.find(needle, index + 1)
    return found


def _lead_abuts(prefix: str, lead: str) -> bool:
    """Return ``True`` when ``lead`` ends where ``prefix`` does.

    Both sides are already normalized and stripped, so the whitespace that separates
    the context from the quote in the source is ignored. Either side may have been
    truncated — the context by the producer's window, the prefix by the start of the
    resource — so a match at the shorter length counts.
    """
    if not lead:
        return False
    prefix = prefix.rstrip()
    return prefix.endswith(lead) or (bool(prefix) and lead.endswith(prefix))


def _trail_abuts(suffix: str, trail: str) -> bool:
    """Return ``True`` when ``trail`` starts where ``suffix`` does (see :func:`_lead_abuts`)."""
    if not trail:
        return False
    suffix = suffix.lstrip()
    return suffix.startswith(trail) or (bool(suffix) and trail.startswith(suffix))


def _best_occurrence(
    haystack: str, occurrences: list[int], length: int, lead: str, trail: str
) -> tuple[int, MatchConfidence]:
    """Pick the occurrence the most context agrees with and grade the result."""
    best_index = occurrences[0]
    best_score = -1
    for index in occurrences:
        score = int(_lead_abuts(haystack[:index], lead)) + int(
            _trail_abuts(haystack[index + length :], trail)
        )
        if score > best_score:
            best_index, best_score = index, score
        if best_score == 2:
            break

    if best_score == 2:
        return best_index, MatchConfidence.BOTH_CONTEXTS
    if best_score == 1:
        return best_index, MatchConfidence.ONE_CONTEXT
    if len(occurrences) == 1:
        return best_index, MatchConfidence.HIGHLIGHT_ONLY
    return best_index, MatchConfidence.AMBIGUOUS


# --------------------------------------------------------------------------------------
# Point anchors and fuzzy fallback
# --------------------------------------------------------------------------------------


def _anchor_point(haystack: str, source: tuple[int, ...], lead: str, trail: str) -> QuoteMatch:
    """Anchor a zero-length position at the boundary between ``lead`` and ``trail``.

    The point is placed where ``lead`` ends (or, with no ``lead``, where ``trail``
    begins), so whitespace separating the two contexts falls on the ``lead`` side.
    """
    if lead:
        candidates = [index + len(lead) for index in _occurrences(haystack, lead)]
    elif trail:
        candidates = _occurrences(haystack, trail)
    else:
        raise ResolutionError("<empty quote>", "no highlight and no context to anchor to")
    if not candidates:
        raise ResolutionError(_excerpt(lead or trail), "context text not found in scope")

    confirmed = [
        index
        for index in candidates
        if (_trail_abuts(haystack[index:], trail) if lead else _lead_abuts(haystack[:index], lead))
    ]
    if confirmed:
        return QuoteMatch(source[confirmed[0]], source[confirmed[0]], MatchConfidence.BOTH_CONTEXTS)
    confidence = MatchConfidence.ONE_CONTEXT if len(candidates) == 1 else MatchConfidence.AMBIGUOUS
    return QuoteMatch(source[candidates[0]], source[candidates[0]], confidence)


def _fuzzy_match(haystack: str, source: tuple[int, ...], needle: str) -> QuoteMatch:
    """Return the window of ``haystack`` most similar to ``needle``, or raise.

    The longest block the two strings share fixes the window's alignment; the window is
    then taken to be the quote's own length around it and scored with a full diff. This
    recovers quotes that drifted by an edit or two, and refuses anything less alike than
    :data:`_MIN_SIMILARITY`.
    """
    blocks = SequenceMatcher(None, haystack, needle, autojunk=False).get_matching_blocks()
    longest = max(blocks, key=lambda block: block.size)
    if longest.size == 0:
        raise ResolutionError(_excerpt(needle), "quote not found in scope")

    start = max(0, longest.a - longest.b)
    end = min(len(haystack), start + len(needle))
    similarity = SequenceMatcher(None, haystack[start:end], needle, autojunk=False).ratio()
    if similarity < _MIN_SIMILARITY:
        raise ResolutionError(
            _excerpt(needle), f"quote not found in scope (best match {similarity:.2f})"
        )
    return QuoteMatch(source[start], source[end], MatchConfidence.FUZZY)


def _excerpt(text: str, limit: int = 60) -> str:
    """Shorten ``text`` for an error message."""
    return text if len(text) <= limit else text[: limit - 1] + "…"
