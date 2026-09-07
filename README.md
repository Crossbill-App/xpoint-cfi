# xpoint-cfi

> [!CAUTION]
> This is really experimental vibe-coded implementation for my personal/experimental use with Crossbill. Use at your own risk and expect that some books have content where positions do not convert properly!

Convert **KOReader (crengine) xpointer** position strings to **EPUB CFI**
(Canonical Fragment Identifiers, EPUB CFI 1.1) and back — in both directions, grounded
in the actual EPUB document. Pure Python; the only runtime dependency is `lxml`.

```text
/body/DocFragment[1]/body/div/p[3]/text().8   <->   epubcfi(/6/2[item1]!/4/2[intro]/6/1:9)
```

## Why a document is required

Conversion is **not** string-to-string. The two formats address the same DOM with
incompatible coordinate systems, so the parsed XHTML of the spine item (plus the OPF for
the package steps) is needed to translate between them:

| | KOReader XPoint | EPUB CFI |
|---|---|---|
| **Document** | `DocFragment[N]` — 1-based spine index | Package steps, e.g. `/6/14[chap05]!` |
| **Element** | XPath, siblings counted **per tag name** (`p[88]` = 88th `<p>` child) | Even index = 2 × position among **all** element children |
| **Text** | `text()[N]` = Nth existing text node | Odd index = character-data *gap* between element children, numbered 1, 3, 5… whether or not text exists there |
| **Offset** | Code-point offset within one text node | Offset within the whole chunk, in **UTF-16 code units** |

An emoji or other astral character therefore shifts the offset (one code point, two
UTF-16 units), and a comment splitting a paragraph's text is invisible to CFI — both are
handled by the document layer.

## Install

```bash
uv add xpoint-cfi                 # from a package index
uv add /path/to/xpoint-cfi        # from a local checkout
# or:  pip install /path/to/xpoint-cfi
```

## Quickstart

The simplest API is the set of string-in / string-out helpers. Each takes a parsed
`EpubMap` (parse the EPUB once, reuse it):

```python
from xpoint_cfi import (
    EpubMap,
    xpoint_to_cfi_string,
    cfi_to_xpoint_string,
    xpoint_range_to_cfi_string,
    cfi_to_xpoint_range_strings,
    verify_range,
)

book = EpubMap.from_bytes(epub_bytes)      # or EpubMap.from_path("book.epub")

# Single position, both directions
cfi = xpoint_to_cfi_string(book, "/body/DocFragment[1]/body/div/p[3]/text().8")
#  -> "epubcfi(/6/2[item1]!/4/2[intro]/6/1:9)"
xp  = cfi_to_xpoint_string(book, cfi)
#  -> "/body/DocFragment[1]/body/div[1]/p[3]/text().8"

# A highlighted range (start + end xpointer) -> a single range CFI
rng = xpoint_range_to_cfi_string(
    book,
    "/body/DocFragment[1]/body/div/p[1]/text().0",
    "/body/DocFragment[1]/body/div/p[3]/text().2",
)
#  -> "epubcfi(/6/2[item1]!/4/2[intro],/2/1:0,/6/1:2)"
start_xp, end_xp = cfi_to_xpoint_range_strings(book, rng)

# Verify a range denotes the text you expected (whitespace-tolerant)
result = verify_range(book, rng, expected_text="the highlighted text")
if not result.ok:
    print("conversion drifted; extracted:", result.extracted_text)
```

## Readium locators

A web reader built on [`@readium/navigator`](https://github.com/readium/ts-toolkit)
addresses positions with a [Readium
Locator](https://readium.org/architecture/models/locators/) — a resource `href`, a CSS
selector, and a text quote with its surrounding context — rather than with an xpointer or
a CFI. Converting both ways lets the same highlight be made on a KOReader device and
shown in a browser:

```python
from xpoint_cfi import (
    EpubMap, MatchConfidence,
    xpoint_to_locator, xpoint_range_to_locator, locator_to_xpoint_range,
)

locator = xpoint_range_to_locator(
    book,
    "/body/DocFragment[1]/body/div/p[3]/text().0",
    "/body/DocFragment[1]/body/div/p[3]/text().23",
)
locator.to_dict()
# {'href': 'OEBPS/chap1.xhtml',
#  'type': 'application/xhtml+xml',
#  'locations': {'progression': 0.5392156862745098,
#                'cssSelector': '#intro > p:nth-child(3)'},
#  'text': {'before': ' world.\nA hyphenated word appears here.\n',
#           'highlight': 'The cat sat on the mat.',
#           'after': '\nThe cat sat on the hat.'}}

# ...and back. The reverse is a *search*, so it grades how sure it is.
match = locator_to_xpoint_range(book, locator)          # accepts the dict too
if match.confidence >= MatchConfidence.ONE_CONTEXT:
    start, end = match.xpoint_range.start, match.xpoint_range.end
```

The two directions are deliberately asymmetric. Producing a locator is exact: the quote
is read out of the document through the same extraction `verify_range` uses, so a
locator and a verified CFI can never disagree about what a range says. Resolving one is a
search — the `cssSelector` only narrows *where* the match may land, and the quote is then
found by text so that a locator still resolves against a DOM a reader processed
differently. `MatchConfidence` is an ordered enum (`FUZZY` < `AMBIGUOUS` <
`HIGHLIGHT_ONLY` < `ONE_CONTEXT` < `BOTH_CONTEXTS`) so callers can set a floor and reject
weak matches instead of storing a bad position.

`xpoint_to_locator` handles a single position: `text.highlight` is `""` and the two
contexts meet at the point.

## API overview

Everything is exported from the top-level `xpoint_cfi` package.

**String helpers** (parse → convert → serialize):

- `xpoint_to_cfi_string(book, xpoint_str) -> str`
- `cfi_to_xpoint_string(book, cfi_str) -> str`
- `xpoint_range_to_cfi_string(book, start_str, end_str) -> str`
- `cfi_to_xpoint_range_strings(book, cfi_str) -> tuple[str, str]`

**Value-object converters** (for working with parsed objects directly):

- `xpoint_to_cfi(book, XPoint) -> Cfi` / `cfi_to_xpoint(book, Cfi) -> XPoint`
- `xpoint_range_to_cfi(book, XPointRange) -> CfiRange` /
  `cfi_range_to_xpoint_range(book, CfiRange) -> XPointRange`

**Readium locators:**

- `xpoint_to_locator(book, XPoint | str) -> Locator`
- `xpoint_range_to_locator(book, start, end) -> Locator` (each end an `XPoint` or string)
- `locator_to_xpoint_range(book, Locator | dict) -> LocatorMatch`
- `Locator`, `LocatorLocations`, `LocatorText` — frozen dataclasses with
  `to_dict()` / `from_dict()` for the Readium JSON shape.
- `LocatorMatch` (fields `xpoint_range`, `confidence`) and the `MatchConfidence` enum.

**Layers you can use standalone:**

- `EpubMap` / `NodeMap` — the parsed document and its coordinate systems.
- `XPoint`, `XPointRange`, `normalize_xpath` — KOReader xpointer value objects
  (`.parse()` / `.to_string()`), no EPUB needed.
- `Cfi`, `CfiRange`, `Step`, `LocalPath`, `CharOffset`, `TextAssertion`, `parse_cfi` —
  the CFI string layer (parse / `to_string()` / `Cfi.sort_key()`), no EPUB needed.
- `xpoint_cfi.css_selector` — `selector_for_element` / `resolve_selector` /
  `escape_css_identifier`, the `querySelector` subset Readium's generators emit.
- `xpoint_cfi.text_anchor` — `find_quote`, Hypothesis-style quote anchoring over plain
  strings, no EPUB needed.

**Verification:** `verify_range(book, CfiRange | str, expected_text) -> VerificationResult`
with fields `ok: bool` and `extracted_text: str`. Texts are compared via
`normalize_for_comparison(s)` (NFC, soft hyphens and zero-width marks dropped, NBSP →
space, whitespace collapsed), because crengine keeps soft hyphens in its DOM text but
strips them from exported highlight text; the plain `normalize_whitespace(s)` helper is
also exported.

**Errors** all derive from `XpointCfiError`: `XPointParseError`, `CfiParseError`,
`ResolutionError` (a parsed location doesn't resolve against the document),
`EpubStructureError` (bad container/OPF/spine).

## Limitations

- **Normalized xpointers only.** Pre-2020 KOReader xpointers (DOM version < 20200223)
  containing crengine boxing elements (`autoBoxing`, `floatBox`, `inlineBox`,
  `tabularBox`) cannot resolve against the source XHTML and are rejected.
- **No temporal (`~`) or spatial (`@`) CFI offsets**, and no side-bias parameters.
- **CFI parameter assertions** (`;name=value`) are parsed but dropped; every other
  supported construct round-trips losslessly.
- **Offset-on-element CFIs are unsupported.** A character offset must sit on an odd
  (text) step; an offset attached to an element step raises `ResolutionError`.
- Round-trips are identity **modulo `[1]` normalization** of the xpath (`p` ↔ `p[1]`).
- **Locators lose edge whitespace.** A quote is compared with whitespace stripped, so a
  range ending on a space comes back one character shorter. It denotes the same words,
  not the same byte span.
- **Locators are per-resource.** A range crossing spine items produces a locator that
  cannot be resolved back — no single resource contains the whole quote. KOReader does
  not produce such highlights in practice.
- **`locations` carries only `progression` and `cssSelector`.** `position`,
  `totalProgression` and `title` need publication-wide data an `EpubMap` does not have;
  `partialCfi` and `domRange` are not emitted (the CFI converters cover the former).

## Testing conversions against real books

The repo ships a validation pipeline that checks conversions against **real KOReader
annotations**: it reads highlights from KOReader's sidecar files, converts every
xpointer range to a CFI and to a Readium locator, and verifies the results five ways.
Design details live in [VALIDATION.md](VALIDATION.md).

### 1. Add books to `test-books/`

KOReader keeps its annotations in a sidecar directory next to each book:
`<Book name>.sdr/metadata.epub.lua`. Copy the whole `.sdr` directory from your device
into `test-books/` and put the **matching EPUB inside it**, so each entry looks like:

```text
test-books/
  My Book - Author.sdr/
    My Book - Author.epub        <- the exact EPUB the highlights were made in
    metadata.epub.lua            <- KOReader sidecar with the annotations
```

`test-books/` is git-ignored — personal books are never committed. Notes:

- The sidecar must use the modern `annotations` format (KOReader 2024+) and normalized
  xpointers (`cre_dom_version >= 20200223`); the pipeline reports older formats clearly.
- The EPUB must be the same file the highlights were made in. The pipeline compares the
  sidecar's recorded `doc_path` against the EPUB filename and warns loudly on a
  mismatch (a wrong file otherwise fails every annotation with opaque errors).

### 2. (Optional) install the independent JS referee

The strongest check resolves the generated CFIs with a *separate* implementation
([epub-cfi-resolver](https://github.com/fread-ink/epub-cfi-resolver) + jsdom in Node)
and compares the text it extracts against what KOReader recorded:

```bash
cd validation/js && npm install    # requires node; skipped automatically if absent
```

### 3. Run

```bash
uv run python -m validation.run                    # full pipeline, all books
uv run python -m validation.run --no-js            # Python-only stages
uv run python -m validation.run path/to/corpus     # a different corpus directory
uv run pytest -m corpus                            # same thing as a pytest suite
```

Every highlight goes through five stages:

1. **convert** — `pos0`/`pos1` xpointers → range CFI;
2. **round-trip** — CFI → xpointers again, compared positionally against the originals;
3. **self-check** — `verify_range` re-extracts the CFI's text and compares it with the
   highlight text KOReader stored;
4. **locator** — the same range → a Readium locator, whose quote must be the text
   KOReader stored, and back again, landing on a range denoting that same text;
5. **js** — the independent JS resolver extracts the same range (when installed).

The run prints a per-book table (`conv-ok` / `rt-ok` / `self-ok` / `loc-ok` / `js-ok`)
with a detail line for every failure (xpointers, CFI, expected vs. extracted text), a
run-wide breakdown of the confidence the locator matches came back with, the full report
to `validation/build/report.json`, and exits non-zero if anything failed — so a problem
book is caught just by dropping its `.sdr` into `test-books/` and running the pipeline.

## Development

```bash
uv sync                     # install deps + dev tools
uv run pytest               # unit tests (corpus tests skip without test-books/)
uv run ruff check .         # lint
uv run ruff format --check .# formatting
uv run pyright              # type checking (strict)
```
