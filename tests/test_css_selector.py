"""Tests for CSS selector generation and resolution.

Generated selectors must look like the ones ``@readium/navigator`` produces (``#id``
when available, otherwise a ``tag:nth-child(n)`` chain from ``body`` joined with ``>``)
and must round-trip through :func:`resolve_selector`, which stands in for
``document.querySelector`` on the Python side.
"""

from __future__ import annotations

import pytest
from conftest import xhtml_doc
from lxml import etree

from xpoint_cfi.css_selector import (
    escape_css_identifier,
    resolve_selector,
    selector_for_element,
)

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]


def parse(body: str) -> _Element:
    return etree.fromstring(xhtml_doc("T", body).encode(), etree.XMLParser())


def find(root: _Element, xpath: str) -> _Element:
    matches = root.xpath(xpath, namespaces={"x": "http://www.w3.org/1999/xhtml"})
    assert isinstance(matches, list) and matches, xpath
    element = matches[0]
    assert isinstance(element, etree._Element)  # pyright: ignore[reportPrivateUsage]
    return element


# --------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------


def test_body_selects_itself() -> None:
    root = parse("<p>x</p>")
    assert selector_for_element(root, find(root, "//x:body")) == "body"


def test_nth_child_chain_is_rooted_at_body_and_joined_with_child_combinator() -> None:
    root = parse("<div><p>one</p><p>two</p></div>")
    target = find(root, "//x:p[2]")
    assert selector_for_element(root, target) == "body > div:nth-child(1) > p:nth-child(2)"


def test_nth_child_counts_all_element_siblings_not_just_same_tag() -> None:
    # The <p> is the third element child even though it is the first <p>.
    root = parse("<div><h1>a</h1><span>b</span><p>c</p></div>")
    target = find(root, "//x:p")
    assert selector_for_element(root, target).endswith("p:nth-child(3)")


def test_comments_do_not_shift_nth_child() -> None:
    root = parse("<div><!--x--><p>one</p><!--y--><p>two</p></div>")
    assert selector_for_element(root, find(root, "//x:p[2]")).endswith("p:nth-child(2)")


def test_own_id_wins_over_the_positional_chain() -> None:
    root = parse('<div><p id="para">one</p></div>')
    assert selector_for_element(root, find(root, "//x:p")) == "#para"


def test_ancestor_id_anchors_the_chain() -> None:
    root = parse('<div id="intro"><p>one</p><p>two</p></div>')
    assert selector_for_element(root, find(root, "//x:p[2]")) == "#intro > p:nth-child(2)"


def test_duplicate_id_is_not_trusted() -> None:
    # Two elements share the id, so querySelector would pick an arbitrary one.
    root = parse('<div><p id="dup">one</p><p id="dup">two</p></div>')
    selector = selector_for_element(root, find(root, "//x:p[2]"))
    assert selector == "body > div:nth-child(1) > p:nth-child(2)"


def test_id_with_a_leading_digit_is_escaped_and_still_resolves() -> None:
    root = parse('<div><p id="123">one</p></div>')
    target = find(root, "//x:p")
    selector = selector_for_element(root, target)
    assert selector == "#\\31 23"
    assert resolve_selector(root, selector) is target


def test_id_containing_whitespace_falls_back_to_the_positional_chain() -> None:
    root = parse('<div><p id="a b">one</p></div>')
    selector = selector_for_element(root, find(root, "//x:p"))
    assert selector == "body > div:nth-child(1) > p:nth-child(1)"


def test_generated_selector_resolves_back_to_every_element() -> None:
    root = parse(
        '<div id="intro"><p>one</p><p><i>two</i><b>three</b></p></div>'
        "<div><ul><li>a</li><li>b</li></ul></div>"
    )
    for element in root.iter():
        if callable(element.tag) or element.tag.endswith("}html"):
            continue
        selector = selector_for_element(root, element)
        assert resolve_selector(root, selector) is element, selector


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


def test_resolves_despite_the_xhtml_default_namespace() -> None:
    # The document is in the XHTML namespace; the selector names no namespace at all.
    root = parse("<div><p>one</p></div>")
    assert resolve_selector(root, "body > div > p") is find(root, "//x:p")


def test_descendant_combinator_skips_levels() -> None:
    root = parse("<div><section><p>one</p></section></div>")
    assert resolve_selector(root, "body p") is find(root, "//x:p")


def test_nth_of_type_counts_same_tag_siblings() -> None:
    root = parse("<div><span>a</span><p>b</p><p>c</p></div>")
    assert resolve_selector(root, "p:nth-of-type(2)") is find(root, "//x:p[2]")


def test_class_and_attribute_selectors() -> None:
    root = parse('<div><p class="a b" data-k="v">one</p></div>')
    target = find(root, "//x:p")
    assert resolve_selector(root, "p.a.b") is target
    assert resolve_selector(root, 'p[data-k="v"]') is target
    assert resolve_selector(root, "p[data-k]") is target
    assert resolve_selector(root, 'p[data-k="other"]') is None


def test_universal_and_namespaced_type_selectors() -> None:
    root = parse("<div><p>one</p></div>")
    assert resolve_selector(root, "div > *:nth-child(1)") is find(root, "//x:p")
    assert resolve_selector(root, "*|p") is find(root, "//x:p")


def test_selector_list_takes_the_first_match_in_document_order() -> None:
    root = parse("<div><h1>a</h1><p>b</p></div>")
    assert resolve_selector(root, "p, h1") is find(root, "//x:h1")


def test_unresolvable_and_unsupported_selectors_return_none() -> None:
    root = parse("<div><p>one</p></div>")
    assert resolve_selector(root, "body > section:nth-child(9)") is None
    assert resolve_selector(root, "p:has(> span)") is None
    assert resolve_selector(root, "") is None


@pytest.mark.parametrize(
    ("raw", "escaped"),
    [
        ("plain", "plain"),
        ("with-dash_and_1", "with-dash_and_1"),
        ("123", "\\31 23"),
        ("-9lives", "-\\39 lives"),
        ("-", "\\-"),
        ("foo:bar", "foo\\:bar"),
        ("a.b", "a\\.b"),
        ("héllo", "héllo"),
    ],
)
def test_escape_css_identifier(raw: str, escaped: str) -> None:
    assert escape_css_identifier(raw) == escaped


def test_escaped_id_selector_resolves_to_the_element() -> None:
    root = parse('<div><p id="foo:bar">one</p><p id="a.b">two</p></div>')
    for id_value in ("foo:bar", "a.b"):
        selector = "#" + escape_css_identifier(id_value)
        resolved = resolve_selector(root, selector)
        assert resolved is not None and resolved.get("id") == id_value
