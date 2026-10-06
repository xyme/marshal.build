"""Deterministic local text extraction from PDF and DOCX bytes
(pdf-docx-ingestion spec R2/R4).

Extraction is verbatim text recovery — no model calls, no OCR, no layout
reconstruction. Callers surface DocExtractionError as a 422 (the
ImportValidationError free-text style). Extractors are sync and CPU-bound;
API seams hop them off the event loop with asyncio.to_thread.

Hostile-input posture (R4):
- PDF: encrypted flag checked BEFORE parsing pages; page-count cap; every
  pypdf exception maps to one named refusal (a parser bug must never 500
  the import route); early-stop past the caller's char cap.
- DOCX: ONLY word/document.xml is ever decompressed (media and all other
  members are never read) and that one member carries an uncompressed-size
  cap; XML parsed with stdlib expat (Python 3.12: built-in entity-
  amplification protection, no external-entity resolution).
"""

import io
import zipfile

PDF_MAGIC = b"%PDF-"
ZIP_MAGIC = b"PK\x03\x04"

_PDF_MAX_PAGES = 500
_DOCX_XML_MAX_BYTES = 16 * 1024 * 1024  # word/document.xml uncompressed
_DOCX_DOCUMENT_XML = "word/document.xml"
_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# Named refusal strings (design §6) — tests pin these.
ERR_ENCRYPTED = "Document is password-protected — decrypt it and retry"
ERR_NO_TEXT = (
    "Document contains no extractable text — scanned documents need OCR, "
    "which marshal does not run"
)
ERR_CORRUPT = "Document could not be parsed — the file may be corrupt"
ERR_TOO_MANY_PAGES = f"Document has too many pages (max {_PDF_MAX_PAGES})"


class DocExtractionError(Exception):
    """Named refusal — callers surface as 422 (ImportValidationError style)."""


def is_docx(zf: zipfile.ZipFile) -> bool:
    """A .docx is a zip carrying word/document.xml; a marshal spec-set zip
    never does — the branches are disjoint in practice."""
    return _DOCX_DOCUMENT_XML in zf.namelist()


def _oversize(max_chars: int) -> DocExtractionError:
    # Mirrors the existing "{doc_type}.md exceeds N characters" convention.
    return DocExtractionError(f"Extracted text exceeds {max_chars} characters")


def extract_pdf_text(raw: bytes, *, max_chars: int) -> str:
    """Page text in document order, joined by blank lines. Early-stops the
    accumulation once past max_chars (bounded CPU on hostile inputs)."""
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocExtractionError(ERR_ENCRYPTED)
        if len(reader.pages) > _PDF_MAX_PAGES:
            raise DocExtractionError(ERR_TOO_MANY_PAGES)
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            text = page.extract_text() or ""
            parts.append(text)
            total += len(text) + 2
            if total > max_chars + 2:
                raise _oversize(max_chars)
    except DocExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 — pypdf raises a zoo; one honest refusal
        raise DocExtractionError(ERR_CORRUPT) from exc
    text = "\n\n".join(parts).strip()
    if not text:
        raise DocExtractionError(ERR_NO_TEXT)
    if len(text) > max_chars:
        raise _oversize(max_chars)
    return text


def extract_docx_text(raw: bytes, *, max_chars: int) -> str:
    """Body text from word/document.xml: per-paragraph lines (w:t runs,
    w:tab → tab, w:br/w:cr → newline). Table-cell paragraphs are w:p nodes
    too, so tables fall out as lines without special casing. Headers,
    footers, footnotes and comments are recorded non-goals (v1)."""
    import xml.etree.ElementTree as ET

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        info = zf.getinfo(_DOCX_DOCUMENT_XML)
        if info.file_size > _DOCX_XML_MAX_BYTES:
            raise DocExtractionError(ERR_CORRUPT)
        root = ET.fromstring(zf.read(info))
    except DocExtractionError:
        raise
    except Exception as exc:  # noqa: BLE001 — BadZipFile, KeyError, ParseError, ...
        raise DocExtractionError(ERR_CORRUPT) from exc

    paragraphs: list[str] = []
    total = 0
    for para in root.iter(f"{_W_NS}p"):
        runs: list[str] = []
        for node in para.iter():
            if node.tag == f"{_W_NS}t":
                runs.append(node.text or "")
            elif node.tag == f"{_W_NS}tab":
                runs.append("\t")
            elif node.tag in (f"{_W_NS}br", f"{_W_NS}cr"):
                runs.append("\n")
        line = "".join(runs)
        paragraphs.append(line)
        total += len(line) + 1
        if total > max_chars + 1:
            raise _oversize(max_chars)
    text = "\n".join(paragraphs).strip()
    if not text:
        raise DocExtractionError(ERR_NO_TEXT)
    if len(text) > max_chars:
        raise _oversize(max_chars)
    return text
