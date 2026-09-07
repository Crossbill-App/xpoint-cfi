"""xpoint-cfi: convert KOReader (crengine) xpointers to EPUB CFI and back.

The public API is a small set of free functions that take a parsed :class:`EpubMap`
(the document is required — the two formats use incompatible DOM coordinates) plus the
value-object and string layers they build on.

Typical use goes through the string-in / string-out helpers::

    from xpoint_cfi import EpubMap, xpoint_to_cfi_string

    book = EpubMap.from_bytes(epub_bytes)
    cfi = xpoint_to_cfi_string(book, "/body/DocFragment[1]/body/div/p[3]/text().8")
"""

from .cfi import Cfi, CfiRange, CharOffset, LocalPath, Step, TextAssertion, parse_cfi
from .convert import (
    cfi_range_to_xpoint_range,
    cfi_to_xpoint,
    cfi_to_xpoint_range_strings,
    cfi_to_xpoint_string,
    xpoint_range_to_cfi,
    xpoint_range_to_cfi_string,
    xpoint_to_cfi,
    xpoint_to_cfi_string,
)
from .epub_map import EpubMap, NodeMap
from .exceptions import (
    CfiParseError,
    EpubStructureError,
    ResolutionError,
    XpointCfiError,
    XPointParseError,
)
from .locator import (
    Locator,
    LocatorLocations,
    LocatorMatch,
    LocatorText,
    locator_to_xpoint_range,
    xpoint_range_to_locator,
    xpoint_to_locator,
)
from .text_anchor import MatchConfidence
from .verify import (
    VerificationResult,
    normalize_for_comparison,
    normalize_whitespace,
    verify_range,
)
from .xpoint import XPoint, XPointRange, normalize_xpath

__all__ = [
    "Cfi",
    "CfiParseError",
    "CfiRange",
    "CharOffset",
    "EpubMap",
    "EpubStructureError",
    "LocalPath",
    "Locator",
    "LocatorLocations",
    "LocatorMatch",
    "LocatorText",
    "MatchConfidence",
    "NodeMap",
    "ResolutionError",
    "Step",
    "TextAssertion",
    "VerificationResult",
    "XPoint",
    "XPointParseError",
    "XPointRange",
    "XpointCfiError",
    "cfi_range_to_xpoint_range",
    "cfi_to_xpoint",
    "cfi_to_xpoint_range_strings",
    "cfi_to_xpoint_string",
    "locator_to_xpoint_range",
    "normalize_for_comparison",
    "normalize_whitespace",
    "normalize_xpath",
    "parse_cfi",
    "verify_range",
    "xpoint_range_to_cfi",
    "xpoint_range_to_cfi_string",
    "xpoint_range_to_locator",
    "xpoint_to_cfi",
    "xpoint_to_cfi_string",
    "xpoint_to_locator",
]
