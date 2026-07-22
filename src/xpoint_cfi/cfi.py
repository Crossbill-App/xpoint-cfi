"""Pure string-level EPUB CFI 1.1 parser and serializer.

This module implements the parsing and serialization of EPUB Canonical Fragment
Identifiers (CFI, version 1.1) as a self-contained string layer with **no document
access**. It never touches an EPUB, an OPF, or an XHTML tree; it only understands the
CFI grammar itself.

Spec: https://idpf.org/epub/linking/cfi/epub-cfi.html

Grammar subset supported (EBNF from the spec)::

    fragment        = "epubcfi(" , ( path , [ range ] ) , ")" ;
    path            = step , local_path ;
    range           = "," , local_path , "," , local_path ;
    local_path      = { step } , ( redirected_path | [ offset ] ) ;
    redirected_path = "!" , ( offset | path ) ;
    step            = "/" , integer , [ "[" , assertion , "]" ] ;
    offset          = ( ":" , integer ) , [ "[" , assertion , "]" ] ;

Deliberate limitations, all documented and enforced:

* **Temporal (``~``) and spatial (``@``) offsets are rejected** with a
  :class:`~xpoint_cfi.exceptions.CfiParseError`. They are out of scope.
* **Parameter assertions** (``;name=value`` inside a bracketed assertion) are parsed
  but **silently dropped**. A CFI that carries only parameters therefore does not
  round-trip byte-for-byte; every other supported construct does.
* A **step assertion** is treated as a single opaque ID string. The rare two-value
  step assertion form ``[a,b]`` is normalized on output to the escaped single value
  ``[a^,b]``. ID assertions with commas do not occur in practice.
* ``!`` (indirection) must be *followed by at least one step*; a bare or trailing
  ``!`` is invalid, as is a ``!`` immediately followed by an offset.

The public entry point is :func:`parse_cfi`. Both :class:`Cfi` and :class:`CfiRange`
expose ``to_string()``; :class:`Cfi` additionally exposes ``sort_key()`` for
document-order comparison.
"""

from dataclasses import dataclass

from .exceptions import CfiParseError

__all__ = [
    "Cfi",
    "CfiRange",
    "CharOffset",
    "LocalPath",
    "Step",
    "TextAssertion",
    "parse_cfi",
]

_SPECIAL: frozenset[str] = frozenset("^[](),;")


def _escape(value: str) -> str:
    """Escape every CFI special character in ``value`` with a leading circumflex."""
    out: list[str] = []
    for ch in value:
        if ch in _SPECIAL:
            out.append("^")
        out.append(ch)
    return "".join(out)


def _unescape(raw: str, source: str) -> str:
    """Resolve ``^``-escapes in ``raw``, raising :class:`CfiParseError` on bad escapes.

    ``source`` is the full original CFI string, used only for error context.
    """
    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch == "^":
            if i + 1 >= n:
                raise CfiParseError(source, "dangling escape character '^'")
            nxt = raw[i + 1]
            if nxt not in _SPECIAL:
                raise CfiParseError(source, f"invalid escape sequence '^{nxt}'")
            out.append(nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _find_closing_bracket(s: str, open_idx: int, source: str) -> int:
    """Return the index of the ``]`` closing the ``[`` at ``open_idx``.

    Respects ``^``-escaping so that ``^]`` does not close the assertion. Raises
    :class:`CfiParseError` if the bracket is unbalanced or nested unescaped.
    """
    i = open_idx + 1
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "^":
            i += 2
            continue
        if ch == "]":
            return i
        if ch == "[":
            raise CfiParseError(source, "unexpected unescaped '[' inside assertion")
        i += 1
    raise CfiParseError(source, "unbalanced '[' in assertion")


def _split_depth0(s: str, sep: str) -> list[str]:
    """Split ``s`` on ``sep`` only where bracket depth is zero and ``sep`` is unescaped.

    Escaped pairs (``^x``) are copied verbatim into the output parts; assertion
    contents are left in their escaped form for the callers to unescape.
    """
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "^":
            cur.append(ch)
            if i + 1 < n:
                cur.append(s[i + 1])
                i += 2
            else:
                i += 1
            continue
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _find_unescaped(s: str, target: str) -> int | None:
    """Return the index of the first unescaped ``target`` in ``s``, or ``None``."""
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == "^":
            i += 2
            continue
        if ch == target:
            return i
        i += 1
    return None


def _strip_parameters(content: str) -> str:
    """Drop any ``;name=value`` parameter assertions, returning the value section."""
    semicolon = _find_unescaped(content, ";")
    if semicolon is None:
        return content
    return content[:semicolon]


@dataclass(frozen=True)
class Step:
    """A single element step ``/<index>`` with an optional ID assertion.

    ``index`` is a positive integer (even = element position, odd = text-node gap).
    ``assertion`` is the opaque, unescaped ID string, or ``None`` when absent.
    """

    index: int
    assertion: str | None

    def to_string(self) -> str:
        """Serialize this step, re-escaping the assertion's special characters."""
        if self.assertion is None:
            return f"/{self.index}"
        return f"/{self.index}[{_escape(self.assertion)}]"


@dataclass(frozen=True)
class TextAssertion:
    """A text-location assertion ``[before,after]`` attached to a character offset.

    ``before`` / ``after`` hold the unescaped text preceding / following the offset;
    either may be ``None`` (forms ``[before]`` and ``[,after]`` respectively).
    """

    before: str | None
    after: str | None

    def to_string(self) -> str:
        """Serialize this text assertion, re-escaping special characters."""
        if self.before is not None and self.after is not None:
            return f"[{_escape(self.before)},{_escape(self.after)}]"
        if self.before is not None:
            return f"[{_escape(self.before)}]"
        if self.after is not None:
            return f"[,{_escape(self.after)}]"
        return "[]"


@dataclass(frozen=True)
class CharOffset:
    """A character offset ``:<value>`` with an optional text-location assertion.

    ``value`` is a non-negative integer offset within the terminal chunk.
    """

    value: int
    text_assertion: TextAssertion | None

    def to_string(self) -> str:
        """Serialize this offset and any trailing text assertion."""
        base = f":{self.value}"
        if self.text_assertion is not None:
            return base + self.text_assertion.to_string()
        return base


@dataclass(frozen=True)
class LocalPath:
    """A run of steps within one indirection level, plus an optional terminal offset.

    A local path may have zero steps only when it carries a bare offset (the relative
    ``start`` / ``end`` of a range, e.g. ``:0``).
    """

    steps: tuple[Step, ...]
    offset: CharOffset | None

    def to_string(self) -> str:
        """Serialize the steps followed by the offset, if any."""
        body = "".join(step.to_string() for step in self.steps)
        if self.offset is not None:
            body += self.offset.to_string()
        return body


def _paths_to_string(paths: tuple[LocalPath, ...]) -> str:
    """Join local paths with ``!`` indirection separators (no ``epubcfi(...)`` wrapper)."""
    return "!".join(local_path.to_string() for local_path in paths)


@dataclass(frozen=True)
class Cfi:
    """A non-range CFI: one or more local paths separated by ``!`` indirections."""

    paths: tuple[LocalPath, ...]

    def to_string(self) -> str:
        """Serialize to a wrapped ``epubcfi(...)`` string."""
        return f"epubcfi({_paths_to_string(self.paths)})"

    def sort_key(self) -> tuple[int, ...]:
        """Return a tuple giving document-order position (assertions ignored).

        Every step index across all indirection levels is flattened into one tuple,
        with the terminal character offset appended last. Because tuple comparison is
        lexicographic, a CFI that is a prefix of another sorts first, and offsets act
        as the final tie-breaker.
        """
        key: list[int] = []
        for local_path in self.paths:
            for step in local_path.steps:
                key.append(step.index)
            if local_path.offset is not None:
                key.append(local_path.offset.value)
        return tuple(key)


@dataclass(frozen=True)
class CfiRange:
    """A range CFI ``epubcfi(parent,start,end)``.

    ``parent`` is the shared prefix (a non-empty path); ``start`` and ``end`` are the
    relative sub-paths, each stored as a :class:`Cfi`. There is intentionally no
    ``sort_key`` on ranges.
    """

    parent: Cfi
    start: Cfi
    end: Cfi

    def to_string(self) -> str:
        """Serialize to a wrapped, three-part ``epubcfi(parent,start,end)`` string."""
        inner = ",".join(
            _paths_to_string(part.paths) for part in (self.parent, self.start, self.end)
        )
        return f"epubcfi({inner})"


def _parse_step_assertion(content: str, source: str) -> str | None:
    """Parse a step (ID) assertion's bracket contents, dropping any parameters."""
    value_section = _strip_parameters(content)
    if value_section == "":
        return None
    return _unescape(value_section, source)


def _parse_text_assertion(content: str, source: str) -> TextAssertion:
    """Parse a text-location assertion's bracket contents, dropping any parameters."""
    value_section = _strip_parameters(content)
    comma = _find_unescaped(value_section, ",")
    if comma is None:
        before = _unescape(value_section, source) if value_section else None
        return TextAssertion(before=before, after=None)
    before_raw = value_section[:comma]
    after_raw = value_section[comma + 1 :]
    before = _unescape(before_raw, source) if before_raw else None
    after = _unescape(after_raw, source) if after_raw else None
    return TextAssertion(before=before, after=after)


def _parse_offset(seg: str, i: int, source: str) -> tuple[CharOffset, int]:
    """Parse a ``:<integer>`` offset (with optional text assertion) starting at ``i``."""
    i += 1
    start = i
    n = len(seg)
    while i < n and seg[i].isdigit():
        i += 1
    if i == start:
        raise CfiParseError(source, "expected an integer offset after ':'")
    value = int(seg[start:i])
    text_assertion: TextAssertion | None = None
    if i < n and seg[i] == "[":
        close = _find_closing_bracket(seg, i, source)
        text_assertion = _parse_text_assertion(seg[i + 1 : close], source)
        i = close + 1
    return CharOffset(value=value, text_assertion=text_assertion), i


def _parse_segment(seg: str, source: str) -> tuple[list[Step], CharOffset | None]:
    """Parse one indirection segment into its steps and optional terminal offset."""
    steps: list[Step] = []
    i = 0
    n = len(seg)
    while i < n and seg[i] == "/":
        i += 1
        start = i
        while i < n and seg[i].isdigit():
            i += 1
        if i == start:
            raise CfiParseError(source, f"expected a step index after '/' in {seg!r}")
        index = int(seg[start:i])
        if index < 1:
            raise CfiParseError(source, f"step index must be >= 1, got {index}")
        assertion: str | None = None
        if i < n and seg[i] == "[":
            close = _find_closing_bracket(seg, i, source)
            assertion = _parse_step_assertion(seg[i + 1 : close], source)
            i = close + 1
        steps.append(Step(index=index, assertion=assertion))

    offset: CharOffset | None = None
    if i < n:
        ch = seg[i]
        if ch == "~":
            raise CfiParseError(source, "temporal offsets ('~') are not supported")
        if ch == "@":
            raise CfiParseError(source, "spatial offsets ('@') are not supported")
        if ch == ":":
            offset, i = _parse_offset(seg, i, source)
        else:
            raise CfiParseError(source, f"unexpected character {ch!r} in {seg!r}")
    if i != n:
        raise CfiParseError(source, f"unexpected trailing content {seg[i:]!r} in {seg!r}")
    return steps, offset


def _parse_path(text: str, source: str, *, require_leading_step: bool) -> tuple[LocalPath, ...]:
    """Parse an ``!``-separated path expression into a tuple of local paths.

    ``require_leading_step`` is ``True`` for a full ``path`` (a standalone CFI or a
    range parent, which must begin with a step) and ``False`` for a relative
    ``local_path`` (a range ``start`` / ``end``, which may be a bare offset).
    """
    segments = _split_depth0(text, "!")
    local_paths: list[LocalPath] = []
    last = len(segments) - 1
    for i, seg in enumerate(segments):
        steps, offset = _parse_segment(seg, source)
        if offset is not None and i != last:
            raise CfiParseError(source, "an offset may only appear in the final local path")
        if not steps:
            if offset is None:
                raise CfiParseError(
                    source, "empty local path (check for a stray, leading, or trailing '!')"
                )
            if i > 0:
                raise CfiParseError(source, "'!' must be followed by at least one step")
            if require_leading_step:
                raise CfiParseError(source, "path must begin with a step")
        local_paths.append(LocalPath(steps=tuple(steps), offset=offset))
    return tuple(local_paths)


def parse_cfi(s: str) -> Cfi | CfiRange:
    """Parse an EPUB CFI 1.1 string into a :class:`Cfi` or :class:`CfiRange`.

    The ``epubcfi(...)`` wrapper is required. A single inner path yields a
    :class:`Cfi`; a three-part ``parent,start,end`` yields a :class:`CfiRange`. The
    range-splitting commas are only those at bracket depth zero (commas inside text
    assertions or escaped as ``^,`` do not split).

    Raises:
        CfiParseError: if the string is not a well-formed, in-scope CFI. The error
            always carries the offending input and a human-readable reason.
    """
    text = s.strip()
    if not text.startswith("epubcfi(") or not text.endswith(")"):
        raise CfiParseError(s, "must be wrapped in 'epubcfi(...)'")
    inner = text[len("epubcfi(") : -1]
    parts = _split_depth0(inner, ",")

    if len(parts) == 1:
        return Cfi(paths=_parse_path(parts[0], s, require_leading_step=True))

    if len(parts) == 3:
        if parts[0] == "":
            raise CfiParseError(s, "range parent path must not be empty")
        parent = _parse_path(parts[0], s, require_leading_step=True)
        start = _parse_path(parts[1], s, require_leading_step=False)
        end = _parse_path(parts[2], s, require_leading_step=False)
        return CfiRange(parent=Cfi(paths=parent), start=Cfi(paths=start), end=Cfi(paths=end))

    raise CfiParseError(
        s, f"expected a single path or a 3-part range, got {len(parts)} comma-separated parts"
    )
