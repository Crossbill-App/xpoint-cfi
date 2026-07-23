"""CLI entry point for the real-book validation pipeline.

Usage::

    uv run python -m validation.run [corpus_dir] [--no-js] [--build-dir DIR]

Discovers every ``*.sdr`` book under ``corpus_dir`` (default ``test-books/``), runs the
convert / self-check / optional JS cross-check pipeline, writes a JSON report, prints a
summary table, and exits non-zero when any annotation failed an attempted stage.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .runner import discover_books, print_summary, validate_book, write_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="validation.run", description=__doc__)
    parser.add_argument(
        "corpus_dir",
        nargs="?",
        default="test-books",
        type=Path,
        help="directory of *.sdr KOReader book folders (default: test-books/)",
    )
    parser.add_argument("--no-js", action="store_true", help="skip the Node cross-check step")
    parser.add_argument(
        "--build-dir",
        default=Path("validation/build"),
        type=Path,
        help="work directory for extracted EPUBs and reports (default: validation/build/)",
    )
    args = parser.parse_args(argv)

    corpus_dir: Path = args.corpus_dir
    build_dir: Path = args.build_dir
    run_js: bool = not args.no_js

    books = discover_books(corpus_dir)
    if not books:
        print(f"No books found under {corpus_dir}", file=sys.stderr)
        return 1

    reports = [validate_book(book, build_dir, run_js=run_js) for book in books]

    report_path = build_dir / "report.json"
    write_report(reports, report_path)
    print_summary(reports)
    print(f"\nReport written to {report_path}")

    return 1 if any(r.any_failure for r in reports) else 0


if __name__ == "__main__":
    raise SystemExit(main())
