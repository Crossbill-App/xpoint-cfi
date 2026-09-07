"""Tests for quote anchoring: disambiguation by context and the confidence grading."""

from __future__ import annotations

import pytest

from xpoint_cfi import ResolutionError
from xpoint_cfi.text_anchor import MatchConfidence, QuoteMatch, find_quote

_SOFT_HYPHEN = "\u00ad"
_NBSP = "\u00a0"

_TEXT = "The cat sat on the mat. The cat sat on the hat. The end."


def quoted(text: str, match: QuoteMatch) -> str:
    """Return the slice of ``text`` a match denotes, in the source's own characters."""
    return text[match.start : match.end]


# --------------------------------------------------------------------------------------
# Confidence grading
# --------------------------------------------------------------------------------------


def test_unique_quote_with_both_contexts() -> None:
    match = find_quote(_TEXT, "on the mat", before="The cat sat ", after=". The cat")
    assert match.confidence is MatchConfidence.BOTH_CONTEXTS
    assert quoted(_TEXT, match) == "on the mat"


def test_unique_quote_with_one_context() -> None:
    match = find_quote(_TEXT, "The end", before="on the hat. ")
    assert match.confidence is MatchConfidence.ONE_CONTEXT


def test_unique_quote_without_usable_context() -> None:
    match = find_quote(_TEXT, "The end")
    assert match.confidence is MatchConfidence.HIGHLIGHT_ONLY
    assert quoted(_TEXT, match) == "The end"


def test_repeated_phrase_is_disambiguated_by_the_following_context() -> None:
    # "The cat sat" occurs twice; only `after` separates the two.
    match = find_quote(_TEXT, "The cat sat", after="on the hat")
    assert match.confidence is MatchConfidence.ONE_CONTEXT
    assert match.start == _TEXT.index("The cat sat", 1)


def test_repeated_phrase_is_disambiguated_by_the_preceding_context() -> None:
    match = find_quote(_TEXT, "The cat sat", before="on the mat.")
    assert match.start == _TEXT.index("The cat sat", 1)


def test_repeated_phrase_without_context_is_ambiguous_and_takes_the_first() -> None:
    match = find_quote(_TEXT, "The cat sat")
    assert match.confidence is MatchConfidence.AMBIGUOUS
    assert match.start == 0


def test_context_that_fits_neither_occurrence_still_returns_the_first() -> None:
    match = find_quote(_TEXT, "The cat sat", before="nothing like this")
    assert match.confidence is MatchConfidence.AMBIGUOUS
    assert match.start == 0


def test_context_truncated_at_the_start_of_the_text_still_counts() -> None:
    # `before` is longer than everything preceding the quote, as it would be for a
    # locator whose window ran past the start of the resource.
    match = find_quote(_TEXT, "cat sat on the mat", before="Once upon a time. The ")
    assert match.confidence is MatchConfidence.ONE_CONTEXT


# --------------------------------------------------------------------------------------
# Normalization tolerance
# --------------------------------------------------------------------------------------


def test_whitespace_differences_do_not_prevent_a_match() -> None:
    text = "Hello\n   brave   new\tworld."
    match = find_quote(text, "brave new world")
    assert match.confidence is MatchConfidence.HIGHLIGHT_ONLY
    assert quoted(text, match) == "brave   new\tworld"


def test_soft_hyphens_in_the_document_do_not_prevent_a_match() -> None:
    # crengine keeps soft hyphens in the DOM but strips them from exported text, so the
    # quote never carries them and the offsets must still land around the hyphenated run.
    text = f"a hy{_SOFT_HYPHEN}phen{_SOFT_HYPHEN}ated word here"
    match = find_quote(text, "hyphenated word")
    assert quoted(text, match) == f"hy{_SOFT_HYPHEN}phen{_SOFT_HYPHEN}ated word"


def test_no_break_spaces_match_ordinary_spaces() -> None:
    text = f"chapter{_NBSP}one begins"
    match = find_quote(text, "chapter one")
    assert quoted(text, match) == f"chapter{_NBSP}one"


def test_astral_characters_keep_the_offsets_in_code_points() -> None:
    text = "before \U0001f600 quote after"
    match = find_quote(text, "quote")
    assert quoted(text, match) == "quote"
    assert match.start == text.index("quote")


# --------------------------------------------------------------------------------------
# Point anchors
# --------------------------------------------------------------------------------------


def test_empty_highlight_anchors_exactly_where_before_ends() -> None:
    # `before` ends with the space, so the point belongs after it — the two contexts
    # partition the text and the boundary is where `after` begins.
    match = find_quote(_TEXT, "", before="The cat sat ", after="on the mat")
    assert match.start == match.end == _TEXT.index("on the mat")
    assert match.confidence is MatchConfidence.BOTH_CONTEXTS


def test_empty_highlight_anchors_on_the_space_when_before_stops_short_of_it() -> None:
    # The same text one character earlier: `before` has no trailing space, so the point
    # is on the space itself. The two cases must not collapse into one.
    match = find_quote(_TEXT, "", before="The cat sat", after=" on the mat")
    assert match.start == match.end == _TEXT.index(" on the mat")


def test_empty_highlight_with_only_a_leading_context() -> None:
    match = find_quote(_TEXT, "", before="on the hat. ")
    assert match.start == match.end == _TEXT.index("The end")
    assert match.confidence is MatchConfidence.ONE_CONTEXT


def test_point_anchor_survives_a_context_whose_whitespace_differs() -> None:
    # A reader's `before` may not reproduce the document's whitespace exactly; the
    # stripped form still anchors, landing at the end of its last real character.
    match = find_quote("Hello   brave world", "", before="Hello")
    assert match.start == match.end == 5


def test_empty_highlight_with_only_a_following_context() -> None:
    match = find_quote(_TEXT, "", after="The end.")
    assert match.start == match.end == _TEXT.index("The end")


def test_empty_highlight_and_no_context_is_an_error() -> None:
    with pytest.raises(ResolutionError, match="no highlight and no context"):
        find_quote(_TEXT, "")


def test_empty_highlight_with_context_that_is_absent_is_an_error() -> None:
    with pytest.raises(ResolutionError, match="context text not found"):
        find_quote(_TEXT, "", before="nothing like this at all")


# --------------------------------------------------------------------------------------
# Fuzzy fallback
# --------------------------------------------------------------------------------------


def test_quote_with_a_small_edit_falls_back_to_a_fuzzy_match() -> None:
    match = find_quote(_TEXT, "The cot sat on the mat")
    assert match.confidence is MatchConfidence.FUZZY
    assert quoted(_TEXT, match) == "The cat sat on the mat"


def test_quote_that_is_nowhere_near_the_text_is_rejected() -> None:
    with pytest.raises(ResolutionError, match="not found in scope"):
        find_quote(_TEXT, "an entirely unrelated sentence about shipping")


# --------------------------------------------------------------------------------------
# The `within` window
# --------------------------------------------------------------------------------------

# "alpha" and "gamma" belong to the parent, "beta" to the selected child: the scope's
# exclusive end (9) is the source offset of a character that is still in the text.
_NESTED = "alphabetagamma"


def test_a_quote_filling_the_window_exactly_still_matches() -> None:
    match = find_quote(_NESTED, "beta", within=(5, 9))
    assert (match.start, match.end) == (5, 9)
    assert match.confidence is MatchConfidence.HIGHLIGHT_ONLY


def test_a_quote_reaching_past_the_window_end_does_not_match_exactly() -> None:
    # "betag" leaves the scope by one character. It must not come back as an exact
    # match spanning [5, 10); the search stays inside the window.
    match = find_quote(_NESTED, "betag", within=(5, 9))
    assert match.end <= 9
    assert match.confidence is MatchConfidence.FUZZY


def test_a_quote_reaching_before_the_window_start_does_not_match_exactly() -> None:
    match = find_quote(_NESTED, "abeta", within=(5, 9))
    assert match.start >= 5
    assert match.confidence is MatchConfidence.FUZZY


def test_a_window_covering_the_whole_text_matches_to_the_last_character() -> None:
    match = find_quote(_NESTED, "gamma", within=(0, len(_NESTED)))
    assert (match.start, match.end) == (9, 14)


def test_a_point_may_anchor_exactly_on_the_window_boundary() -> None:
    # The boundary itself is a legal caret position even though no character inside the
    # window sits there.
    match = find_quote(_NESTED, "", before="alphabeta", within=(5, 9))
    assert match.start == match.end == 9
