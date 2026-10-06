"""Marketplace tests (marketplace spec R1–R5)."""

import uuid

import pytest
from sqlalchemy import select

from app.models import MarketplaceSample, Project, Spec, Template
from app.services import marketplace as svc

pytestmark = pytest.mark.asyncio

SNAPSHOT = {
    "requirements_md": "# Reqs\n\n- US-1: As a user, I want things.",
    "design_md": "# Design\n\n```mermaid\ngraph TD\n  A --> B\n```",
    "tasks_md": "# Tasks\n\n- [ ] 1. Build it",
}


async def _mk_sample(db, author=None, status="published", **overrides) -> MarketplaceSample:
    fields = dict(
        title=f"Sample {uuid.uuid4().hex[:6]}",
        description="A useful sample application for testing the catalog.",
        long_description="Long text",
        category="chatbot",
        complexity="beginner",
        models_used=["us.anthropic.claude-sonnet-5"],
        spec_snapshot=dict(SNAPSHOT),
        keywords=["rag", "test"],
        author_id=author.id if author else None,
        status=status,
    )
    fields.update(overrides)
    sample = MarketplaceSample(**fields)
    db.add(sample)
    await db.commit()
    await db.refresh(sample)
    return sample


# ------------------------------------------------------------------ visibility


async def test_draft_invisible_until_published(client, db_session, test_user, audit_db):
    sample = await _mk_sample(db_session, status="draft")
    listing = (await client.get("/api/v1/marketplace/samples")).json()
    assert listing["total"] == 0
    detail = await client.get(f"/api/v1/marketplace/samples/{sample.id}")
    assert detail.status_code == 404


async def test_published_visible_and_view_counted(client, db_session, audit_db):
    sample = await _mk_sample(db_session, status="published")
    detail = (await client.get(f"/api/v1/marketplace/samples/{sample.id}")).json()
    assert detail["title"] == sample.title
    await client.get(f"/api/v1/marketplace/samples/{sample.id}")
    await db_session.refresh(sample)
    assert sample.view_count == 2


async def test_admin_sees_all_statuses(admin_user, client_for, db_session, audit_db):
    await _mk_sample(db_session, status="draft")
    await _mk_sample(db_session, status="published")
    await _mk_sample(db_session, status="archived")
    async with client_for(admin_user) as ac:
        body = (await ac.get("/api/v1/admin/marketplace/samples")).json()
        assert body["total"] == 3
        drafts = (await ac.get("/api/v1/admin/marketplace/samples", params={"status": "draft"})).json()
        assert drafts["total"] == 1


# --------------------------------------------------------------- search/filter


async def test_search_and_filters(client, db_session, audit_db):
    await _mk_sample(db_session, title="Policy Q&A Bot", keywords=["rag", "hr"], category="chatbot")
    await _mk_sample(
        db_session, title="Invoice Parser", keywords=["finance"],
        category="document_processing", complexity="intermediate",
    )
    q = (await client.get("/api/v1/marketplace/samples", params={"q": "policy"})).json()
    assert q["total"] == 1 and q["items"][0]["title"] == "Policy Q&A Bot"
    kw = (await client.get("/api/v1/marketplace/samples", params={"q": "finance"})).json()
    assert kw["total"] == 1 and kw["items"][0]["title"] == "Invoice Parser"
    cat = (await client.get("/api/v1/marketplace/samples", params={"category": "chatbot"})).json()
    assert cat["total"] == 1
    cx = (await client.get("/api/v1/marketplace/samples", params={"complexity": "intermediate"})).json()
    assert cx["total"] == 1
    none = (await client.get("/api/v1/marketplace/samples", params={"q": "zzz-nope"})).json()
    assert none["total"] == 0 and none["items"] == []


async def test_sort_popular_and_pagination(client, db_session, audit_db):
    for i in range(3):
        await _mk_sample(db_session, title=f"S{i} sample name", fork_count=i)
    body = (await client.get("/api/v1/marketplace/samples", params={"sort": "popular", "page_size": 2})).json()
    assert [i["fork_count"] for i in body["items"]] == [2, 1]
    assert body["total"] == 3
    page2 = (await client.get("/api/v1/marketplace/samples", params={"sort": "popular", "page_size": 2, "page": 2})).json()
    assert len(page2["items"]) == 1


async def test_category_counts(client, db_session, audit_db):
    await _mk_sample(db_session, category="chatbot")
    await _mk_sample(db_session, category="chatbot")
    await _mk_sample(db_session, category="data_analysis")
    counts = {c["category"]: c["count"] for c in (await client.get("/api/v1/marketplace/categories")).json()}
    assert counts == {"chatbot": 2, "data_analysis": 1}


# ------------------------------------------------------------------------ fork


async def test_fork_creates_independent_project(client, db_session, test_user, audit_db):
    sample = await _mk_sample(db_session)
    resp = await client.post(
        f"/api/v1/marketplace/samples/{sample.id}/fork", json={"name": "My Fork"}
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["warnings"] == []

    project = await db_session.get(Project, uuid.UUID(body["project_id"]))
    assert project.origin == "marketplace_fork"
    assert project.forked_from_sample_id == sample.id
    assert project.status == "spec_complete"

    specs = (
        await db_session.execute(select(Spec).where(Spec.project_id == project.id))
    ).scalars().all()
    assert {s.type for s in specs} == {"requirements", "design", "tasks"}
    assert all(s.version == 1 and s.origin == "forked" for s in specs)

    await db_session.refresh(sample)
    assert sample.fork_count == 1

    # Independence (§4.3.8 AC-3): mutate the sample after forking
    sample.spec_snapshot = {**sample.spec_snapshot, "requirements_md": "# CHANGED"}
    await db_session.commit()
    req = next(s for s in specs if s.type == "requirements")
    await db_session.refresh(req)
    assert req.content == SNAPSHOT["requirements_md"]


async def test_fork_warns_on_deprecated_template(client, db_session, admin_user, audit_db):
    template = Template(name="Old T", category="chatbot", status="deprecated", guardrails={}, scaffolding={})
    db_session.add(template)
    await db_session.commit()
    sample = await _mk_sample(db_session, template_id=template.id)
    body = (
        await client.post(f"/api/v1/marketplace/samples/{sample.id}/fork", json={"name": "Fork W"})
    ).json()
    assert any("deprecated" in w for w in body["warnings"])
    project = await db_session.get(Project, uuid.UUID(body["project_id"]))
    assert project.template_id == template.id


async def test_fork_warns_on_unregistered_models(client, db_session, audit_db):
    sample = await _mk_sample(db_session, models_used=["legacy.claude-1"])
    body = (
        await client.post(f"/api/v1/marketplace/samples/{sample.id}/fork", json={"name": "Fork M"})
    ).json()
    assert any("legacy.claude-1" in w for w in body["warnings"])


async def test_fork_rolls_back_atomically(db_session, test_user, monkeypatch, audit_db):
    """Failure mid-fork leaves no project, no specs, no count bump (R4.6)."""
    sample = await _mk_sample(db_session)

    class ExplodingSpec:
        def __init__(self, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(svc, "Spec", ExplodingSpec)
    with pytest.raises(RuntimeError):
        await svc.fork_sample(db_session, sample, test_user, "Doomed Fork")
    await db_session.rollback()

    projects = (await db_session.execute(select(Project))).scalars().all()
    specs = (await db_session.execute(select(Spec))).scalars().all()
    await db_session.refresh(sample)
    assert projects == [] and specs == [] and sample.fork_count == 0


# -------------------------------------------------------------------- curation


async def test_admin_create_update_publish_flow(admin_user, client_for, db_session, audit_db):
    async with client_for(admin_user) as ac:
        created = await ac.post(
            "/api/v1/admin/marketplace/samples",
            json={
                "title": "Curated Sample",
                "description": "Testing the admin curation path end to end.",
                "category": "chatbot",
                "complexity": "beginner",
                "models_used": ["us.anthropic.claude-sonnet-5"],
            },
        )
        assert created.status_code == 201
        sid = created.json()["id"]
        assert created.json()["status"] == "draft"

        # Publish blocked while snapshot empty (R5.3)
        blocked = await ac.post(f"/api/v1/admin/marketplace/samples/{sid}/publish")
        assert blocked.status_code == 422
        assert "requirements_md" in blocked.json()["detail"]

        updated = await ac.put(
            f"/api/v1/admin/marketplace/samples/{sid}",
            json={
                "title": "Curated Sample",
                "description": "Testing the admin curation path end to end.",
                "category": "chatbot",
                "complexity": "beginner",
                "models_used": ["us.anthropic.claude-sonnet-5"],
                "spec_snapshot": SNAPSHOT,
            },
        )
        assert updated.status_code == 200
        published = await ac.post(f"/api/v1/admin/marketplace/samples/{sid}/publish")
        assert published.status_code == 200
        assert published.json()["status"] == "published"


async def test_admin_import_from_project(admin_user, client_for, db_session, audit_db):
    project = Project(user_id=admin_user.id, name="Source", status="spec_complete")
    db_session.add(project)
    await db_session.flush()
    db_session.add(Spec(project_id=project.id, version=1, type="requirements", content="v1 req"))
    db_session.add(Spec(project_id=project.id, version=2, type="requirements", content="v2 req"))
    db_session.add(Spec(project_id=project.id, version=1, type="design", content="v1 design"))
    await db_session.commit()
    sample = await _mk_sample(db_session, status="draft", spec_snapshot={})
    async with client_for(admin_user) as ac:
        resp = await ac.post(
            f"/api/v1/admin/marketplace/samples/{sample.id}/import-spec",
            json={"project_id": str(project.id)},
        )
        assert resp.status_code == 200
        snap = resp.json()["spec_snapshot"]
        assert snap["requirements_md"] == "v2 req"  # latest version wins
        assert snap["design_md"] == "v1 design"
        assert "tasks_md" not in snap


async def test_archive_hides_but_keeps_forks(admin_user, client_for, client, db_session, audit_db):
    sample = await _mk_sample(db_session)
    fork = (
        await client.post(f"/api/v1/marketplace/samples/{sample.id}/fork", json={"name": "Keeper"})
    ).json()
    async with client_for(admin_user) as ac:
        resp = await ac.post(f"/api/v1/admin/marketplace/samples/{sample.id}/archive")
        assert resp.status_code == 200
    assert (await client.get(f"/api/v1/marketplace/samples/{sample.id}")).status_code == 404
    project = await db_session.get(Project, uuid.UUID(fork["project_id"]))
    assert project is not None and project.status == "spec_complete"


async def test_delete_draft_only(admin_user, client_for, db_session, audit_db):
    published = await _mk_sample(db_session, status="published")
    draft = await _mk_sample(db_session, status="draft")
    async with client_for(admin_user) as ac:
        assert (await ac.delete(f"/api/v1/admin/marketplace/samples/{published.id}")).status_code == 422
        assert (await ac.delete(f"/api/v1/admin/marketplace/samples/{draft.id}")).status_code == 204


async def test_admin_routes_role_gated(client, db_session, audit_db):
    """Default client is a power user — admin curation must 403."""
    resp = await client.post(
        "/api/v1/admin/marketplace/samples",
        json={"title": "Nope", "description": "Not allowed to do this.", "category": "chatbot"},
    )
    assert resp.status_code == 403


# ---------------------------------------------------- external import (I1/I2)
# Spec: .kiro/specs/external-import-connectors — import mirrors the fork
# creation shape, so its tests live beside the fork tests.


def _zip_b64(files: dict[str, str]) -> str:
    import base64
    import io
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return base64.b64encode(buffer.getvalue()).decode()


async def test_import_paste_creates_project_and_scores(
    client, db_session, test_user, monkeypatch, audit_db
):
    from app.services import project_import as import_svc

    calls: list = []
    monkeypatch.setattr(
        import_svc, "trigger_assessment", lambda pid, uid: calls.append((pid, uid)),
        raising=False,
    )
    # trigger_assessment is imported inside the function — patch at source too.
    from app.services import risk as risk_svc

    monkeypatch.setattr(risk_svc, "trigger_assessment", lambda pid, uid: calls.append((pid, uid)))

    resp = await client.post(
        "/api/v1/projects/import",
        json={
            "name": "Imported Agent",
            "source": "external-repo",
            "docs": {"requirements": "# Req\nSHALL work", "design": "# Design"},
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Imported Agent"
    assert body["status"] == "spec_complete"

    from app.models import Project, Spec

    project = await db_session.get(Project, uuid.UUID(body["id"]))
    assert project.origin == "imported"
    specs = (
        (await db_session.execute(select(Spec).where(Spec.project_id == project.id)))
        .scalars().all()
    )
    assert {s.type for s in specs} == {"requirements", "design"}
    assert all(s.version == 1 and s.origin == "imported" for s in specs)
    # AC-2: the same scoring trigger the fork path fires
    assert calls and calls[0][0] == project.id


async def test_import_zip_round_trip(client, db_session, monkeypatch, audit_db):
    from app.services import risk as risk_svc

    monkeypatch.setattr(risk_svc, "trigger_assessment", lambda *a: None)
    docs = {
        ".kiro/specs/my-agent/requirements.md": "# R\ncontent-1",
        ".kiro/specs/my-agent/design.md": "# D\ncontent-2",
        ".kiro/specs/my-agent/tasks.md": "# T\ncontent-3",
        ".kiro/specs/my-agent/manifest.json": "{\"v\":1}",
    }
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "Zipped", "archive_b64": _zip_b64(docs)},
    )
    assert resp.status_code == 201, resp.text

    from app.models import Spec

    specs = (
        (await db_session.execute(
            select(Spec).where(Spec.project_id == uuid.UUID(resp.json()["id"]))
        )).scalars().all()
    )
    by_type = {s.type: s.content for s in specs}
    # AC-1: byte-identical round trip of every provided document
    assert by_type["requirements"] == "# R\ncontent-1"
    assert by_type["design"] == "# D\ncontent-2"
    assert by_type["tasks"] == "# T\ncontent-3"


async def test_import_validation_failures(client, audit_db):
    # neither shape
    assert (
        await client.post("/api/v1/projects/import", json={"name": "X"})
    ).status_code == 422
    # both shapes
    assert (
        await client.post(
            "/api/v1/projects/import",
            json={"name": "X", "docs": {"requirements": "r"}, "archive_b64": "aaaa"},
        )
    ).status_code == 422
    # requirements missing
    resp = await client.post(
        "/api/v1/projects/import", json={"name": "X", "docs": {"design": "only"}}
    )
    assert resp.status_code == 422
    assert "requirements" in resp.json()["detail"]
    # not a zip
    import base64

    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "archive_b64": base64.b64encode(b"not a zip").decode()},
    )
    assert resp.status_code == 422
    # oversize document
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "docs": {"requirements": "r" * 400_001}},
    )
    assert resp.status_code == 422


async def test_import_requires_editor_persona(business_user, client_for, audit_db):
    # AC-3: business personas get the canonical editor-persona 403
    async with client_for(business_user) as ac:
        resp = await ac.post(
            "/api/v1/projects/import",
            json={"name": "Nope", "docs": {"requirements": "r"}},
        )
        assert resp.status_code == 403


# ------------------------------------------------- I3 v1: public-URL import


def _fake_resolver(monkeypatch, ip="93.184.216.34"):
    import socket

    def fake_getaddrinfo(host, port, proto=0, **kw):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)


def _fake_fetch_http(monkeypatch, *, body=b"# Req\nSHALL work", status=200, headers=None):
    from app.services import project_import as import_svc

    class FakeStream:
        def __init__(self):
            self.status_code = status
            self.headers = headers or {"content-type": "text/markdown"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def aiter_bytes(self):
            for i in range(0, len(body), 65536):
                yield body[i : i + 65536]

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, headers=None):
            return FakeStream()

    import httpx

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    return import_svc


async def test_fetch_public_url_ssrf_matrix(monkeypatch):
    from app.services import project_import as import_svc

    # Scheme / port / credentials refusals need no network at all
    for url, msg in [
        ("http://example.com/spec.md", "Only https"),
        ("https://example.com:8443/spec.md", "default https port"),
        ("https://user:pw@example.com/spec.md", "credentials"),
    ]:
        with pytest.raises(import_svc.ImportValidationError, match=msg):
            await import_svc.fetch_public_url(url)

    # Private/loopback resolution refused
    for ip in ("127.0.0.1", "10.0.0.8", "169.254.169.254", "192.168.1.5"):
        _fake_resolver(monkeypatch, ip=ip)
        with pytest.raises(import_svc.ImportValidationError, match="non-public"):
            await import_svc.fetch_public_url("https://internal.example.com/x.md")


async def test_fetch_public_url_size_and_redirect_bounds(monkeypatch):
    from app.services import project_import as import_svc

    _fake_resolver(monkeypatch)
    _fake_fetch_http(monkeypatch, body=b"x" * (import_svc._MAX_FETCH_BYTES + 1))
    with pytest.raises(import_svc.ImportValidationError, match="1.5MB"):
        await import_svc.fetch_public_url("https://example.com/huge.md")

    # Redirect to a non-https target is refused at the hop re-validation
    _fake_fetch_http(
        monkeypatch,
        status=302,
        headers={"location": "http://internal/x.md"},
    )
    with pytest.raises(import_svc.ImportValidationError, match="Only https"):
        await import_svc.fetch_public_url("https://example.com/redirect")


def test_docs_from_fetched_branches():
    import base64
    import io
    import zipfile

    from app.services import project_import as import_svc

    # Markdown → requirements doc
    docs = import_svc.docs_from_fetched(b"# Req\nSHALL work", "text/markdown")
    assert docs == {"requirements": "# Req\nSHALL work"}

    # Zip magic → archive path (root-level layout)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("requirements.md", "# R")
        z.writestr("design.md", "# D")
    docs = import_svc.docs_from_fetched(buf.getvalue(), "application/zip")
    assert set(docs) == {"requirements", "design"}
    # sanity: same bytes through the b64 path give the same docs (shared core)
    assert import_svc.parse_archive(base64.b64encode(buf.getvalue()).decode()) == docs

    # Binary garbage → honest refusal listing the supported formats
    # (pdf-docx-ingestion: the old "not available" boundary string retired)
    with pytest.raises(import_svc.ImportValidationError, match="not a supported format"):
        import_svc.docs_from_fetched(b"\x89PNG\r\n\x1a\n....", "image/png")


async def test_import_from_url_api(test_user, client_for, db_session, audit_db, monkeypatch):
    from app.services import project_import as import_svc

    async def fake_fetch(url):
        assert url == "https://raw.example.com/agent/requirements.md"
        return b"# Req\nThe agent SHALL answer questions.", "text/markdown"

    monkeypatch.setattr(import_svc, "fetch_public_url", fake_fetch)
    async with client_for(test_user) as ac:
        # XOR rule: url + docs together refused
        r = await ac.post(
            "/api/v1/projects/import",
            json={
                "name": "X",
                "url": "https://raw.example.com/agent/requirements.md",
                "docs": {"requirements": "# r"},
            },
        )
        assert r.status_code == 422

        r = await ac.post(
            "/api/v1/projects/import",
            json={
                "name": "URL Import",
                "url": "https://raw.example.com/agent/requirements.md",
            },
        )
        assert r.status_code in (200, 201)
        body = r.json()
        assert body["name"] == "URL Import"
        assert body["status"] == "spec_complete"


# ------------------------------------ PDF/DOCX ingestion (pdf-docx-ingestion)
# One magic-byte dispatcher serves upload + URL; extension and Content-Type
# never decide. Fixtures are built in-test — no binary files committed.


def _tiny_pdf(text: str = "hello marshal") -> bytes:
    """Hand-assembled single-page PDF with one text object — reliably
    extractable by pypdf; ~500 bytes."""
    stream = f"BT /F1 12 Tf 50 700 Td ({text}) Tj ET".encode()
    objs = [
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj",
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj",
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R"
        b"/Resources<</Font<</F1 5 0 R>>>>>>endobj",
        b"4 0 obj<</Length " + str(len(stream)).encode() + b">>stream\n" + stream
        + b"\nendstream endobj",
        b"5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for obj in objs:
        offsets.append(len(out))
        out += obj + b"\n"
    xref = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer<</Size 6/Root 1 0 R>>\nstartxref\n" + str(xref).encode() + b"\n%%EOF"
    return bytes(out)


def _tiny_docx(paragraphs: tuple[str, ...] = ("Hello marshal", "Second para")) -> bytes:
    import io
    import zipfile

    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/'
            'package/2006/content-types"/>',
        )
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
            'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            f"{body}</w:body></w:document>",
        )
    return buf.getvalue()


def _b64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode()


def test_docs_from_bytes_dispatch_matrix():
    """Magic bytes decide: PDF, DOCX (a zip WITHOUT spec docs), marshal
    spec-set zip (UNCHANGED round-trip pin), text, garbage."""
    import io
    import zipfile

    from app.services import project_import as import_svc

    assert import_svc.docs_from_bytes(_tiny_pdf()) == {"requirements": "hello marshal"}
    assert import_svc.docs_from_bytes(_tiny_docx()) == {
        "requirements": "Hello marshal\nSecond para"
    }
    # Spec-set detection runs BEFORE the docx probe: a zip carrying BOTH
    # spec docs and word/document.xml keeps today's archive semantics.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("requirements.md", "# R")
        z.writestr("word/document.xml", "<w:document/>")
    assert import_svc.docs_from_bytes(buf.getvalue()) == {"requirements": "# R"}
    # A zip with neither names the .docx possibility now
    buf2 = io.BytesIO()
    with zipfile.ZipFile(buf2, "w") as z:
        z.writestr("readme.txt", "hi")
    with pytest.raises(import_svc.ImportValidationError, match="Word .docx"):
        import_svc.docs_from_bytes(buf2.getvalue())


def test_doc_extract_refusals_are_named():
    """Encrypted, no-text (OCR boundary), oversize early-stop, corrupt."""
    import io

    from pypdf import PdfWriter

    from app.services import doc_extract

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.encrypt("secret")
    buf = io.BytesIO()
    writer.write(buf)
    with pytest.raises(doc_extract.DocExtractionError, match="password-protected"):
        doc_extract.extract_pdf_text(buf.getvalue(), max_chars=1000)

    with pytest.raises(doc_extract.DocExtractionError, match="no extractable text"):
        doc_extract.extract_pdf_text(_tiny_pdf(text=""), max_chars=1000)

    with pytest.raises(doc_extract.DocExtractionError, match="exceeds 5 characters"):
        doc_extract.extract_pdf_text(_tiny_pdf("way past the tiny cap"), max_chars=5)
    with pytest.raises(doc_extract.DocExtractionError, match="exceeds 5 characters"):
        doc_extract.extract_docx_text(_tiny_docx(), max_chars=5)

    with pytest.raises(doc_extract.DocExtractionError, match="corrupt"):
        doc_extract.extract_pdf_text(b"%PDF-1.4 truncated nonsense", max_chars=100)


async def test_import_pdf_and_docx_documents(client, db_session, monkeypatch, audit_db):
    from app.services import risk as risk_svc

    monkeypatch.setattr(risk_svc, "trigger_assessment", lambda *a: None)

    resp = await client.post(
        "/api/v1/projects/import",
        json={
            "name": "PDF Import",
            "document_b64": _b64(_tiny_pdf()),
            "document_name": "brief.pdf",
        },
    )
    assert resp.status_code == 201, resp.text
    from app.models import Project, Spec

    project = await db_session.get(Project, uuid.UUID(resp.json()["id"]))
    assert project.origin == "imported"
    specs = (
        (await db_session.execute(select(Spec).where(Spec.project_id == project.id)))
        .scalars().all()
    )
    assert [s.type for s in specs] == ["requirements"]
    assert specs[0].content == "hello marshal"

    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "DOCX Import", "document_b64": _b64(_tiny_docx())},
    )
    assert resp.status_code == 201, resp.text
    specs = (
        (await db_session.execute(
            select(Spec).where(Spec.project_id == uuid.UUID(resp.json()["id"]))
        )).scalars().all()
    )
    assert specs[0].content == "Hello marshal\nSecond para"


async def test_import_document_validation(client, audit_db):
    # XOR now spans four members
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "docs": {"requirements": "r"}, "document_b64": "aGk="},
    )
    assert resp.status_code == 422
    assert "docs, archive_b64, url or document_b64" in resp.json()["detail"]
    # bad base64
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "document_b64": "!!!not-base64!!!"},
    )
    assert resp.status_code == 422
    assert "document_b64 is not valid base64" in resp.json()["detail"]
    # scanned/empty PDF → the OCR boundary, honestly named
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "document_b64": _b64(_tiny_pdf(text=""))},
    )
    assert resp.status_code == 422
    assert "OCR" in resp.json()["detail"]
    # invalid UTF-8 that is neither pdf nor zip → supported-formats refusal
    resp = await client.post(
        "/api/v1/projects/import",
        json={"name": "X", "document_b64": _b64(b"\x89PNG\r\n\x1a\n....")},
    )
    assert resp.status_code == 422
    assert "not a supported format" in resp.json()["detail"]


async def test_import_url_pdf_content_type_ignored(
    test_user, client_for, db_session, audit_db, monkeypatch
):
    """A URL serving PDF bytes with a LYING Content-Type imports fine —
    magic bytes are the truth (R1.2)."""
    from app.services import project_import as import_svc
    from app.services import risk as risk_svc

    monkeypatch.setattr(risk_svc, "trigger_assessment", lambda *a: None)

    async def fake_fetch(url):
        return _tiny_pdf("from the web"), "text/html; charset=utf-8"

    monkeypatch.setattr(import_svc, "fetch_public_url", fake_fetch)
    async with client_for(test_user) as ac:
        resp = await ac.post(
            "/api/v1/projects/import",
            json={"name": "URL PDF", "url": "https://docs.example.com/spec.pdf"},
        )
    assert resp.status_code == 201, resp.text

    from app.models import Spec

    specs = (
        (await db_session.execute(
            select(Spec).where(Spec.project_id == uuid.UUID(resp.json()["id"]))
        )).scalars().all()
    )
    assert specs[0].content == "from the web"
