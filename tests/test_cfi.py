"""Tests for the pure string-level EPUB CFI parser/serializer in ``xpoint_cfi.cfi``."""

import pytest

from xpoint_cfi.cfi import (
    Cfi,
    CfiRange,
    CharOffset,
    LocalPath,
    Step,
    TextAssertion,
    parse_cfi,
)
from xpoint_cfi.exceptions import CfiParseError


def _cfi(result: Cfi | CfiRange) -> Cfi:
    assert isinstance(result, Cfi)
    return result


def _range(result: Cfi | CfiRange) -> CfiRange:
    assert isinstance(result, CfiRange)
    return result


# --------------------------------------------------------------------------------------
# Round-trips
# --------------------------------------------------------------------------------------

ROUND_TRIPS = [
    "epubcfi(/6/14[chap05]!/4/2/16/1:223)",
    "epubcfi(/6/4/2)",
    "epubcfi(/6/4/1:0)",
    "epubcfi(/6/14[chap05]!/4/2/16/1)",
    "epubcfi(/6[foo^]bar]!/4)",
    "epubcfi(/6/4/2/1:10[pre,post])",
    "epubcfi(/6/4/2/1:10[,post])",
    "epubcfi(/6/4/2/1:10[pre])",
    "epubcfi(/6/2!/4/6!/8/1:3)",
    "epubcfi(/6/4[a^,b]!/8)",
    "epubcfi(/6/4[id^^x]!/2)",
    "epubcfi(/6/4[chap]!/2/1:5[be^]fore,af^,ter])",
]


@pytest.mark.parametrize("text", ROUND_TRIPS)
def test_single_path_round_trip(text: str) -> None:
    parsed = parse_cfi(text)
    assert parsed.to_string() == text


def test_simple_path_structure() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/14[chap05]!/4/2/16/1:223)"))
    assert len(cfi.paths) == 2
    assert cfi.paths[0].steps == (Step(6, None), Step(14, "chap05"))
    assert cfi.paths[0].offset is None
    assert cfi.paths[1].steps == (Step(4, None), Step(2, None), Step(16, None), Step(1, None))
    assert cfi.paths[1].offset == CharOffset(223, None)


def test_id_assertion_escaping() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6[foo^]bar]!/4)"))
    assert cfi.paths[0].steps[0].assertion == "foo]bar"
    assert cfi.to_string() == "epubcfi(/6[foo^]bar]!/4)"


def test_circumflex_escape_round_trip() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/4[id^^x]!/2)"))
    assert cfi.paths[0].steps[1].assertion == "id^x"


def test_text_assertion_variants() -> None:
    both = _cfi(parse_cfi("epubcfi(/6/4/1:10[pre,post])"))
    assert both.paths[0].offset == CharOffset(10, TextAssertion("pre", "post"))

    after_only = _cfi(parse_cfi("epubcfi(/6/4/1:10[,post])"))
    assert after_only.paths[0].offset == CharOffset(10, TextAssertion(None, "post"))

    before_only = _cfi(parse_cfi("epubcfi(/6/4/1:10[pre])"))
    assert before_only.paths[0].offset == CharOffset(10, TextAssertion("pre", None))


def test_text_assertion_with_escaped_specials() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/4/1:5[be^]fore,af^,ter])"))
    ta = cfi.paths[0].offset
    assert ta is not None
    assert ta.text_assertion == TextAssertion("be]fore", "af,ter")


def test_multiple_indirections() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/2!/4/6!/8/1:3)"))
    assert len(cfi.paths) == 3
    assert cfi.paths[2].offset == CharOffset(3, None)


def test_odd_text_step_allowed() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/14[chap05]!/4/2/16/1)"))
    assert cfi.paths[1].steps[-1] == Step(1, None)


# --------------------------------------------------------------------------------------
# Ranges
# --------------------------------------------------------------------------------------


def test_range_with_step_subpaths() -> None:
    rng = _range(parse_cfi("epubcfi(/6/14[chap05]!/4,/2/1:1,/3/1:5)"))
    assert rng.parent.to_string() == "epubcfi(/6/14[chap05]!/4)"
    assert rng.start.to_string() == "epubcfi(/2/1:1)"
    assert rng.end.to_string() == "epubcfi(/3/1:5)"
    assert rng.to_string() == "epubcfi(/6/14[chap05]!/4,/2/1:1,/3/1:5)"


def test_range_with_bare_offset_subpaths() -> None:
    rng = _range(parse_cfi("epubcfi(/6/4!/2/1,:0,:10)"))
    assert rng.start.paths[0].steps == ()
    assert rng.start.paths[0].offset == CharOffset(0, None)
    assert rng.end.paths[0].offset == CharOffset(10, None)
    assert rng.to_string() == "epubcfi(/6/4!/2/1,:0,:10)"


def test_range_commas_inside_text_assertions_do_not_split() -> None:
    rng = _range(parse_cfi("epubcfi(/6/4!/2/1,:0[a,b],:5[c,d])"))
    start_offset = rng.start.paths[0].offset
    end_offset = rng.end.paths[0].offset
    assert start_offset is not None and start_offset.text_assertion == TextAssertion("a", "b")
    assert end_offset is not None and end_offset.text_assertion == TextAssertion("c", "d")
    assert rng.to_string() == "epubcfi(/6/4!/2/1,:0[a,b],:5[c,d])"


def test_range_with_indirection_in_subpath() -> None:
    text = "epubcfi(/6/4!/2,/1:0,/3!/2/1:4)"
    rng = _range(parse_cfi(text))
    assert len(rng.end.paths) == 2
    assert rng.to_string() == text


# --------------------------------------------------------------------------------------
# Rejections
# --------------------------------------------------------------------------------------

REJECTS = [
    "/6/4",  # missing wrapper
    "epubcfi(/6/4",  # missing closing paren
    "epubcfi(/6/0)",  # zero step index
    "epubcfi(/6/-2)",  # negative step index
    "epubcfi(/6/)",  # missing step index
    "epubcfi(:5)",  # path must begin with a step
    "epubcfi(/6:5!/4)",  # offset in a non-final local path
    "epubcfi(/6/4~3.0)",  # temporal offset
    "epubcfi(/6/4@1:2)",  # spatial offset
    "epubcfi(/6/4!)",  # trailing bare '!'
    "epubcfi(!/4/2)",  # leading bare '!'
    "epubcfi(/6/4!!/2)",  # empty local path between '!'
    "epubcfi(/6!:5)",  # '!' followed only by an offset
    "epubcfi(,/2/1,/4/1)",  # empty range parent
    "epubcfi(/6[chap)",  # unbalanced '[' in assertion
    "epubcfi(/6/4,/2)",  # two-part (invalid) range
    "epubcfi(/6/4[a^b])",  # invalid escape sequence
]


@pytest.mark.parametrize("text", REJECTS)
def test_rejects(text: str) -> None:
    with pytest.raises(CfiParseError):
        parse_cfi(text)


def test_reject_preserves_offending_input() -> None:
    with pytest.raises(CfiParseError) as excinfo:
        parse_cfi("/6/4")
    assert excinfo.value.cfi == "/6/4"
    assert excinfo.value.reason


# --------------------------------------------------------------------------------------
# Parameter assertions are dropped (documented non-lossless case)
# --------------------------------------------------------------------------------------


def test_parameter_assertions_are_dropped() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/4[chap;vnd.example=1])"))
    assert cfi.paths[0].steps[1].assertion == "chap"
    assert cfi.to_string() == "epubcfi(/6/4[chap])"


def test_parameter_only_assertion_dropped_to_bare_step() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/4[;vnd.example=1])"))
    assert cfi.paths[0].steps[1].assertion is None
    assert cfi.to_string() == "epubcfi(/6/4)"


def test_text_assertion_parameters_dropped() -> None:
    cfi = _cfi(parse_cfi("epubcfi(/6/4/1:3[pre,post;s=b])"))
    assert cfi.paths[0].offset == CharOffset(3, TextAssertion("pre", "post"))
    assert cfi.to_string() == "epubcfi(/6/4/1:3[pre,post])"


# --------------------------------------------------------------------------------------
# sort_key document ordering
# --------------------------------------------------------------------------------------


def _key(text: str) -> tuple[int, ...]:
    return _cfi(parse_cfi(text)).sort_key()


def test_sort_key_prefix_sorts_first() -> None:
    assert _key("epubcfi(/6/4)") < _key("epubcfi(/6/4/2)")


def test_sort_key_prefix_across_indirection() -> None:
    assert _key("epubcfi(/6/14!/4)") < _key("epubcfi(/6/14!/4/2)")


def test_sort_key_offset_tiebreak() -> None:
    assert _key("epubcfi(/6/4/1:5)") < _key("epubcfi(/6/4/1:10)")


def test_sort_key_offsetless_sorts_before_offset() -> None:
    assert _key("epubcfi(/6/4/1)") < _key("epubcfi(/6/4/1:1)")


def test_sort_key_by_step_index() -> None:
    assert _key("epubcfi(/6/4)") < _key("epubcfi(/6/6)")


def test_sort_key_ignores_assertions() -> None:
    assert _key("epubcfi(/6/4[alpha])") == _key("epubcfi(/6/4[omega])")


def test_sort_key_full_document_ordering() -> None:
    texts = [
        "epubcfi(/6/14!/4/2/16/1:223)",
        "epubcfi(/6/4)",
        "epubcfi(/6/4/1:5)",
        "epubcfi(/6/4/1:10)",
        "epubcfi(/6/4/2)",
        "epubcfi(/6/14!/4)",
    ]
    ordered = sorted(texts, key=_key)
    assert ordered == [
        "epubcfi(/6/4)",
        "epubcfi(/6/4/1:5)",
        "epubcfi(/6/4/1:10)",
        "epubcfi(/6/4/2)",
        "epubcfi(/6/14!/4)",
        "epubcfi(/6/14!/4/2/16/1:223)",
    ]


# --------------------------------------------------------------------------------------
# Frozen dataclass sanity
# --------------------------------------------------------------------------------------


def test_dataclasses_are_frozen() -> None:
    step = Step(2, None)
    with pytest.raises(AttributeError):
        step.index = 4  # type: ignore[misc]


def test_constructed_cfi_serializes() -> None:
    cfi = Cfi(paths=(LocalPath(steps=(Step(6, None), Step(4, "id")), offset=CharOffset(3, None)),))
    assert cfi.to_string() == "epubcfi(/6/4[id]:3)"
