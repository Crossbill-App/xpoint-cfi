"""Per-book validation pipeline: convert, self-check, optional JS cross-check, report.

For each KOReader highlight in a book's sidecar this:

1. converts the ``pos0``/``pos1`` xpointer range to a range CFI with the library;
2. self-checks the CFI with :func:`xpoint_cfi.verify_range` (does it re-extract the
   recorded text?);
3. builds a Readium locator for the same range and resolves it back, checking that its
   quote is what KOReader recorded and that the recovered range denotes the same text;
4. optionally resolves the same CFI with an independent Node/``epub-cfi-resolver``
   reference implementation and compares its extracted text.

Texts are compared only after :func:`normalize_for_comparison` folds away the cosmetic
differences (hyphenation, zero-width marks, whitespace) between the two engines.
"""

from __future__ import annotations

import glob
import json
import re
import shutil
import subprocess
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

from xpoint_cfi import (
    EpubMap,
    XpointCfiError,
    cfi_to_xpoint_range_strings,
    locator_to_xpoint_range,
    normalize_for_comparison,
    normalize_whitespace,
    verify_range,
    xpoint_range_to_cfi_string,
    xpoint_range_to_locator,
)
from xpoint_cfi.text_range import extract_between
from xpoint_cfi.xpoint import XPoint, normalize_xpath

from .sidecar import as_dict, as_list, parse_sidecar

__all__ = [
    "AnnotationResult",
    "BookReport",
    "BookUnderTest",
    "discover_books",
    "js_available",
    "normalize_for_comparison",
    "print_summary",
    "validate_book",
    "write_report",
]

_RESOLVE_MJS = Path(__file__).resolve().parent / "js" / "resolve.mjs"
_JS_NODE_MODULES = Path(__file__).resolve().parent / "js" / "node_modules"


@dataclass(frozen=True)
class BookUnderTest:
    """A single discovered book: its slug and the two source paths."""

    slug: str
    epub_path: Path
    sidecar_path: Path


@dataclass
class AnnotationResult:
    """The outcome of every attempted stage for one highlight."""

    index: int
    pos0: str
    pos1: str
    cfi: str | None
    conversion_status: str  # "ok" | "conversion-error"
    conversion_error: str | None = None
    roundtrip_status: str | None = None  # "pass" | "fail" | None (not attempted)
    roundtrip_detail: str | None = None  # populated on round-trip failure
    self_check_status: str | None = None  # "pass" | "fail" | None (not attempted)
    extracted_text: str | None = None  # populated on self-check failure/error
    locator_status: str | None = None  # "pass" | "fail" | None (not attempted)
    locator_detail: str | None = None  # populated on locator failure
    locator_confidence: str | None = None  # MatchConfidence name of the reverse match
    locator_exact: bool | None = None  # reverse match landed on the original xpointers
    js_status: str | None = None  # "ok" | "mismatch" | "error" | None (not attempted)
    js_text: str | None = None
    js_error: str | None = None
    expected_text: str = ""

    @property
    def failed(self) -> bool:
        """True when any attempted stage did not succeed."""
        return (
            self.conversion_status != "ok"
            or self.roundtrip_status == "fail"
            or self.self_check_status == "fail"
            or self.locator_status == "fail"
            or self.js_status in ("mismatch", "error")
        )


@dataclass
class BookReport:
    """Aggregate counts and per-annotation detail for one book."""

    slug: str
    title: str | None
    cre_dom_version: int | None
    epub_path: str
    results: list[AnnotationResult] = field(default_factory=list[AnnotationResult])
    js_ran: bool = False
    js_skip_reason: str | None = None
    pairing_warning: str | None = None

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def conversion_ok(self) -> int:
        return sum(1 for r in self.results if r.conversion_status == "ok")

    @property
    def conversion_errors(self) -> int:
        return sum(1 for r in self.results if r.conversion_status != "ok")

    @property
    def roundtrip_ok(self) -> int:
        return sum(1 for r in self.results if r.roundtrip_status == "pass")

    @property
    def roundtrip_fail(self) -> int:
        return sum(1 for r in self.results if r.roundtrip_status == "fail")

    @property
    def self_check_ok(self) -> int:
        return sum(1 for r in self.results if r.self_check_status == "pass")

    @property
    def self_check_fail(self) -> int:
        return sum(1 for r in self.results if r.self_check_status == "fail")

    @property
    def locator_ok(self) -> int:
        return sum(1 for r in self.results if r.locator_status == "pass")

    @property
    def locator_fail(self) -> int:
        return sum(1 for r in self.results if r.locator_status == "fail")

    @property
    def locator_exact(self) -> int:
        """How many locator round-trips recovered the original xpointers exactly."""
        return sum(1 for r in self.results if r.locator_exact)

    @property
    def locator_confidence_counts(self) -> dict[str, int]:
        """Tally of the confidence each locator round-trip came back with."""
        counts: dict[str, int] = {}
        for result in self.results:
            if result.locator_confidence is not None:
                counts[result.locator_confidence] = counts.get(result.locator_confidence, 0) + 1
        return counts

    @property
    def js_ok(self) -> int:
        return sum(1 for r in self.results if r.js_status == "ok")

    @property
    def js_mismatch(self) -> int:
        return sum(1 for r in self.results if r.js_status == "mismatch")

    @property
    def js_error(self) -> int:
        return sum(1 for r in self.results if r.js_status == "error")

    @property
    def failures(self) -> list[AnnotationResult]:
        return [r for r in self.results if r.failed]

    @property
    def any_failure(self) -> bool:
        return any(r.failed for r in self.results)


def _xpoint_equivalence(original: str, roundtripped: str) -> str | None:
    """Return ``None`` when the round-tripped xpointer is equivalent to the original.

    Equivalence is positional: same spine fragment, same element path modulo implicit
    ``[1]`` indices, same text node and collapsed-space offset. One asymmetry is
    accepted by design: an original with a text position on a textless element (the
    ``img.0`` shape, ``text()[1].0``) degrades to an element-boundary xpointer.

    Returns a human-readable difference description otherwise.
    """
    orig = XPoint.parse(original)
    rt = XPoint.parse(roundtripped)
    if orig.doc_fragment_index != rt.doc_fragment_index:
        return f"fragment {orig.doc_fragment_index} != {rt.doc_fragment_index}"
    if normalize_xpath(orig.xpath) != normalize_xpath(rt.xpath):
        return f"xpath {orig.xpath!r} != {rt.xpath!r}"
    if orig.has_text_position != rt.has_text_position:
        textless_degrade = (
            orig.has_text_position
            and not rt.has_text_position
            and orig.text_node_index == 1
            and orig.char_offset == 0
        )
        if textless_degrade:
            return None
        return f"text-position presence differs ({original!r} vs {roundtripped!r})"
    if orig.text_node_index != rt.text_node_index:
        return f"text node {orig.text_node_index} != {rt.text_node_index}"
    if orig.char_offset != rt.char_offset:
        return f"offset {orig.char_offset} != {rt.char_offset}"
    return None


def _slugify(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower()
    return slug or "book"


def discover_books(corpus_dir: Path) -> list[BookUnderTest]:
    """Find every valid ``*.sdr`` book directory under ``corpus_dir``.

    A directory qualifies when it holds exactly one ``*.epub`` (ignoring macOS
    AppleDouble ``._`` files) and a ``metadata.epub.lua`` sidecar.
    """
    books: list[BookUnderTest] = []
    for sdr in sorted(corpus_dir.glob("*.sdr")):
        if not sdr.is_dir():
            continue
        epubs = [p for p in sdr.glob("*.epub") if not p.name.startswith("._")]
        sidecar = sdr / "metadata.epub.lua"
        if len(epubs) != 1 or not sidecar.is_file():
            continue
        books.append(
            BookUnderTest(slug=_slugify(sdr.stem), epub_path=epubs[0], sidecar_path=sidecar)
        )
    return books


def _version_key(node_path: str) -> tuple[int, ...]:
    match = re.search(r"/v(\d+)\.(\d+)\.(\d+)/", node_path)
    return tuple(int(g) for g in match.groups()) if match else (0, 0, 0)


def _find_node() -> str | None:
    node = shutil.which("node")
    if node:
        return node
    candidates = glob.glob(str(Path.home() / ".nvm" / "versions" / "node" / "*" / "bin" / "node"))
    if not candidates:
        return None
    return max(candidates, key=_version_key)


def js_available() -> bool:
    """True when the Node cross-check can run: node, ``resolve.mjs`` and ``node_modules``."""
    return _find_node() is not None and _RESOLVE_MJS.is_file() and _JS_NODE_MODULES.is_dir()


def validate_book(book: BookUnderTest, build_dir: Path, *, run_js: bool) -> BookReport:
    """Run the full pipeline for one book and return its :class:`BookReport`."""
    sidecar = parse_sidecar(book.sidecar_path)
    report = BookReport(
        slug=book.slug,
        title=sidecar.title,
        cre_dom_version=sidecar.cre_dom_version,
        epub_path=str(book.epub_path),
    )

    if sidecar.doc_path is not None:
        sidecar_basename = sidecar.doc_path.rsplit("/", 1)[-1]
        if sidecar_basename != book.epub_path.name:
            report.pairing_warning = (
                f"sidecar was created for {sidecar_basename!r} but the folder contains "
                f"{book.epub_path.name!r} — xpointers will not resolve against this EPUB"
            )

    epub_map = EpubMap.from_path(book.epub_path)

    for index, ann in enumerate(sidecar.annotations):
        result = AnnotationResult(
            index=index,
            pos0=ann.pos0,
            pos1=ann.pos1,
            cfi=None,
            conversion_status="ok",
            expected_text=ann.text,
        )
        try:
            result.cfi = xpoint_range_to_cfi_string(epub_map, ann.pos0, ann.pos1)
        except XpointCfiError as exc:
            result.conversion_status = "conversion-error"
            result.conversion_error = f"{type(exc).__name__}: {exc}"
            report.results.append(result)
            continue

        try:
            rt0, rt1 = cfi_to_xpoint_range_strings(epub_map, result.cfi)
            diff0 = _xpoint_equivalence(ann.pos0, rt0)
            diff1 = _xpoint_equivalence(ann.pos1, rt1)
            if diff0 is None and diff1 is None:
                result.roundtrip_status = "pass"
            else:
                result.roundtrip_status = "fail"
                details = [d for d in (diff0 and f"start: {diff0}", diff1 and f"end: {diff1}") if d]
                result.roundtrip_detail = "; ".join(details) + f" (got {rt0!r} -> {rt1!r})"
        except XpointCfiError as exc:
            result.roundtrip_status = "fail"
            result.roundtrip_detail = f"<{type(exc).__name__}: {exc}>"

        try:
            verification = verify_range(epub_map, result.cfi, expected_text=ann.text)
            if verification.ok:
                result.self_check_status = "pass"
            else:
                result.self_check_status = "fail"
                result.extracted_text = verification.extracted_text
        except XpointCfiError as exc:
            result.self_check_status = "fail"
            result.extracted_text = f"<{type(exc).__name__}: {exc}>"

        _check_locator(epub_map, ann.pos0, ann.pos1, ann.text, result)

        report.results.append(result)

    if run_js:
        _run_js_stage(book, report, build_dir)
    else:
        report.js_skip_reason = "JS step disabled"

    return report


def _check_locator(
    epub_map: EpubMap, pos0: str, pos1: str, expected_text: str, result: AnnotationResult
) -> None:
    """Run the Readium locator stage for one annotation and record the outcome.

    Two things must hold. The locator's quote has to be the text KOReader recorded —
    that is what a web reader will search for — and resolving the locator back has to
    land on a range denoting the same text. Positional identity with the original
    xpointers is recorded as ``locator_exact`` but is *not* required: a text anchor may
    legitimately land at the end of one text node where KOReader named the start of the
    next, which is the same place in the document.
    """
    expected = normalize_for_comparison(expected_text)
    try:
        locator = xpoint_range_to_locator(epub_map, pos0, pos1)
        problems: list[str] = []
        quote = locator.text.highlight or ""
        if normalize_for_comparison(quote) != expected:
            problems.append(f"quote differs (got {_truncate(quote)!r})")

        match = locator_to_xpoint_range(epub_map, locator)
        result.locator_confidence = match.confidence.name
        recovered = extract_between(epub_map, match.xpoint_range.start, match.xpoint_range.end)
        if normalize_for_comparison(recovered) != expected:
            problems.append(f"round-trip text differs (got {_truncate(recovered)!r})")

        rt0 = match.xpoint_range.start.to_string()
        rt1 = match.xpoint_range.end.to_string()
        result.locator_exact = (
            _xpoint_equivalence(pos0, rt0) is None and _xpoint_equivalence(pos1, rt1) is None
        )
        if problems:
            result.locator_status = "fail"
            result.locator_detail = f"{'; '.join(problems)} (via {rt0!r} -> {rt1!r})"
        else:
            result.locator_status = "pass"
    except XpointCfiError as exc:
        result.locator_status = "fail"
        result.locator_detail = f"<{type(exc).__name__}: {exc}>"


def _run_js_stage(book: BookUnderTest, report: BookReport, build_dir: Path) -> None:
    """Resolve each converted CFI with the Node reference impl and record the outcome."""
    node = _find_node()
    if node is None:
        report.js_skip_reason = "node executable not found"
        return
    if not _RESOLVE_MJS.is_file():
        report.js_skip_reason = f"missing {_RESOLVE_MJS}"
        return
    if not _JS_NODE_MODULES.is_dir():
        report.js_skip_reason = f"missing {_JS_NODE_MODULES} (run npm install)"
        return

    book_dir = build_dir / book.slug
    epub_dir = book_dir / "epub"
    _extract_epub(book.epub_path, epub_dir)

    jobs = [
        {"index": r.index, "cfi": r.cfi}
        for r in report.results
        if r.conversion_status == "ok" and r.cfi is not None
    ]
    if not jobs:
        report.js_ran = True
        return

    jobs_path = book_dir / "jobs.json"
    results_path = book_dir / "results.json"
    jobs_payload = {"books": [{"id": book.slug, "root": str(epub_dir.resolve()), "jobs": jobs}]}
    jobs_path.write_text(json.dumps(jobs_payload), encoding="utf-8")

    proc = subprocess.run(
        [node, str(_RESOLVE_MJS), str(jobs_path), str(results_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not results_path.is_file():
        detail = (proc.stderr or proc.stdout or "no output").strip()
        report.js_skip_reason = f"resolve.mjs failed (exit {proc.returncode}): {detail[:200]}"
        return

    by_index = _read_js_results(results_path, book.slug)
    report.js_ran = True
    for result in report.results:
        if result.conversion_status != "ok":
            continue
        row = by_index.get(result.index)
        if row is None:
            result.js_status = "error"
            result.js_error = "no result row returned"
            continue
        if row.get("status") == "ok":
            text = row.get("text")
            js_text = text if isinstance(text, str) else ""
            result.js_text = js_text
            if normalize_for_comparison(js_text) == normalize_for_comparison(result.expected_text):
                result.js_status = "ok"
            else:
                result.js_status = "mismatch"
        else:
            result.js_status = "error"
            error = row.get("error")
            result.js_error = error if isinstance(error, str) else str(row.get("status"))


def _extract_epub(epub_path: Path, dest: Path) -> None:
    if dest.exists():
        return
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path) as archive:
        archive.extractall(dest)


def _read_js_results(results_path: Path, slug: str) -> dict[int, dict[object, object]]:
    payload: object = json.loads(results_path.read_text(encoding="utf-8"))
    out: dict[int, dict[object, object]] = {}
    for entry in as_list(as_dict(payload).get("books")):
        entry_dict = as_dict(entry)
        if entry_dict.get("id") != slug:
            continue
        for row in as_list(entry_dict.get("results")):
            row_dict = as_dict(row)
            index = row_dict.get("index")
            if isinstance(index, int):
                out[index] = row_dict
    return out


def write_report(reports: list[BookReport], path: Path) -> None:
    """Serialize the reports (counts + per-annotation detail) to JSON at ``path``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "books": [
            {
                "slug": r.slug,
                "title": r.title,
                "cre_dom_version": r.cre_dom_version,
                "epub_path": r.epub_path,
                "total": r.total,
                "conversion_ok": r.conversion_ok,
                "conversion_errors": r.conversion_errors,
                "roundtrip_ok": r.roundtrip_ok,
                "roundtrip_fail": r.roundtrip_fail,
                "self_check_ok": r.self_check_ok,
                "self_check_fail": r.self_check_fail,
                "locator_ok": r.locator_ok,
                "locator_fail": r.locator_fail,
                "locator_exact": r.locator_exact,
                "locator_confidence": r.locator_confidence_counts,
                "js_ran": r.js_ran,
                "js_skip_reason": r.js_skip_reason,
                "pairing_warning": r.pairing_warning,
                "js_ok": r.js_ok,
                "js_mismatch": r.js_mismatch,
                "js_error": r.js_error,
                "annotations": [asdict(a) for a in r.results],
            }
            for r in reports
        ]
    }
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


def _truncate(text: str, limit: int = 120) -> str:
    collapsed = normalize_whitespace(text)
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def print_summary(reports: list[BookReport]) -> None:
    """Print an aligned per-book table plus a detail line for each failure."""
    headers = ("book", "annots", "conv-ok", "rt-ok", "self-ok", "loc-ok", "js-ok", "failures")
    rows: list[tuple[str, ...]] = []
    for r in reports:
        js_cell = str(r.js_ok) if r.js_ran else "skip"
        rows.append(
            (
                r.slug,
                str(r.total),
                f"{r.conversion_ok}/{r.total}",
                f"{r.roundtrip_ok}/{r.conversion_ok}",
                f"{r.self_check_ok}/{r.conversion_ok}",
                f"{r.locator_ok}/{r.conversion_ok}",
                js_cell,
                str(len(r.failures)),
            )
        )

    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def fmt(row: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row))

    print(fmt(headers))
    print("  ".join("-" * w for w in widths))
    for row in rows:
        print(fmt(row))

    totals: dict[str, int] = {}
    exact = 0
    for r in reports:
        exact += r.locator_exact
        for name, count in r.locator_confidence_counts.items():
            totals[name] = totals.get(name, 0) + count
    if totals:
        breakdown = ", ".join(f"{name.lower()}={count}" for name, count in sorted(totals.items()))
        resolved = sum(totals.values())
        print(f"\nlocator match confidence: {breakdown}")
        print(f"locator round-trips landing on the original xpointers: {exact}/{resolved}")

    for r in reports:
        if r.pairing_warning:
            print(f"\n[{r.slug}] WARNING: {r.pairing_warning}")
        if r.js_skip_reason and not r.js_ran:
            print(f"\n[{r.slug}] JS step skipped: {r.js_skip_reason}")
        if not r.failures:
            continue
        print(f"\n[{r.slug}] {len(r.failures)} failure(s):")
        for a in r.failures:
            print(f"  #{a.index} {a.pos0} -> {a.pos1}")
            if a.conversion_status != "ok":
                print(f"      conversion-error: {a.conversion_error}")
                continue
            print(f"      cfi: {a.cfi}")
            if a.roundtrip_status == "fail":
                print(f"      round-trip: {a.roundtrip_detail}")
            if a.self_check_status == "fail":
                print(f"      self-check expected: {_truncate(a.expected_text)}")
                print(f"      self-check extracted: {_truncate(a.extracted_text or '')}")
            if a.locator_status == "fail":
                print(f"      locator: {a.locator_detail}")
            if a.js_status == "mismatch":
                print(f"      js expected: {_truncate(a.expected_text)}")
                print(f"      js extracted: {_truncate(a.js_text or '')}")
            elif a.js_status == "error":
                print(f"      js error: {a.js_error}")
