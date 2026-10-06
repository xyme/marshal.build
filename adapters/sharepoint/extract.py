"""Text extraction for get_document_text (sharepoint-connector R1.3).

Text-native formats pass through (html stripped); .docx extracts via the
same stdlib zipfile + expat walk the platform uses (self-contained copy —
the adapter deploys independently). Everything else gets an HONEST
unsupported-format refusal, never mangled bytes (AC-4).
"""

import io
import re
import zipfile

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_XML_MAX_BYTES = 16 * 1024 * 1024

TEXT_EXTENSIONS = (".txt", ".md", ".csv", ".json", ".html", ".htm", ".docx")


class UnsupportedFormat(Exception):
    pass


def extract_text(name: str, raw: bytes, *, max_chars: int) -> str:
    lower = name.lower()
    if lower.endswith(".docx"):
        text = _docx_text(raw, max_chars=max_chars)
    elif lower.endswith((".html", ".htm")):
        text = _strip_html(raw.decode("utf-8", errors="replace"))
    elif lower.endswith((".txt", ".md", ".csv", ".json")):
        text = raw.decode("utf-8", errors="replace")
    else:
        raise UnsupportedFormat(
            f"'{name}' is not a text-native format — the adapter serves "
            f"{', '.join(TEXT_EXTENSIONS)} (no OCR, no binary conversion)"
        )
    text = text.strip()
    if not text:
        raise UnsupportedFormat(f"'{name}' contains no extractable text")
    if len(text) > max_chars:
        return text[:max_chars] + "\n[truncated by the adapter's character cap]"
    return text


def _strip_html(html: str) -> str:
    html = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    return re.sub(r"[ \t]+", " ", html)


def _docx_text(raw: bytes, *, max_chars: int) -> str:
    import xml.etree.ElementTree as ET

    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        info = zf.getinfo("word/document.xml")
        if info.file_size > _DOCX_XML_MAX_BYTES:
            raise UnsupportedFormat("Word document XML exceeds the adapter cap")
        root = ET.fromstring(zf.read(info))
    except UnsupportedFormat:
        raise
    except Exception as exc:  # noqa: BLE001
        raise UnsupportedFormat("Word document could not be parsed") from exc
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
        if total > max_chars * 2:
            break  # bounded work; the caller clips to max_chars
    return "\n".join(paragraphs)
