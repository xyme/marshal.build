"""External project import (spec: .kiro/specs/external-import-connectors, I1/I2).

Creates an independent project + version-1 specs from user-supplied markdown
(pasted documents or a base64 zip in the marshal export layout), mirroring
the marketplace-fork creation shape so imported content is governance-
identical to authored content: the same risk-scoring trigger fires, the same
deploy gates apply, the same audit trail records it. The gates are
provenance-blind by design — import changes where content comes FROM, never
what governs it.
"""

import base64
import binascii
import io
import logging
import zipfile

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Project, Spec, User
from app.services.marketplace import DOC_TYPES

logger = logging.getLogger("marshal.project_import")

# Mirrors SpecSaveIn.content max_length (schemas/specs.py) — one ceiling for
# authored and imported documents alike.
MAX_DOC_CHARS = 400_000

# Zip-bomb guards (I2): a spec set is three text files, so these bounds are
# generous for legitimate archives and hostile to everything else.
_MAX_ENTRIES = 64
_MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024


class ImportValidationError(Exception):
    """Invalid import payload — surfaces as 422 with this message."""


def _doc_from_name(name: str) -> tuple[str, str] | None:
    """Map an archive entry to (slug_group, doc_type) or None.

    Accepts the export layout `.kiro/specs/<slug>/<doc>.md` and, as a
    convenience, the three documents at the archive root. Everything else
    (manifest.json, assets, nested folders) is ignored.
    """
    clean = name.replace("\\", "/").lstrip("/")
    parts = [p for p in clean.split("/") if p]
    if len(parts) == 1 and parts[0].endswith(".md"):
        doc = parts[0][:-3]
        return ("", doc) if doc in DOC_TYPES else None
    if (
        len(parts) == 4
        and parts[0] == ".kiro"
        and parts[1] == "specs"
        and parts[3].endswith(".md")
    ):
        doc = parts[3][:-3]
        return (parts[2], doc) if doc in DOC_TYPES else None
    return None


def parse_archive(archive_b64: str) -> dict[str, str]:
    """Extract {doc_type: content} from a base64 zip (I2, AC-1 round-trip)."""
    try:
        raw = base64.b64decode(archive_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImportValidationError("archive_b64 is not valid base64") from exc
    return parse_archive_bytes(raw)


def parse_archive_bytes(raw: bytes) -> dict[str, str]:
    """Zip-bytes core shared by the b64 body path (I2) and URL fetch (I3)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile as exc:
        raise ImportValidationError("Archive is not a valid zip file") from exc

    infos = [i for i in archive.infolist() if not i.is_dir()]
    if len(infos) > _MAX_ENTRIES:
        raise ImportValidationError(
            f"Archive has too many entries (max {_MAX_ENTRIES})"
        )
    if sum(i.file_size for i in infos) > _MAX_TOTAL_UNCOMPRESSED:
        raise ImportValidationError("Archive uncompressed size exceeds the limit")

    groups: dict[str, dict[str, str]] = {}
    for info in infos:
        located = _doc_from_name(info.filename)
        if located is None:
            continue
        slug, doc_type = located
        if info.file_size > MAX_DOC_CHARS * 4:  # utf-8 upper bound
            raise ImportValidationError(f"{doc_type}.md exceeds the size limit")
        try:
            content = archive.read(info).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ImportValidationError(
                f"{doc_type}.md is not valid UTF-8 text"
            ) from exc
        groups.setdefault(slug, {})[doc_type] = content

    if not groups:
        raise ImportValidationError(
            "No specification documents found — expected requirements/design/"
            "tasks .md files at the root or under .kiro/specs/<slug>/"
        )
    # Deterministic choice when several spec sets are present: first slug in
    # sorted order; root-level docs sort first ("" < any slug).
    chosen = groups[sorted(groups)[0]]
    return chosen


async def import_spec_set(
    db: AsyncSession,
    user: User,
    *,
    name: str,
    docs: dict[str, str],
    source: str | None = None,
) -> tuple[Project, list[str]]:
    """Create the project + v1 specs (fork-shaped) and trigger risk scoring."""
    cleaned: dict[str, str] = {}
    for doc_type in DOC_TYPES:
        content = docs.get(doc_type) or ""
        if not content.strip():
            continue
        if len(content) > MAX_DOC_CHARS:
            raise ImportValidationError(
                f"{doc_type}.md exceeds {MAX_DOC_CHARS} characters"
            )
        cleaned[doc_type] = content
    if "requirements" not in cleaned:
        raise ImportValidationError("requirements.md is required and cannot be empty")

    project = Project(
        user_id=user.id,
        name=name,
        description=None,
        status="draft",
        origin="imported",
    )
    db.add(project)
    await db.flush()
    # Composable agents R1.2: imported requirements sync the graph too —
    # provenance-blind, the same named refusals as an authored save.
    from app.services.composition import sync_project_composition

    await sync_project_composition(db, project, cleaned["requirements"])
    # Agent substance R3.2: the capability rail is provenance-blind too.
    from app.services.capabilities import enforce_template_rail

    await enforce_template_rail(db, project, cleaned["requirements"])
    for doc_type, content in cleaned.items():
        db.add(
            Spec(
                project_id=project.id,
                version=1,
                type=doc_type,
                content=content,
                origin="imported",
                created_by=user.id,
            )
        )
    project.status = "spec_complete"
    await db.commit()
    await db.refresh(project)

    # Risk scoring on import — the S4-02 contract (save/generation/fork/import
    # all score) is what keeps provenance out of the gate.
    from app.services.risk import trigger_assessment

    trigger_assessment(project.id, user.id)
    logger.info(
        "project imported: %s docs=%s source=%s", project.id, sorted(cleaned), source
    )
    return project, sorted(cleaned)


# ------------------------------------------------- I3 v1: public-URL fetch
# Owner resolution (7 Sep 2026): https-only public sources, no credentials,
# SSRF-filtered, size-capped. PDF/DOCX ingestion is a separate OPEN decision.

_FETCH_TIMEOUT_S = 8
_MAX_FETCH_BYTES = 1_500_000
_MAX_REDIRECTS = 3
_ZIP_MAGIC = b"PK\x03\x04"


def _assert_public_host(host: str) -> None:
    """Every resolved address must be globally routable — private, loopback,
    link-local, reserved and multicast ranges are refused (SSRF posture)."""
    import ipaddress
    import socket

    try:
        infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise ImportValidationError(f"Could not resolve host '{host}'") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise ImportValidationError(
                "URL resolves to a non-public address — only public https "
                "sources can be imported"
            )


async def fetch_public_url(url: str) -> tuple[bytes, str]:
    """Fetch (content_bytes, content_type) under the I3 v1 SSRF posture:
    https only, default port, no URL credentials, every redirect hop
    re-validated against the same rules, bounded size/time/hops. The client
    re-resolves DNS after the public-address check — the narrow rebinding
    window is an accepted v1 risk, recorded as a hardening item alongside
    network egress control (C1-b v2).
    """
    import asyncio
    import urllib.parse

    import httpx

    for _hop in range(_MAX_REDIRECTS + 1):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https":
            raise ImportValidationError("Only https:// URLs can be imported")
        if parsed.port not in (None, 443):
            raise ImportValidationError("Only the default https port (443) is allowed")
        if parsed.username or parsed.password:
            raise ImportValidationError("URLs carrying credentials are not allowed")
        if not parsed.hostname:
            raise ImportValidationError("URL has no host")
        await asyncio.to_thread(_assert_public_host, parsed.hostname)
        async with httpx.AsyncClient(
            timeout=_FETCH_TIMEOUT_S, follow_redirects=False
        ) as client:
            async with client.stream(
                "GET", url, headers={"User-Agent": "marshal-import/1", "Accept": "*/*"}
            ) as resp:
                if resp.status_code in (301, 302, 303, 307, 308):
                    location = resp.headers.get("location")
                    if not location:
                        raise ImportValidationError("Source redirected without a location")
                    url = urllib.parse.urljoin(url, location)
                    continue
                if resp.status_code != 200:
                    raise ImportValidationError(
                        f"Source returned HTTP {resp.status_code}"
                    )
                total = 0
                chunks: list[bytes] = []
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > _MAX_FETCH_BYTES:
                        raise ImportValidationError(
                            "Source exceeds the 1.5MB import limit"
                        )
                    chunks.append(chunk)
                return b"".join(chunks), resp.headers.get("content-type", "")
    raise ImportValidationError("Too many redirects")


def docs_from_bytes(content: bytes) -> dict[str, str]:
    """One detection truth for every byte source (upload, URL) — magic bytes
    decide, extension and Content-Type never do (pdf-docx-ingestion R3):
    PDF → extracted text as requirements; zip → marshal spec set, else DOCX
    (a .docx IS a zip — disambiguated by word/document.xml); UTF-8 → raw
    markdown as requirements; anything else → named refusal."""
    from app.services import doc_extract

    if content[:5] == doc_extract.PDF_MAGIC:
        return {
            "requirements": _extract_or_422(
                doc_extract.extract_pdf_text, content, max_chars=MAX_DOC_CHARS
            )
        }
    if content[:4] == _ZIP_MAGIC:
        try:
            zf = zipfile.ZipFile(io.BytesIO(content))
        except zipfile.BadZipFile as exc:
            raise ImportValidationError("Archive is not a valid zip file") from exc
        has_spec_docs = any(
            _doc_from_name(i.filename) for i in zf.infolist() if not i.is_dir()
        )
        if has_spec_docs:
            return parse_archive_bytes(content)
        if doc_extract.is_docx(zf):
            return {
                "requirements": _extract_or_422(
                    doc_extract.extract_docx_text, content, max_chars=MAX_DOC_CHARS
                )
            }
        raise ImportValidationError(
            "No specification documents found — expected requirements/design/"
            "tasks .md files at the root or under .kiro/specs/<slug>/ — or a "
            "Word .docx document"
        )
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ImportValidationError(
            "Source is not a supported format — expected a spec-set zip, a "
            "PDF, a Word .docx document, or UTF-8 text"
        ) from exc
    if not text.strip():
        raise ImportValidationError("Source document is empty")
    return {"requirements": text}


def _extract_or_422(extractor, content: bytes, *, max_chars: int) -> str:
    """Wrap DocExtractionError into ImportValidationError at this seam so the
    route's existing except-clause needs no change."""
    from app.services.doc_extract import DocExtractionError

    try:
        return extractor(content, max_chars=max_chars)
    except DocExtractionError as exc:
        raise ImportValidationError(str(exc)) from exc


def parse_document(document_b64: str) -> dict[str, str]:
    """The document_b64 upload path: base64 → the shared byte dispatcher."""
    try:
        raw = base64.b64decode(document_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ImportValidationError("document_b64 is not valid base64") from exc
    return docs_from_bytes(raw)


def docs_from_fetched(content: bytes, content_type: str) -> dict[str, str]:
    """I3 delegate — the Content-Type header remains IGNORED by design;
    magic bytes are the truth (same posture the zip branch always had)."""
    del content_type
    return docs_from_bytes(content)
