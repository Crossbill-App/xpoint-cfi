"""Shared pytest fixtures: an in-memory EPUB builder and a representative sample book.

No binary fixtures live in the repo; every document test assembles a synthetic EPUB
with :func:`build_epub` from plain XHTML strings.
"""

from __future__ import annotations

import io
import urllib.parse
import zipfile

import pytest

_CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" '
    'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)

_METADATA = (
    '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
    '<dc:identifier id="uid">urn:uuid:xpoint-cfi-test</dc:identifier>'
    "<dc:title>Test Book</dc:title>"
    "<dc:language>en</dc:language>"
    "</metadata>"
)


def _build_opf(hrefs: list[str], *, spine_step_padding: bool) -> str:
    """Assemble an OPF whose manifest and spine list ``hrefs`` in order.

    Manifest hrefs are URL-encoded (so a space becomes ``%20``); zip entries are stored
    under the decoded path. When ``spine_step_padding`` is set an extra ``<guide/>`` is
    inserted before ``<spine>`` so the spine element's position (and hence the computed
    CFI step) differs from the unpadded default.
    """
    items: list[str] = []
    itemrefs: list[str] = []
    for i, href in enumerate(hrefs, start=1):
        quoted = urllib.parse.quote(href)
        items.append(f'<item id="item{i}" href="{quoted}" media-type="application/xhtml+xml"/>')
        itemrefs.append(f'<itemref idref="item{i}"/>')
    manifest = "<manifest>" + "".join(items) + "</manifest>"
    spine = "<spine>" + "".join(itemrefs) + "</spine>"
    padding = "<guide/>" if spine_step_padding else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
        'unique-identifier="uid">'
        f"{_METADATA}{manifest}{padding}{spine}"
        "</package>"
    )


def build_epub(docs: dict[str, str], *, spine_step_padding: bool = False) -> bytes:
    """Assemble a minimal valid EPUB from ``docs`` (a mapping of href to XHTML string).

    Manifest item ids and spine idrefs are ``item1..itemN`` in dict order. The returned
    bytes are a complete EPUB zip: a stored ``mimetype`` entry first, ``container.xml``,
    the OPF, and one document per entry under ``OEBPS/``.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            zipfile.ZipInfo("mimetype"),
            "application/epub+zip",
            compress_type=zipfile.ZIP_STORED,
        )
        archive.writestr("META-INF/container.xml", _CONTAINER_XML)
        archive.writestr(
            "OEBPS/content.opf",
            _build_opf(list(docs), spine_step_padding=spine_step_padding),
        )
        for href, content in docs.items():
            archive.writestr(f"OEBPS/{href}", content)
    return buffer.getvalue()


def xhtml_doc(title: str, body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<html xmlns="http://www.w3.org/1999/xhtml">'
        f"<head><title>{title}</title></head>"
        f"<body>{body}</body>"
        "</html>"
    )


# The first chapter packs every structural feature the chunk model must handle:
# nested divs, an id, a comment splitting a paragraph's text, a non-BMP emoji, an empty
# paragraph, mixed inline elements, and repeated same-tag siblings for [N] addressing.
_CHAP1_BODY = (
    '<div id="intro">'
    "<p>Hello <i>brave</i> new <b>world</b>.</p>"
    "<p>Before<!--c-->after</p>"
    "<p>Emoji \U0001f600 tail</p>"
    "<p></p>"
    "<div><p>alpha</p><p>beta</p></div>"
    "</div>"
)

_CHAP2_BODY = '<h1 id="title">Second</h1><p>A <a href="#x">link</a> here.</p>'

_CHAP3_BODY = "<p>Third chapter.</p>"


@pytest.fixture
def simple_book() -> bytes:
    """A three-spine-item EPUB with representative structure for the document tests."""
    return build_epub(
        {
            "chap1.xhtml": xhtml_doc("Chapter 1", _CHAP1_BODY),
            "chap2.xhtml": xhtml_doc("Chapter 2", _CHAP2_BODY),
            "chap3.xhtml": xhtml_doc("Chapter 3", _CHAP3_BODY),
        }
    )
