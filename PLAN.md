# xpoint-cfi — Implementation Plan

Standalone Python library that converts KOReader (crengine) xpointer position strings to
EPUB CFI (Canonical Fragment Identifiers, EPUB CFI 1.1) and back. Intended to be consumed
by crossbill-web's backend but has **zero** dependencies on it. Only runtime dependency: `lxml`.

## Why a document is required

Conversion is not string-to-string. The two formats use incompatible DOM coordinates:

| | KOReader XPoint | EPUB CFI |
|---|---|---|
| Document | `DocFragment[N]` — 1-based spine index | Package steps, e.g. `/6/14[chap05]!` |
| Element | XPath, siblings counted **per tag name** (`p[88]` = 88th `<p>` child) | Even index = 2 × position among **all** element children |
| Text | `text()[N]` = Nth existing text-node child (XPath semantics: `elem.text`, `child1.tail`, `child2.tail`, …) | Odd index = character-data *gap* between element children, numbered 1, 3, 5… whether or not text exists there; comments/PIs are invisible and do not split a chunk |
| Offset | Code-point offset within one text node | Offset within the whole chunk, in **UTF-16 code units** |

Mapping either direction therefore needs the parsed XHTML of the spine item, plus the OPF
for the package steps.

## Public API (exported from `xpoint_cfi/__init__.py`)

```python
from xpoint_cfi import EpubMap, XPoint, XPointRange, parse_cfi

book = EpubMap.from_bytes(epub_bytes)      # or EpubMap.from_path(path)

cfi_str  = book.xpoint_to_cfi("/body/DocFragment[14]/body/div/p[88]/text().223")
xp       = book.cfi_to_xpoint("epubcfi(/6/28[chap14]!/4/2/176/1:223)")   # -> XPoint
rng_str  = book.xpoint_range_to_cfi(start_xpoint_str, end_xpoint_str)    # range CFI
xp_rng   = book.cfi_to_xpoint_range(range_cfi_str)                       # -> XPointRange

# Verification (optional): does the CFI denote the expected text?
result = book.verify_range(rng_str, expected_text="the highlighted text")
# -> VerificationResult(ok: bool, extracted_text: str)
```

`XPoint.to_string()` / `XPointRange` round-trip back to KOReader string format.
String-level CFI parsing (`parse_cfi` / `Cfi.to_string()`) works without any EPUB.

## Modules

### 1. `exceptions.py`

`XpointCfiError` base; subclasses `XPointParseError`, `CfiParseError`, `ResolutionError`
(path does not resolve in the document), `EpubStructureError` (bad container/OPF/spine).
Every error message must include the offending string/step.

### 2. `xpoint.py` — KOReader xpointer value objects

Port of crossbill-web's `backend/src/domain/common/value_objects/xpoint.py` (frozen
dataclasses `XPoint`, `XPointRange` with `parse`/`to_string`), self-contained:

- Fields: `doc_fragment_index` (1-based spine index, default 1), `xpath` (element path,
  no `text()` part), `text_node_index` (1-based, default 1), `char_offset` (0-based
  code points, default 0).
- Accepted formats: `/body/DocFragment[12]/body/div/p[88]/text().223`,
  `/body/DocFragment[14]/body/a` (element boundary → offset 0),
  `/body/DocFragment[20]/body/div/p[1]/img.0`, missing `DocFragment` prefix,
  `text()` with and without `[N]`.
- Drop the crossbill `to_dict`/`from_dict` JSON plumbing — not needed here.
- Reject paths containing crengine internal boxing elements (`autoBoxing`, `floatBox`,
  `inlineBox`, `tabularBox`) with a dedicated error message: these are pre-2020
  un-normalized xpointers (KOReader DOM version < 20200223) and cannot resolve against
  the source XHTML.
- Helper `normalize_xpath(xpath) -> tuple[(tag, index_1based), ...]`: split into segments,
  default missing `[1]`.

### 3. `cfi.py` — CFI string layer (no document access)

AST + parser + serializer for EPUB CFI 1.1. Grammar subset:

- `epubcfi( path )` and range form `epubcfi( parent , start , end )`.
- Path = one or more *local paths* separated by `!` (indirection). Each local path is a
  sequence of steps `/<int>` with optional assertion `[...]`, ending optionally in a
  character offset `:<int>` with optional text-location assertion `[pre,post]`.
- Assertions: ID assertion `[id]`, text assertion `[pre,post]` / `[,post]`, and the
  spec's escaping with `^` for special chars (`^[ ^] ^( ^) ^, ^; ^^`). Parse and preserve
  them; parameter assertions (`;s=b`) may be parsed and ignored.
- Temporal/spatial offsets (`~`, `@`) are rejected with `CfiParseError` (out of scope).

Dataclasses (frozen): `Step(index: int, assertion: str | None)`,
`LocalPath(steps: tuple[Step, ...], offset: CharOffset | None)`,
`CharOffset(value: int, text_assertion: ... | None)`,
`Cfi(paths: tuple[LocalPath, ...])`,
`CfiRange(parent: Cfi, start: Cfi, end: Cfi)`.
`parse_cfi(s) -> Cfi | CfiRange`; both have `.to_string()`. Round-trip must be lossless
for the supported grammar. Provide document-order comparison (`sort_key()`) on `Cfi`
by numeric steps (assertions ignored), since CFI sortability is a feature we want.

### 4. `epub_map.py` — the document layer

`EpubMap` — parses the EPUB **once**, lazily maps spine items:

- `from_bytes(data: bytes)` / `from_path(path)`: open with stdlib `zipfile`. Read
  `META-INF/container.xml` → OPF path → parse OPF with lxml (**XML** parser).
  Store: position of `<spine>` among `<package>`'s element children (usually 3rd →
  CFI step 6 — compute, never hardcode), ordered list of `(idref, href)` for spine
  itemrefs (resolve manifest hrefs relative to the OPF directory, URL-decode).
- `spine_step(spine_index_1based) -> Step`: even index `2 * n` with `[idref]` assertion.
- `spine_index_for_step(step) -> int`: reverse; if the even index is out of range but the
  ID assertion matches an itemref idref, use the assertion (CFI self-repair), else
  `ResolutionError`.
- `doc(spine_index_1based) -> NodeMap`: parse the spine XHTML lazily, cache per index.
  Use `etree.XMLParser` first; on `XMLSyntaxError` retry with `recover=True`. Strip
  nothing; namespaces: match elements by local name (`local-name()`-style), since
  xpointer paths are namespace-free while XHTML has a default namespace.

`NodeMap` — per spine item, wraps the lxml tree and implements both coordinate systems:

- `element_by_xpath(segments) -> element`: descend from the document element by
  `(tag, n)` segments matching on local name. The first segment is `body`
  (xpoint paths start at `/body`), so descend `html → body` then the rest.
- `xpath_for_element(elem) -> segments`: reverse (count preceding same-local-name
  element siblings).
- `cfi_steps_for_element(elem) -> list[Step]`: walk document element → elem; each step
  `2 * (1-based position among element children)`; add `[id]` assertion when the element
  has an `id` attribute (spec requires it).
- `element_by_cfi_steps(steps) -> element`: reverse; on ID-assertion mismatch with the
  reached element, fall back to document-wide `id` lookup (self-repair), else error.
- **Chunk model** (the heart of the conversion). For an element, build an ordered list of
  child *slots*: element children partition the character data into gaps
  `gap_0` (before 1st element child), `gap_k` (after k-th), each gap having CFI odd index
  `2k + 1`. Within a gap, the actual lxml text nodes are: for `gap_0`, `elem.text` plus
  the `.tail` of any leading comments/PIs; for `gap_k`, the tail chain starting at the
  k-th *element* child through subsequent comment/PI siblings. Comments and PIs do
  **not** count as element children for CFI, but in lxml they are children with tails —
  they split a gap into multiple text nodes while CFI sees one chunk.
  Build per element on demand:
  `chunks(elem) -> list[Chunk]` where
  `Chunk(odd_index, parts: list[TextPart])`,
  `TextPart(node, attr: "text"|"tail", text: str, xpath_text_index: int | None)` —
  `xpath_text_index` is the node's 1-based position in XPath `text()` counting
  (only `elem.text` and tails of *element* children count for XPath on the crengine
  side; **decide and document**: crengine drops comments at parse, so adjacent source
  text nodes split by a comment are likely a single crengine text node — map
  xpoint `text()[N]` onto the *chunk* sequence (gaps that contain any text), not the raw
  lxml node sequence, and treat the chunk's concatenated text as one node for offset
  purposes. This matches crengine better and is self-consistent for round-trips.)
- `text_position_to_cfi(elem, text_node_index, char_offset) -> (odd_index, utf16_offset)`
  and the reverse `cfi_to_text_position(elem, odd_index, utf16_offset)`.
  UTF-16 conversion: `utf16 = len(text[:cp_offset].encode("utf-16-le")) // 2`; reverse by
  decoding — implement both as small pure helpers with tests (surrogate-pair characters).
  When `char_offset` exceeds the chunk text length → `ResolutionError` with context.
- `extract_text(start: (elem, odd, off) | None, end: ... | None) -> str`: document-order
  text between two resolved positions (used by verification). Simple itertext-style walk.

### 5. `convert.py` — the converters

Free functions used by `EpubMap` methods:

- `xpoint_to_cfi(book, xpoint) -> Cfi`:
  1. `spine_step(xpoint.doc_fragment_index)`, prefix with the package spine element step.
  2. `NodeMap.element_by_xpath(...)`, then `cfi_steps_for_element`.
  3. If the xpoint has a text position (i.e. it is not a bare element boundary):
     append odd step + `:offset`. A bare element xpoint (offset 0, default text node,
     original string had no `.offset` suffix — `XPoint` must preserve a
     `has_text_position: bool` flag from parsing) maps to the element step only.
  4. Assemble `Cfi` with the `!` indirection between package path and document path.
- `cfi_to_xpoint(book, cfi) -> XPoint`: exact reverse. CFI ending at an element
  step → element-boundary xpoint. CFI pointing at the package level only → error.
- `xpoint_range_to_cfi(book, start, end) -> CfiRange`: convert both ends, factor the
  longest common step prefix into the parent path (the parent path must not be empty —
  at minimum it holds the package steps; if the two ends are in different spine items the
  common prefix ends before `!`).
- `cfi_range_to_xpoint_range(book, rng) -> XPointRange`: rejoin parent + subpaths, convert.

### 6. `verify.py`

`verify_range(book, cfi_range, expected_text) -> VerificationResult`: resolve the range,
extract text via `NodeMap.extract_text` (across spine items when needed: rest of start
doc + full intermediate docs + head of end doc), normalize whitespace on both sides
(collapse runs, strip), compare. This is the crengine-fidelity safety net: callers
(crossbill) store the highlighted text and can flag conversions that don't reproduce it.

## Testing (pytest)

- `tests/conftest.py`: **fixture EPUB builder** — helper that assembles a minimal valid
  EPUB in memory with `zipfile` from a dict of XHTML strings (generates container.xml,
  OPF with correct spine order, mimetype). All document tests use synthetic books; no
  binary fixtures in the repo.
- Unit tests per module: xpoint parse/serialize round-trips (all accepted formats +
  rejects, boxing-element reject); CFI grammar round-trips incl. assertions, escaping,
  ranges, sort_key ordering; chunk model against hand-built XHTML with comments, PIs,
  empty gaps, nested elements, elements with `id`; UTF-16 offset helpers with emoji.
- Conversion tests: hand-verified expected CFI strings for known xpoints in a fixture
  book (compute expected values manually in the test comments); element-boundary
  xpoints; ranges within one element, across elements, across spine items.
- **Property tests (no extra deps, just randomized loops with a fixed seed)**: for every
  text position in a fixture book, xpoint → CFI → xpoint round-trip is identity
  (modulo `[1]` normalization); CFI sort order matches document order of positions.
- Verification tests: correct text extracted for ranges; mismatch detected when the
  document changes.

Quality gates, all must pass: `uv run ruff check .`, `uv run ruff format --check .`,
`uv run pyright`, `uv run pytest`.

## Out of scope (for now)

- Temporal/spatial CFI offsets, side bias parameters.
- TextQuoteSelector / W3C annotation export (future, thin layer on top of `extract_text`).
- Un-normalized (pre-2020) KOReader xpointers containing boxing elements.
- CLI.

## Implementation phases

1. **Scaffold + string layers** — `exceptions.py`, `xpoint.py`, `cfi.py` + their tests.
   Two independent work streams (xpoint vs cfi), no document access needed.
2. **Document layer** — `epub_map.py` (EpubMap, NodeMap, chunk model) + fixture EPUB
   builder + tests.
3. **Converters + verification** — `convert.py`, `verify.py`, public API in
   `__init__.py`, round-trip property tests, README with usage examples.

Each phase ends with all four quality gates green.
