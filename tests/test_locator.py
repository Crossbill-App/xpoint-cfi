"""Tests for Readium locator output and the reverse text-anchored conversion.

The round-trip property these exercise is the one the ticket cares about: an xpointer
range turned into a locator and back must denote the same text, through structures that
make the mapping hard — astral characters, comments splitting a paragraph, soft hyphens,
and repeated phrases that only ``before``/``after`` can tell apart.
"""

from __future__ import annotations

import pytest
from conftest import build_epub, xhtml_doc

from xpoint_cfi import (
    EpubMap,
    Locator,
    LocatorLocations,
    LocatorText,
    MatchConfidence,
    ResolutionError,
    XPointRange,
    locator_to_xpoint_range,
    normalize_for_comparison,
    xpoint_range_to_locator,
    xpoint_to_locator,
)
from xpoint_cfi.text_range import extract_between

_SOFT_HYPHEN = "\u00ad"
_EMOJI = "\U0001f600"

_CHAP1 = (
    '<div id="intro">'
    "<p>Hello <i>brave</i> new <b>world</b>.</p>"
    "<p>Before<!--a comment-->after the comment.</p>"
    f"<p>An emoji {_EMOJI} sits mid sentence.</p>"
    f"<p>A hy{_SOFT_HYPHEN}phen{_SOFT_HYPHEN}ated word appears here.</p>"
    "<p>The cat sat on the mat.</p>"
    "<p>The cat sat on the hat.</p>"
    "</div>"
)
_CHAP2 = "<h1>Two</h1><p>Second chapter body.</p>"


@pytest.fixture
def book() -> EpubMap:
    return EpubMap.from_bytes(
        build_epub(
            {
                "chap1.xhtml": xhtml_doc("C1", _CHAP1),
                "chap2.xhtml": xhtml_doc("C2", _CHAP2),
            }
        )
    )


def xp(path: str) -> str:
    return f"/body/DocFragment[1]/body/div/{path}"


def round_trip(book: EpubMap, start: str, end: str) -> tuple[str, MatchConfidence]:
    """Convert a range to a locator and back, returning the text it lands on."""
    locator = xpoint_range_to_locator(book, start, end)
    match = locator_to_xpoint_range(book, locator)
    text = extract_between(book, match.xpoint_range.start, match.xpoint_range.end)
    return text, match.confidence


# --------------------------------------------------------------------------------------
# Locator output
# --------------------------------------------------------------------------------------


def test_locator_carries_the_resource_href_and_media_type(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[1]/text()[3].1"))
    assert locator.href == "OEBPS/chap1.xhtml"
    assert locator.type == "application/xhtml+xml"


def test_highlight_is_the_ranges_text(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[1]/text()[3].1"))
    assert locator.text.highlight == "Hello brave new world."


def test_context_windows_surround_the_quote(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[5]/text().4"), xp("p[5]/text().7"))
    assert locator.text.highlight == "cat"
    assert locator.text.before is not None and locator.text.before.endswith("The ")
    assert locator.text.after is not None and locator.text.after.startswith(" sat on the mat.")


def test_context_windows_are_bounded(book: EpubMap) -> None:
    from xpoint_cfi.locator import CONTEXT_CHARS

    locator = xpoint_range_to_locator(book, xp("p[5]/text().4"), xp("p[5]/text().7"))
    assert len(locator.text.before or "") <= CONTEXT_CHARS
    assert len(locator.text.after or "") <= CONTEXT_CHARS


def test_css_selector_names_the_enclosing_element(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[5]/text().0"), xp("p[5]/text().3"))
    assert locator.locations.css_selector == "#intro > p:nth-child(5)"


def test_css_selector_of_a_cross_element_range_is_the_common_ancestor(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[2]/text().3"))
    assert locator.locations.css_selector == "#intro"


def test_progression_grows_through_the_resource(book: EpubMap) -> None:
    first = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[1]/text().5"))
    last = xpoint_range_to_locator(book, xp("p[6]/text().0"), xp("p[6]/text().3"))
    assert first.locations.progression == 0.0
    last_progression = last.locations.progression
    assert last_progression is not None
    assert 0.0 < last_progression < 1.0


def test_single_point_locator_has_an_empty_highlight_between_its_contexts(
    book: EpubMap,
) -> None:
    locator = xpoint_to_locator(book, xp("p[1]/text().6"))
    assert locator.text.highlight == ""
    assert locator.text.before is not None and locator.text.before.endswith("Hello ")
    assert locator.text.after is not None and locator.text.after.startswith("brave")


def test_locator_serializes_to_the_readium_json_shape(book: EpubMap) -> None:
    payload = xpoint_range_to_locator(book, xp("p[5]/text().4"), xp("p[5]/text().7")).to_dict()
    assert payload["href"] == "OEBPS/chap1.xhtml"
    assert payload["type"] == "application/xhtml+xml"
    assert set(payload["locations"]) == {"progression", "cssSelector"}  # pyright: ignore[reportArgumentType]
    assert set(payload["text"]) == {"before", "highlight", "after"}  # pyright: ignore[reportArgumentType]
    assert "title" not in payload


def test_locator_dict_round_trips(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[5]/text().4"), xp("p[5]/text().7"))
    assert Locator.from_dict(locator.to_dict()) == locator


def test_locator_from_dict_tolerates_missing_and_ill_typed_fields() -> None:
    locator = Locator.from_dict({"href": "a.xhtml", "locations": 7, "text": {"highlight": 3}})
    assert locator.href == "a.xhtml"
    assert locator.type == "application/xhtml+xml"
    assert locator.locations == LocatorLocations()
    assert locator.text == LocatorText()


def test_locator_from_dict_requires_an_href() -> None:
    with pytest.raises(ResolutionError, match="no 'href'"):
        Locator.from_dict({"type": "application/xhtml+xml"})


# --------------------------------------------------------------------------------------
# Reverse conversion
# --------------------------------------------------------------------------------------


def test_round_trip_returns_the_same_text(book: EpubMap) -> None:
    text, confidence = round_trip(book, xp("p[5]/text().0"), xp("p[5]/text().23"))
    assert normalize_for_comparison(text) == "The cat sat on the mat."
    assert confidence is MatchConfidence.BOTH_CONTEXTS


def test_quote_at_the_start_of_the_resource_has_only_a_following_context(
    book: EpubMap,
) -> None:
    # Nothing precedes it, so `before` is empty and only `after` can confirm the match.
    locator = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[1]/text()[3].1"))
    assert locator.text.before == ""
    text, confidence = round_trip(book, xp("p[1]/text().0"), xp("p[1]/text()[3].1"))
    assert normalize_for_comparison(text) == "Hello brave new world."
    assert confidence is MatchConfidence.ONE_CONTEXT


def test_round_trip_across_inline_elements(book: EpubMap) -> None:
    # The quote starts in one text node of p[1] and ends in another, across <i> and <b>.
    text, _ = round_trip(book, xp("p[1]/text()[2].1"), xp("p[1]/text()[3].0"))
    assert normalize_for_comparison(text) == "new world"


def test_round_trip_across_paragraphs(book: EpubMap) -> None:
    text, _ = round_trip(book, xp("p[5]/text().0"), xp("p[6]/text().7"))
    assert normalize_for_comparison(text) == "The cat sat on the mat. The cat"


def test_round_trip_over_a_comment_in_the_content(book: EpubMap) -> None:
    # The comment splits the paragraph's source text but neither crengine nor the quote
    # sees it, so the offsets must be measured over the joined chunk.
    text, confidence = round_trip(book, xp("p[2]/text().0"), xp("p[2]/text().18"))
    assert normalize_for_comparison(text) == "Beforeafter the co"
    assert confidence is MatchConfidence.BOTH_CONTEXTS


def test_round_trip_after_an_astral_character(book: EpubMap) -> None:
    # "An emoji <emoji> sits mid sentence." — the emoji is one code point but two UTF-16
    # units, so offsets 11..15 only name "sits" if the round trip stays in code points.
    start, end = xp("p[3]/text().11"), xp("p[3]/text().15")
    assert xpoint_range_to_locator(book, start, end).text.highlight == "sits"
    assert round_trip(book, start, end)[0] == "sits"


def test_round_trip_spanning_an_astral_character(book: EpubMap) -> None:
    start, end = xp("p[3]/text().9"), xp("p[3]/text().15")
    assert xpoint_range_to_locator(book, start, end).text.highlight == f"{_EMOJI} sits"
    assert round_trip(book, start, end)[0] == f"{_EMOJI} sits"


def test_round_trip_preserves_the_xpointers_around_an_astral_character(book: EpubMap) -> None:
    # The strongest form of the astral check: the recovered xpointers are the originals.
    start, end = xp("p[3]/text().11"), xp("p[3]/text().15")
    match = locator_to_xpoint_range(book, xpoint_range_to_locator(book, start, end))
    assert match.xpoint_range.start.to_string() == "/body/DocFragment[1]/body/div[1]/p[3]/text().11"
    assert match.xpoint_range.end.to_string() == "/body/DocFragment[1]/body/div[1]/p[3]/text().15"


def test_round_trip_over_soft_hyphens(book: EpubMap) -> None:
    # crengine keeps the soft hyphens in the DOM but strips them from exported text.
    text, _ = round_trip(book, xp("p[4]/text().2"), xp("p[4]/text().19"))
    assert normalize_for_comparison(text) == "hyphenated word"


def test_repeated_phrase_is_disambiguated_by_context(book: EpubMap) -> None:
    # "The cat sat on the " appears in both p[5] and p[6]; only the context separates
    # them, and the scope selector alone does not (it is the enclosing paragraph here,
    # but the search must still pick the right occurrence when scoped to the div).
    locator = xpoint_range_to_locator(book, xp("p[6]/text().0"), xp("p[6]/text().18"))
    widened = Locator(
        href=locator.href,
        type=locator.type,
        locations=LocatorLocations(
            progression=locator.locations.progression, css_selector="#intro"
        ),
        text=locator.text,
    )
    match = locator_to_xpoint_range(book, widened)
    assert match.confidence is MatchConfidence.BOTH_CONTEXTS
    assert match.xpoint_range.start.xpath.endswith("p[6]")


def test_repeated_phrase_without_context_is_reported_as_ambiguous(book: EpubMap) -> None:
    locator = Locator(
        href="OEBPS/chap1.xhtml",
        type="application/xhtml+xml",
        locations=LocatorLocations(css_selector="#intro"),
        text=LocatorText(highlight="The cat sat on the "),
    )
    match = locator_to_xpoint_range(book, locator)
    assert match.confidence is MatchConfidence.AMBIGUOUS
    assert match.xpoint_range.start.xpath.endswith("p[5]")


def test_round_trip_in_the_second_spine_item(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(
        book,
        "/body/DocFragment[2]/body/p/text().0",
        "/body/DocFragment[2]/body/p/text().6",
    )
    assert locator.href == "OEBPS/chap2.xhtml"
    match = locator_to_xpoint_range(book, locator)
    assert match.xpoint_range.start.doc_fragment_index == 2
    assert extract_between(book, match.xpoint_range.start, match.xpoint_range.end) == "Second"


def test_a_point_locator_round_trips_to_a_zero_length_range(book: EpubMap) -> None:
    locator = xpoint_to_locator(book, xp("p[1]/text().6"))
    match = locator_to_xpoint_range(book, locator)
    assert match.xpoint_range.start == match.xpoint_range.end
    assert extract_between(book, match.xpoint_range.start, match.xpoint_range.end) == ""


def test_locator_can_be_given_as_a_plain_mapping(book: EpubMap) -> None:
    payload = xpoint_range_to_locator(book, xp("p[5]/text().4"), xp("p[5]/text().7")).to_dict()
    match = locator_to_xpoint_range(book, payload)
    assert extract_between(book, match.xpoint_range.start, match.xpoint_range.end) == "cat"


# --------------------------------------------------------------------------------------
# Fallbacks and errors
# --------------------------------------------------------------------------------------


def test_unresolvable_selector_falls_back_to_the_whole_body(book: EpubMap) -> None:
    locator = Locator(
        href="OEBPS/chap1.xhtml",
        type="application/xhtml+xml",
        locations=LocatorLocations(css_selector="#nothing-here > p:nth-child(99)"),
        text=LocatorText(before="An emoji ", highlight=f"{_EMOJI} sits", after=" mid"),
    )
    match = locator_to_xpoint_range(book, locator)
    assert extract_between(book, match.xpoint_range.start, match.xpoint_range.end) == (
        f"{_EMOJI} sits"
    )


def test_absent_selector_falls_back_to_the_whole_body(book: EpubMap) -> None:
    locator = Locator(
        href="OEBPS/chap1.xhtml",
        type="application/xhtml+xml",
        text=LocatorText(highlight="hyphenated word"),
    )
    match = locator_to_xpoint_range(book, locator)
    assert (
        normalize_for_comparison(
            extract_between(book, match.xpoint_range.start, match.xpoint_range.end)
        )
        == "hyphenated word"
    )


@pytest.mark.parametrize(
    "href",
    ["OEBPS/chap1.xhtml", "chap1.xhtml", "/OEBPS/chap1.xhtml", "OEBPS/chap1.xhtml#frag"],
)
def test_href_matching_tolerates_different_bases(book: EpubMap, href: str) -> None:
    locator = Locator(
        href=href,
        type="application/xhtml+xml",
        text=LocatorText(highlight="Hello brave new world."),
    )
    match = locator_to_xpoint_range(book, locator)
    assert match.xpoint_range.start.doc_fragment_index == 1


def test_segment_flattening_reproduces_extract_text(book: EpubMap) -> None:
    # The invariant the whole reverse direction rests on: the flattened, chunk-tagged
    # text the search runs over is character-for-character what extract_text yields, so
    # an offset found in one means the same thing in the other.
    from xpoint_cfi.locator import _segments  # pyright: ignore[reportPrivateUsage]

    for index in (1, 2):
        node = book.doc(index)
        flattened = "".join(segment.text for segment in _segments(node, node.body))
        assert flattened == node.extract_text(None, None)


def test_round_trip_in_a_document_stored_as_nfd() -> None:
    # Some EPUBs ship decomposed text while the quote arrives composed; both sides must
    # fold to the same thing, and the offsets must stay in source code points.
    import unicodedata

    body = unicodedata.normalize("NFD", "<p>Le café était très bon aujourd'hui.</p>")
    nfd_book = EpubMap.from_bytes(build_epub({"a.xhtml": xhtml_doc("A", body)}))
    locator = Locator(
        href="OEBPS/a.xhtml",
        type="application/xhtml+xml",
        text=LocatorText(before="Le ", highlight="café était", after=" très"),
    )
    match = locator_to_xpoint_range(nfd_book, locator)
    assert match.confidence is MatchConfidence.BOTH_CONTEXTS
    recovered = extract_between(nfd_book, match.xpoint_range.start, match.xpoint_range.end)
    assert unicodedata.normalize("NFC", recovered) == "café était"


def test_unknown_href_is_rejected(book: EpubMap) -> None:
    locator = Locator(href="nowhere.xhtml", type="application/xhtml+xml")
    with pytest.raises(ResolutionError, match="spine item"):
        locator_to_xpoint_range(book, locator)


def test_quote_that_is_absent_is_rejected(book: EpubMap) -> None:
    locator = Locator(
        href="OEBPS/chap1.xhtml",
        type="application/xhtml+xml",
        text=LocatorText(highlight="a sentence that appears nowhere in this book at all"),
    )
    with pytest.raises(ResolutionError, match="not found in scope"):
        locator_to_xpoint_range(book, locator)


def test_reverse_conversion_returns_an_xpoint_range(book: EpubMap) -> None:
    locator = xpoint_range_to_locator(book, xp("p[1]/text().0"), xp("p[1]/text().5"))
    match = locator_to_xpoint_range(book, locator)
    assert isinstance(match.xpoint_range, XPointRange)
    assert match.xpoint_range.start.doc_fragment_index == 1
