"""Per-book corpus validation, parametrized over ``test-books/*.sdr``.

Books are discovered at collection time; the module is skipped when the corpus is absent
so CI stays fast. The Node cross-check runs only when node, ``resolve.mjs`` and its
``node_modules`` are all present, otherwise each test only checks conversion + self-check
and warns that the JS step was skipped.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from validation.runner import BookUnderTest, discover_books, js_available, validate_book

_CORPUS_DIR = Path(__file__).resolve().parent.parent / "test-books"
_BOOKS = discover_books(_CORPUS_DIR) if _CORPUS_DIR.is_dir() else []

if not _BOOKS:
    pytest.skip("no corpus books found under test-books/", allow_module_level=True)


@pytest.fixture(scope="session")
def corpus_build_dir() -> Path:
    build_dir = Path(__file__).resolve().parent.parent / "validation" / "build" / "pytest"
    build_dir.mkdir(parents=True, exist_ok=True)
    return build_dir


@pytest.mark.corpus
@pytest.mark.parametrize("book", _BOOKS, ids=[b.slug for b in _BOOKS])
def test_corpus_book(book: BookUnderTest, corpus_build_dir: Path) -> None:
    report = validate_book(book, corpus_build_dir, run_js=js_available())

    assert report.conversion_errors == 0, (
        f"{report.conversion_errors} conversion error(s): "
        + "; ".join(
            f"#{f.index} {f.conversion_error}" for f in report.failures if f.conversion_error
        )
    )
    assert report.self_check_fail == 0, (
        f"{report.self_check_fail} self-check failure(s): "
        + "; ".join(
            f"#{f.index} {f.pos0}->{f.pos1}"
            for f in report.failures
            if f.self_check_status == "fail"
        )
    )

    if report.js_ran:
        assert report.js_mismatch == 0, f"{report.js_mismatch} JS mismatch(es): " + "; ".join(
            f"#{f.index} {f.pos0}->{f.pos1}" for f in report.failures if f.js_status == "mismatch"
        )
    else:
        warnings.warn(
            f"JS cross-check skipped for {book.slug}: {report.js_skip_reason}",
            stacklevel=2,
        )
