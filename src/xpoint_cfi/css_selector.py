"""Generate and resolve the CSS selectors a Readium locator carries.

A Readium ``locations.cssSelector`` must be resolvable with ``document.querySelector``,
so this module speaks the same dialect Readium's own generators emit — an ``#id`` when
one is available, otherwise a ``tag:nth-child(n)`` chain joined with ``>`` and rooted at
``body``:

* ``@readium/navigator`` bundles `css-selector-generator
  <https://github.com/fczbkk/css-selector-generator>`_;
* the Electron navigator (``r2-navigator-js``) uses a fork of `finder
  <https://github.com/antonmedv/finder>`_.

Both prefer ``#`` + ``CSS.escape(id)`` over an attribute selector, count ``:nth-child``
1-based over *element* siblings only, and verify a candidate resolves uniquely before
returning it. :func:`selector_for_element` does the same.

:func:`resolve_selector` is the matching reader, needed to turn a locator produced
elsewhere back into a document position. It implements the subset a navigator can emit
(type / ``*`` / ``#id`` / ``.class`` / ``[attr=value]`` / ``:nth-child`` /
``:nth-of-type``, combined with ``>`` and descendant whitespace) and matches element
names by **local name**, because XHTML carries a default namespace that the selectors do
not mention.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from lxml import etree

from .epub_map import element_children

_Element = etree._Element  # pyright: ignore[reportPrivateUsage]

__all__ = [
    "escape_css_identifier",
    "resolve_selector",
    "selector_for_element",
]

# An id Readium's generators refuse to put in a selector: empty, or containing
# whitespace (which no amount of escaping makes usable as an id selector).
_UNUSABLE_ID = re.compile(r"^$|\s")

_HEX = re.compile(r"[0-9a-fA-F]")


def _local_name(node: _Element) -> str:
    tag = node.tag
    if callable(tag):  # lxml comments and processing instructions
        return ""
    return tag.rsplit("}", 1)[-1]


def _is_element(node: _Element) -> bool:
    return not callable(node.tag)


# --------------------------------------------------------------------------------------
# Identifier escaping (CSSOM "serialize an identifier", i.e. CSS.escape)
# --------------------------------------------------------------------------------------


def escape_css_identifier(value: str) -> str:
    """Escape ``value`` for use as a CSS identifier, like the DOM's ``CSS.escape``.

    Implements the CSSOM *serialize an identifier* algorithm: control characters and a
    leading digit become a hex escape terminated by a space, a lone ``-`` is escaped,
    ASCII alphanumerics plus ``-``, ``_`` and everything above U+007F pass through, and
    every other character is backslash-escaped.
    """
    out: list[str] = []
    for i, ch in enumerate(value):
        code = ord(ch)
        digit = "0" <= ch <= "9"
        needs_hex = (
            code <= 0x1F
            or code == 0x7F
            or (i == 0 and digit)
            or (i == 1 and digit and value[0] == "-")
        )
        if code == 0:
            out.append("\ufffd")
        elif needs_hex:
            out.append(f"\\{code:x} ")
        elif i == 0 and ch == "-" and len(value) == 1:
            out.append("\\-")
        elif code >= 0x80 or ch in "-_" or (ch.isascii() and ch.isalnum()):
            out.append(ch)
        else:
            out.append("\\" + ch)
    return "".join(out)


def _unescape_css_identifier(value: str) -> str:
    """Resolve CSS backslash escapes in ``value`` (the inverse of the escaping above)."""
    out: list[str] = []
    i = 0
    n = len(value)
    while i < n:
        ch = value[i]
        if ch != "\\" or i + 1 >= n:
            out.append(ch)
            i += 1
            continue
        i += 1
        digits = ""
        while i < n and len(digits) < 6 and _HEX.match(value[i]):
            digits += value[i]
            i += 1
        if digits:
            out.append(chr(int(digits, 16)))
            if i < n and value[i] in " \t\r\n\f":
                i += 1
        else:
            out.append(value[i])
            i += 1
    return "".join(out)


# --------------------------------------------------------------------------------------
# Generation
# --------------------------------------------------------------------------------------


def _nth_child(node: _Element) -> int:
    """Return the 1-based ``:nth-child`` position of ``node`` among element siblings."""
    parent = node.getparent()
    if parent is None:
        return 1
    for position, child in enumerate(element_children(parent), start=1):
        if child is node:
            return position
    return 1


def _unique_ids(root: _Element) -> frozenset[str]:
    """Return the ids that occur exactly once under ``root`` and can anchor a selector.

    A duplicated id is excluded because ``querySelector`` would then pick an arbitrary
    one of them; an empty or whitespace-bearing id is excluded because no escaping makes
    it usable — Readium's generators drop both too.
    """
    counts: dict[str, int] = {}
    for el in root.iter():
        id_value = el.get("id") if _is_element(el) else None
        if id_value is not None and not _UNUSABLE_ID.search(id_value):
            counts[id_value] = counts.get(id_value, 0) + 1
    return frozenset(value for value, count in counts.items() if count == 1)


def selector_for_element(root: _Element, elem: _Element) -> str:
    """Return a ``querySelector``-resolvable CSS selector addressing ``elem``.

    The chain is built bottom-up and stops at the first anchor it can trust: the
    element's own unique ``id``, then an ancestor's, then ``body``. Every other level
    contributes ``tag:nth-child(n)``. The result is verified against
    :func:`resolve_selector`; if an id-anchored chain somehow fails to select ``elem``
    the plain positional chain from ``body`` is returned instead.

    ``root`` is the document element the selector will be resolved against.
    """
    id_anchored = _build_selector(elem, _unique_ids(root))
    if resolve_selector(root, id_anchored) is elem:
        return id_anchored
    return _build_selector(elem, frozenset())


def _build_selector(elem: _Element, unique_ids: frozenset[str]) -> str:
    if _local_name(elem) == "html":
        return "html"
    parts: list[str] = []
    node: _Element | None = elem
    while node is not None and _is_element(node):
        name = _local_name(node)
        if name == "html":
            break
        id_value = node.get("id")
        if id_value is not None and id_value in unique_ids:
            parts.append("#" + escape_css_identifier(id_value))
            break
        if name == "body":
            parts.append("body")
            break
        parts.append(f"{escape_css_identifier(name)}:nth-child({_nth_child(node)})")
        node = node.getparent()
    parts.reverse()
    return " > ".join(parts)


# --------------------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Compound:
    """One compound selector: an optional type plus any number of qualifiers."""

    local_name: str | None = None
    element_id: str | None = None
    classes: tuple[str, ...] = ()
    attributes: tuple[tuple[str, str | None], ...] = ()
    nth_child: int | None = None
    nth_of_type: int | None = None


@dataclass(frozen=True)
class _Chain:
    """A parsed complex selector: compounds plus the combinator preceding each."""

    compounds: tuple[_Compound, ...]
    combinators: tuple[str, ...] = field(default=())


# A CSS escape sequence: a hex escape (whose optional trailing whitespace terminates it
# rather than separating two compounds) or a backslash-escaped character.
_ESCAPE_SOURCE = r"\\[0-9a-fA-F]{1,6}[ \t\r\n\f]?|\\."
_ESCAPE = re.compile(_ESCAPE_SOURCE)

_COMPOUND_TOKEN = re.compile(
    r"""
    (?P<type>\*\|[\w-]+|\*|(?:{esc}|[\w-])+)          # type: tag, *, *|tag
  | \#(?P<id>(?:{esc}|[^\s.\#\[:>,])+)                # #id
  | \.(?P<class>(?:{esc}|[^\s.\#\[:>,])+)             # .class
  | \[(?P<attr>[^\]]*)\]                              # [attr] or [attr="value"]
  | :(?P<pseudo>nth-child|nth-of-type)\(\s*(?P<n>\d+)\s*\)
    """.replace("{esc}", _ESCAPE_SOURCE),
    re.VERBOSE | re.IGNORECASE,
)

_ATTR = re.compile(r"^\s*([\w:-]+)\s*(?:([~|^$*]?=)\s*(.*?)\s*)?$")


def _split_top_level(selector: str, separators: str) -> list[str]:
    """Split ``selector`` on any character in ``separators``.

    Separators inside ``[]``, inside a quoted attribute value, or consumed by a CSS
    escape sequence do not split — which is why the descendant combinator (whitespace)
    cannot be found with a plain :meth:`str.split`: the space that terminates a hex
    escape such as ``#\\31 23`` belongs to the identifier.
    """
    parts: list[str] = []
    current: list[str] = []
    depth = 0
    quote: str | None = None
    i = 0
    while i < len(selector):
        ch = selector[i]
        escape = _ESCAPE.match(selector, i)
        if escape is not None:
            current.append(escape.group(0))
            i = escape.end()
            continue
        if quote is not None:
            current.append(ch)
            quote = None if ch == quote else quote
        elif ch in "\"'":
            quote = ch
            current.append(ch)
        elif ch == "[":
            depth += 1
            current.append(ch)
        elif ch == "]":
            depth = max(depth - 1, 0)
            current.append(ch)
        elif ch in separators and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts


def _parse_compound(text: str) -> _Compound | None:
    local_name: str | None = None
    element_id: str | None = None
    classes: list[str] = []
    attributes: list[tuple[str, str | None]] = []
    nth_child: int | None = None
    nth_of_type: int | None = None

    position = 0
    while position < len(text):
        match = _COMPOUND_TOKEN.match(text, position)
        if match is None:
            return None
        if match.group("type") is not None:
            raw = match.group("type")
            if position != 0:
                return None
            if raw != "*":
                local_name = _unescape_css_identifier(raw.removeprefix("*|"))
        elif match.group("id") is not None:
            element_id = _unescape_css_identifier(match.group("id"))
        elif match.group("class") is not None:
            classes.append(_unescape_css_identifier(match.group("class")))
        elif match.group("attr") is not None:
            attr = _ATTR.match(match.group("attr"))
            if attr is None:
                return None
            name, operator, value = attr.groups()
            if operator not in (None, "="):
                return None
            attributes.append((name, None if value is None else value.strip("\"'")))
        else:
            count = int(match.group("n"))
            if match.group("pseudo").lower() == "nth-child":
                nth_child = count
            else:
                nth_of_type = count
        position = match.end()

    return _Compound(
        local_name=local_name,
        element_id=element_id,
        classes=tuple(classes),
        attributes=tuple(attributes),
        nth_child=nth_child,
        nth_of_type=nth_of_type,
    )


def _parse_chain(selector: str) -> _Chain | None:
    """Parse one complex selector, or return ``None`` when it uses unsupported syntax."""
    compounds: list[_Compound] = []
    combinators: list[str] = []
    for index, section in enumerate(_split_top_level(selector, ">")):
        words = [word for word in _split_top_level(section, " \t\r\n\f") if word]
        if not words:
            return None
        for word_index, word in enumerate(words):
            compound = _parse_compound(word)
            if compound is None:
                return None
            if compounds:
                combinators.append(">" if index > 0 and word_index == 0 else " ")
            compounds.append(compound)
    if not compounds:
        return None
    return _Chain(compounds=tuple(compounds), combinators=tuple(combinators))


def _nth_of_type(node: _Element) -> int:
    parent = node.getparent()
    if parent is None:
        return 1
    name = _local_name(node)
    position = 0
    for child in element_children(parent):
        if _local_name(child) == name:
            position += 1
            if child is node:
                return position
    return 1


def _matches_compound(node: _Element, compound: _Compound) -> bool:
    if compound.local_name is not None and _local_name(node) != compound.local_name:
        return False
    if compound.element_id is not None and node.get("id") != compound.element_id:
        return False
    if compound.classes:
        present = set((node.get("class") or "").split())
        if not present.issuperset(compound.classes):
            return False
    for name, value in compound.attributes:
        actual = node.get(name)
        if actual is None or (value is not None and actual != value):
            return False
    if compound.nth_child is not None and _nth_child(node) != compound.nth_child:
        return False
    return not (compound.nth_of_type is not None and _nth_of_type(node) != compound.nth_of_type)


def _matches_chain(node: _Element, chain: _Chain, index: int) -> bool:
    if not _matches_compound(node, chain.compounds[index]):
        return False
    if index == 0:
        return True
    parent = node.getparent()
    if chain.combinators[index - 1] == ">":
        return (
            parent is not None and _is_element(parent) and _matches_chain(parent, chain, index - 1)
        )
    ancestor = parent
    while ancestor is not None and _is_element(ancestor):
        if _matches_chain(ancestor, chain, index - 1):
            return True
        ancestor = ancestor.getparent()
    return False


def resolve_selector(root: _Element, selector: str) -> _Element | None:
    """Return the first element under ``root`` matching ``selector``, like ``querySelector``.

    Returns ``None`` when the selector matches nothing or uses syntax outside the
    supported subset — callers are expected to fall back rather than fail, because a
    locator may have been produced by a navigator running against a differently
    processed DOM.
    """
    chains = [
        chain
        for chain in (_parse_chain(part) for part in _split_top_level(selector, ","))
        if chain is not None
    ]
    if not chains:
        return None
    for node in root.iter():
        if not _is_element(node):
            continue
        for chain in chains:
            if _matches_chain(node, chain, len(chain.compounds) - 1):
                return node
    return None
