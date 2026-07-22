"""Exception hierarchy for xpoint-cfi."""


class XpointCfiError(Exception):
    """Base class for all xpoint-cfi errors."""


class XPointParseError(XpointCfiError):
    """A KOReader xpointer string could not be parsed."""

    def __init__(self, xpoint: str, reason: str) -> None:
        self.xpoint = xpoint
        self.reason = reason
        super().__init__(f"Invalid xpointer {xpoint!r}: {reason}")


class CfiParseError(XpointCfiError):
    """An EPUB CFI string could not be parsed."""

    def __init__(self, cfi: str, reason: str) -> None:
        self.cfi = cfi
        self.reason = reason
        super().__init__(f"Invalid CFI {cfi!r}: {reason}")


class ResolutionError(XpointCfiError):
    """A parsed location could not be resolved against the EPUB document."""

    def __init__(self, location: str, reason: str) -> None:
        self.location = location
        self.reason = reason
        super().__init__(f"Cannot resolve {location!r}: {reason}")


class EpubStructureError(XpointCfiError):
    """The EPUB container, OPF, or spine is malformed."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"Malformed EPUB: {reason}")
