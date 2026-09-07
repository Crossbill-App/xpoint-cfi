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

from bisect import bisect_left
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


def find_quote(
    text: str,
    highlight: str,
    before: str = "",
    after: str = "",
    *,
    within: tuple[int, int] | None = None,
) -> QuoteMatch:
    """Find ``highlight`` in ``text``, using ``before``/``after`` to disambiguate.

    An empty ``highlight`` anchors a zero-length point at the boundary between the two
    contexts, which is how a Readium locator expresses a position rather than a
    selection.

    ``within`` narrows *where the match may land* to a code-point window of ``text``,
    without narrowing ``text`` itself. That distinction matters: a locator's CSS
    selector often names the very element the quote sits in, whose text stops exactly
    where the contexts begin — searching only that element would leave ``before`` and
    ``after`` with nothing to confirm against, and every match would come back as
    :attr:`MatchConfidence.HIGHLIGHT_ONLY`.

    Args:
        text: The text to search, in its raw document form.
        highlight: The quote to find.
        before: Text that immediately precedes the quote in the source document.
        after: Text that immediately follows it.
        within: Optional ``(start, end)`` window of ``text`` the match must fall inside.

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
    window = _normalized_window(source, len(haystack), within)

    if not needle:
        return _anchor_point(haystack, source, window, lead, trail)

    occurrences = _occurrences(haystack, needle, window, len(needle))
    if occurrences:
        index, confidence = _best_occurrence(haystack, occurrences, len(needle), lead, trail)
        return QuoteMatch(source[index], source[index + len(needle)], confidence)
    return _fuzzy_match(haystack, source, window, needle)


def _normalized_window(
    source: tuple[int, ...], length: int, within: tuple[int, int] | None
) -> tuple[int, int]:
    """Translate a source-coordinate window into normalized-string coordinates.

    ``within``'s end is **exclusive**, so both bounds use a left-bound insertion: the
    returned end is the first normalized position whose source index has reached the
    window's end, and every position below it came from inside the window. A right-bound
    insertion would admit the character sitting exactly at that exclusive end — the
    parent's tail or the next sibling's text, one character outside the element a CSS
    selector named.

    The end stays a legal *position* rather than a forbidden one, so a point anchor may
    still sit exactly on the boundary; it is only characters beyond it that are out.
    """
    if within is None:
        return (0, length)
    start, end = within
    return (bisect_left(source, start, hi=length), bisect_left(source, end, hi=length))


# --------------------------------------------------------------------------------------
# Exact matching
# --------------------------------------------------------------------------------------


def _occurrences(haystack: str, needle: str, window: tuple[int, int], length: int) -> list[int]:
    """Return the start offsets of every occurrence of ``needle`` fitting in ``window``.

    Capped at :data:`_MAX_OCCURRENCES`: a quote appearing more often than that is
    degenerate (a single space, one letter) and no amount of context makes it
    trustworthy.
    """
    low, high = window
    found: list[int] = []
    index = haystack.find(needle, low)
    while index != -1 and index + length <= high and len(found) < _MAX_OCCURRENCES:
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


def _anchor_point(
    haystack: str, source: tuple[int, ...], window: tuple[int, int], lead: str, trail: str
) -> QuoteMatch:
    """Anchor a zero-length position at the boundary between ``lead`` and ``trail``.

    The point is placed where ``lead`` ends (or, with no ``lead``, where ``trail``
    begins), so whitespace separating the two contexts falls on the ``lead`` side.

    The context is searched for across the whole text and only the resulting *point* is
    required to fall inside ``window`` — a point's context routinely reaches past the
    element the selector named.
    """
    whole = (0, len(haystack))
    if lead:
        candidates = [index + len(lead) for index in _occurrences(haystack, lead, whole, len(lead))]
    elif trail:
        candidates = _occurrences(haystack, trail, whole, len(trail))
    else:
        raise ResolutionError("<empty quote>", "no highlight and no context to anchor to")
    low, high = window
    candidates = [index for index in candidates if low <= index <= high]
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


def _fuzzy_match(
    haystack: str, source: tuple[int, ...], window: tuple[int, int], needle: str
) -> QuoteMatch:
    """Return the part of ``window`` most similar to ``needle``, or raise.

    The longest block the two strings share fixes the alignment; the candidate is then
    taken to be the quote's own length around it and scored with a full diff. This
    recovers quotes that drifted by an edit or two, and refuses anything less alike than
    :data:`_MIN_SIMILARITY`.
    """
    low, high = window
    scoped = haystack[low:high]
    blocks = SequenceMatcher(None, scoped, needle, autojunk=False).get_matching_blocks()
    longest = max(blocks, key=lambda block: block.size)
    if longest.size == 0:
        raise ResolutionError(_excerpt(needle), "quote not found in scope")

    start = max(0, longest.a - longest.b)
    end = min(len(scoped), start + len(needle))
    similarity = SequenceMatcher(None, scoped[start:end], needle, autojunk=False).ratio()
    if similarity < _MIN_SIMILARITY:
        raise ResolutionError(
            _excerpt(needle), f"quote not found in scope (best match {similarity:.2f})"
        )
    return QuoteMatch(source[low + start], source[low + end], MatchConfidence.FUZZY)


def _excerpt(text: str, limit: int = 60) -> str:
    """Shorten ``text`` for an error message."""
    return text if len(text) <= limit else text[: limit - 1] + "…"
