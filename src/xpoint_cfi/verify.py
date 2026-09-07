"""Verification: does a converted range denote the text the caller expected?

Conversions between two crengine-vs-CFI coordinate systems can drift when a document's
structure is unusual. Callers (e.g. crossbill) store the highlighted text alongside the
xpointer, so :func:`verify_range` re-extracts the document text a CFI range denotes and
compares it — after whitespace normalization — against that stored text. A mismatch is a
signal that the conversion (or the stored range) is untrustworthy, not a hard error.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .cfi import CfiRange, parse_cfi
from .convert import cfi_range_to_xpoint_range
from .exceptions import ResolutionError
from .text_range import extract_between

if TYPE_CHECKING:
    from .epub_map import EpubMap

__all__ = [
    "VerificationResult",
    "normalize_for_comparison",
    "normalize_whitespace",
    "verify_range",
]


@dataclass(frozen=True)
class VerificationResult:
    """The outcome of :func:`verify_range`.

    Attributes:
        ok: Whether the extracted text matches the expected text after whitespace
            normalization.
        extracted_text: The raw text the range denotes, before normalization.
    """

    ok: bool
    extracted_text: str


def normalize_whitespace(s: str) -> str:
    """Collapse every run of whitespace to a single space and strip the ends."""
    return " ".join(s.split())


# Characters dropped entirely before comparison: soft hyphen and zero-width marks.
_ZERO_WIDTH = {"\u00ad", "\u200b", "\u200c", "\u200d", "\ufeff"}
_NBSP = "\u00a0"


def normalize_for_comparison(s: str) -> str:
    """Normalize text so cosmetic engine differences don't cause mismatches.

    NFC-normalize, drop soft hyphens and zero-width characters (crengine keeps soft
    hyphens in its DOM text but strips them from exported highlight text), turn no-break
    spaces into ordinary spaces, then collapse whitespace runs and strip.
    """
    s = unicodedata.normalize("NFC", s)
    s = s.replace(_NBSP, " ")
    s = "".join(ch for ch in s if ch not in _ZERO_WIDTH)
    return normalize_whitespace(s)


def verify_range(book: EpubMap, rng: CfiRange | str, expected_text: str) -> VerificationResult:
    """Extract the text a CFI range denotes and compare it to ``expected_text``.

    ``rng`` may be a :class:`CfiRange` or a range-CFI string. Extraction goes through
    :func:`~xpoint_cfi.text_range.extract_between`, so it spans spine items when the
    range crosses them.

    Raises:
        ResolutionError: if a range-CFI string parses to a non-range CFI, or if either
            end does not resolve against the book.
    """
    if isinstance(rng, str):
        parsed = parse_cfi(rng)
        if not isinstance(parsed, CfiRange):
            raise ResolutionError(rng, "expected a range CFI (epubcfi(parent,start,end))")
        rng = parsed

    xpoint_range = cfi_range_to_xpoint_range(book, rng)

    extracted = extract_between(book, xpoint_range.start, xpoint_range.end)
    ok = normalize_for_comparison(extracted) == normalize_for_comparison(expected_text)
    return VerificationResult(ok=ok, extracted_text=extracted)
