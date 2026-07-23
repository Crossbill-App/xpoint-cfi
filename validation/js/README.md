# Independent JS CFI resolver

The JavaScript half of the validation pipeline (see `../VALIDATION.md`, "JS
contract"). It resolves range CFIs produced by this library against a real
(extracted) EPUB using [`epub-cfi-resolver`](https://www.npmjs.com/package/epub-cfi-resolver)
and [`jsdom`](https://www.npmjs.com/package/jsdom), then extracts the text each
CFI denotes. If the extracted text matches what KOReader recorded, our CFI is
interoperable — not just self-consistent.

`epub-cfi-resolver` is AGPLv3 and used here as **dev-time validation tooling
only**, never as a runtime dependency of the library.

## What it does

`resolve.mjs` reads a `jobs.json` and writes a `results.json`.

Input:

```json
{"books": [{"id": "<slug>", "root": "/abs/extracted-epub-dir",
            "jobs": [{"index": 0, "cfi": "epubcfi(...)"}]}]}
```

`root` is the unzipped EPUB directory. For each job the script parses
`META-INF/container.xml` → the OPF, hands the OPF to the resolver with a fetch
callback that reads spine/manifest hrefs relative to `root` (XHTML parsed as
`application/xhtml+xml`, falling back to `text/html`), resolves the range's
start and end `{node, offset}`, and extracts the text between them in document
order (UTF-16 code-unit offsets, sliced directly).

Output:

```json
{"books": [{"id": "<slug>", "results": [
  {"index": 0, "status": "ok", "text": "..."},
  {"index": 1, "status": "error", "error": "..."},
  {"index": 2, "status": "cross-document"}]}]}
```

Every job yields exactly one row. A single job's failure never aborts the run:
its row gets `status: "error"` with the message. `status` is one of `ok`
(text present), `error` (error present), or `cross-document` (range ends land in
different documents). Non-range CFIs return `error` ("not a range CFI").

## Run standalone

```bash
# install deps (node_modules is git-ignored)
npm install

# resolve a jobs file
node resolve.mjs jobs.json results.json
# or: npm run resolve -- jobs.json results.json
```

Requires Node with ES-module support. `resolve.mjs` takes the input path as the
first CLI argument and the output path as the second.

## Smoke test

```bash
npm run smoke
```

`smoke.mjs` builds a tiny extracted-EPUB fixture in a temp dir, writes a
`jobs.json` with a hand-computed range CFI (crossing an element boundary and an
emoji, to prove UTF-16 slicing) plus two deliberately broken jobs, runs
`resolve.mjs`, and asserts the results. It exits non-zero on any failure.
