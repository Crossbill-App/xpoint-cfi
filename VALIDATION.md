# Real-book validation pipeline

Automated cross-validation of the xpoint → CFI conversion against real KOReader
annotations. The corpus lives in `test-books/` (git-ignored — personal EPUBs): one
KOReader `.sdr` directory per book containing the EPUB and its `metadata.epub.lua`
sidecar with highlights (`pos0`/`pos1` xpointers + the highlighted `text`).

## Pipeline

```
test-books/*.sdr
  │ 1. sidecar parse (lua → python via lupa)
  ▼
annotations (pos0, pos1, text, chapter, pageno)
  │ 2. xpoint_range_to_cfi_string(EpubMap, pos0, pos1)      [this library]
  ▼
range CFI per annotation
  │ 3a. library self-check: verify_range(book, cfi, text)   [this library]
  │ 3b. locator round-trip: xpoint -> locator -> xpoint     [this library]
  │ 3c. independent check: node + epub-cfi-resolver + jsdom [JS reference impl]
  ▼
validation/build/report.json + console summary; pytest wrapper per book
```

Step 3c is the point of the exercise: an independent JS implementation resolves our
CFIs against the same EPUB. If it extracts the same text KOReader recorded, the CFI
is interoperable, not just self-consistent.

## Locator round-trip (step 3b)

`_check_locator` in `runner.py` builds a Readium locator for the annotation's range and
resolves it back, asserting two things:

- the locator's `text.highlight` is the text KOReader recorded (that is what a web
  reader searches for), and
- the recovered `XPointRange` denotes that same text.

Equivalence is **by text, not by identical xpointers**. A text anchor may land at the end
of one text node where KOReader named the start of the next — the same place in the
document — and a quote ending in whitespace comes back a character shorter because
comparison strips it. Positional identity is still reported: per annotation as
`locator_exact`, and run-wide as a count alongside the `MatchConfidence` breakdown, so a
regression in it is visible without failing the run.

A locator produced from the same document should come back as `BOTH_CONTEXTS`; anything
weaker on a corpus book is worth looking at even when the text matched.

## Layout

```
validation/
  __init__.py
  sidecar.py       lua sidecar → Annotation list (lupa)
  runner.py        per-book pipeline: convert, self-check, JS check, report
  run.py           CLI driver: python validation/run.py [--no-js] [test-books-dir]
  js/
    package.json   epub-cfi-resolver + jsdom (node_modules git-ignored)
    resolve.mjs    jobs.json → results.json
  build/           work dir: extracted EPUBs, jobs/results, report (git-ignored)
tests/
  test_sidecar.py            unit tests with inline lua fixtures (CI-safe)
  test_validation_corpus.py  per-book corpus test; skipped when test-books/ absent
```

## Sidecar parsing (`validation/sidecar.py`)

- Execute the sidecar with `lupa` (`lua.execute` on the file content; it is a single
  `return {...}` of pure data) and read the `annotations` array: entries with both
  `pos0` and `pos1` are highlights (entries with only `page` are bookmarks — skip).
  Fields kept: `pos0`, `pos1`, `text`, `chapter`, `pageno`, `datetime`.
- Also read `cre_dom_version` (int) and `doc_props.title`. Reject sidecars without an
  `annotations` table (the pre-2024 `highlight` format is out of scope; report clearly).
- Skip macOS AppleDouble files (`._metadata.epub.lua`).
- `Annotation` frozen dataclass; `parse_sidecar(path) -> SidecarData`.

## JS contract (`validation/js/resolve.mjs`)

Input `jobs.json`:

```json
{"books": [{"id": "<slug>", "root": "/abs/extracted-epub-dir",
            "jobs": [{"index": 0, "cfi": "epubcfi(...)"}]}]}
```

`root` is the extracted (unzipped) EPUB directory. The script resolves each CFI with
`epub-cfi-resolver`, starting from `META-INF/container.xml` → OPF (fetch callback =
read file relative to `root`, parse with jsdom, XHTML as `application/xhtml+xml`) so
that the resolver independently interprets our package steps too. For a range CFI,
resolve start and end to `{node, offset}` and extract the text between them in
document order (TreeWalker over text nodes; slice first/last by offset). Ends in
different documents → `status: "cross-document"`.

Output `results.json`:

```json
{"books": [{"id": "<slug>", "results": [
  {"index": 0, "status": "ok", "text": "..."},
  {"index": 1, "status": "error", "error": "..."}]}]}
```

The script must never throw on a single job — every job yields a result row.

## Comparison

Both sides are normalized before comparing (helper shared in `runner.py`):
NFC-normalize, drop soft hyphens (U+00AD) and zero-width chars (U+200B–U+200D,
U+FEFF), NBSP → space, collapse whitespace runs to one space, strip. KOReader
collapses whitespace when storing highlight text, and crengine may hyphenate.

## Report

`runner.py` produces per book: total annotations, converted OK / conversion errors,
self-check pass/fail, locator pass/fail plus the exact-position count and confidence
tally, JS pass/fail/skipped, with per-failure detail (xpointers, CFI, expected vs
extracted text, error). JSON report to `validation/build/report.json` + aligned console
table. Exit non-zero when any annotation fails.

## Pytest integration

`test_validation_corpus.py` discovers `test-books/*.sdr` at collection time and
parametrizes one test per book (`pytest.skip` when the directory is missing/empty).
Each test runs the pipeline for that book; JS step is included when `node` and
`validation/js/node_modules` are present, otherwise the test only asserts conversion,
round-trip, self-check and the locator round-trip, and records the JS step as skipped.
Corpus tests are marked
`@pytest.mark.corpus`; the default unit suite stays fast and CI-safe.

## Notes

- All current corpus sidecars are `cre_dom_version = 20240114` (normalized
  xpointers); all 367 ranges are same-fragment.
- `epub-cfi-resolver` is AGPLv3 — dev-time validation tooling only, never a runtime
  dependency of the library.
- `lupa` is a dev-group dependency (sidecars are trusted local files).
