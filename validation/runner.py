"""Per-book validation pipeline: convert, self-check, optional JS cross-check, report.

For each KOReader highlight in a book's sidecar this:

1. converts the ``pos0``/``pos1`` xpointer range to a range CFI with the library;
2. self-checks the CFI with :func:`xpoint_cfi.verify_range` (does it re-extract the
   recorded text?);
3. optionally resolves the same CFI with an independent Node/``epub-cfi-resolver``
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
import unicodedata
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import cast

from xpoint_cfi import (
    EpubMap,
    XpointCfiError,
    normalize_whitespace,
    verify_range,
    xpoint_range_to_cfi_string,
)

from .sidecar import parse_sidecar

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

# Characters dropped entirely before comparison: soft hyphen and zero-width marks.
_ZERO_WIDTH = {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}
_NBSP = "\u00a0"


def normalize_for_comparison(s: str) -> str:
    """Normalize text so the two engines' cosmetic differences don't cause mismatches.

    NFC-normalize, drop soft hyphens and zero-width characters, turn no-break spaces into
    ordinary spaces, then collapse whitespace runs and strip. Extends the library's
    :func:`normalize_whitespace` (which only does the final collapse) with the Unicode
    folding the cross-engine comparison needs.
    """
    s = unicodedata.normalize("NFC", s)
    s = s.replace(_NBSP, " ")
    s = "".join(ch for ch in s if ch not in _ZERO_WIDTH)
    return normalize_whitespace(s)


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
    self_check_status: str | None = None  # "pass" | "fail" | None (not attempted)
    extracted_text: str | None = None  # populated on self-check failure/error
    js_status: str | None = None  # "ok" | "mismatch" | "error" | None (not attempted)
    js_text: str | None = None
    js_error: str | None = None
    expected_text: str = ""

    @property
    def failed(self) -> bool:
        """True when any attempted stage did not succeed."""
        return (
            self.conversion_status != "ok"
            or self.self_check_status == "fail"
            or self.js_status in ("mismatch", "error")
        )


def _new_results() -> list[AnnotationResult]:
    return []


@dataclass
class BookReport:
    """Aggregate counts and per-annotation detail for one book."""

    slug: str
    title: str | None
    cre_dom_version: int | None
    epub_path: str
    results: list[AnnotationResult] = field(default_factory=_new_results)
    js_ran: bool = False
    js_skip_reason: str | None = None

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
    def self_check_ok(self) -> int:
        return sum(1 for r in self.results if r.self_check_status == "pass")

    @property
    def self_check_fail(self) -> int:
        return sum(1 for r in self.results if r.self_check_status == "fail")

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
            verification = verify_range(epub_map, result.cfi, expected_text=ann.text)
            if verification.ok:
                result.self_check_status = "pass"
            else:
                result.self_check_status = "fail"
                result.extracted_text = verification.extracted_text
        except XpointCfiError as exc:
            result.self_check_status = "fail"
            result.extracted_text = f"<{type(exc).__name__}: {exc}>"

        report.results.append(result)

    if run_js:
        _run_js_stage(book, report, build_dir)
    else:
        report.js_skip_reason = "JS step disabled"

    return report


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


def _as_dict(value: object) -> dict[object, object]:
    return cast("dict[object, object]", value) if isinstance(value, dict) else {}


def _as_list(value: object) -> list[object]:
    return cast("list[object]", value) if isinstance(value, list) else []


def _read_js_results(results_path: Path, slug: str) -> dict[int, dict[object, object]]:
    payload: object = json.loads(results_path.read_text(encoding="utf-8"))
    out: dict[int, dict[object, object]] = {}
    for entry in _as_list(_as_dict(payload).get("books")):
        entry_dict = _as_dict(entry)
        if entry_dict.get("id") != slug:
            continue
        for row in _as_list(entry_dict.get("results")):
            row_dict = _as_dict(row)
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
                "self_check_ok": r.self_check_ok,
                "self_check_fail": r.self_check_fail,
                "js_ran": r.js_ran,
                "js_skip_reason": r.js_skip_reason,
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
    headers = ("book", "annots", "conv-ok", "self-ok", "js-ok", "failures")
    rows: list[tuple[str, ...]] = []
    for r in reports:
        js_cell = str(r.js_ok) if r.js_ran else "skip"
        rows.append(
            (
                r.slug,
                str(r.total),
                f"{r.conversion_ok}/{r.total}",
                f"{r.self_check_ok}/{r.conversion_ok}",
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

    for r in reports:
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
            if a.self_check_status == "fail":
                print(f"      self-check expected: {_truncate(a.expected_text)}")
                print(f"      self-check extracted: {_truncate(a.extracted_text or '')}")
            if a.js_status == "mismatch":
                print(f"      js expected: {_truncate(a.expected_text)}")
                print(f"      js extracted: {_truncate(a.js_text or '')}")
            elif a.js_status == "error":
                print(f"      js error: {a.js_error}")
