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

**Layers you can use standalone:**

- `EpubMap` / `NodeMap` — the parsed document and its coordinate systems.
- `XPoint`, `XPointRange`, `normalize_xpath` — KOReader xpointer value objects
  (`.parse()` / `.to_string()`), no EPUB needed.
- `Cfi`, `CfiRange`, `Step`, `LocalPath`, `CharOffset`, `TextAssertion`, `parse_cfi` —
  the CFI string layer (parse / `to_string()` / `Cfi.sort_key()`), no EPUB needed.

**Verification:** `verify_range(book, CfiRange | str, expected_text) -> VerificationResult`
with fields `ok: bool` and `extracted_text: str`, plus the `normalize_whitespace(s)`
helper it uses.

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

## Development

```bash
uv sync                     # install deps + dev tools
uv run pytest               # tests
uv run ruff check .         # lint
uv run ruff format --check .# formatting
uv run pyright              # type checking (strict)
```
