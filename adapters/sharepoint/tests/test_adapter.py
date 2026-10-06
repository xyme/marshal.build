"""SharePoint adapter tests: MCP surface, auth, allowlist, extraction caps.

Graph is mocked via an injected httpx.MockTransport — no network, no real
tenant. (This is a NEW standalone component: its suite starts here rather
than appending to the platform suite, which cannot see this directory.)
"""

import io
import json
import sys
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from adapter import AdapterState, create_app  # noqa: E402
from graph import GraphClient  # noqa: E402

API_KEY = "test-key-123"
SITE = "contoso.sharepoint.com:/sites/eng"
SITE_ID = "contoso.sharepoint.com,11111111-aaaa,22222222-bbbb"


def _docx(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0"?><w:document xmlns:w="http://schemas.'
            'openxmlformats.org/wordprocessingml/2006/main"><w:body>'
            f"<w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body></w:document>",
        )
    return buf.getvalue()


def _mock_graph() -> httpx.MockTransport:
    def route(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "login.microsoftonline.com" in url:
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if url.endswith(f"/sites/{SITE}"):
            return httpx.Response(
                200, json={"id": SITE_ID, "displayName": "Engineering", "webUrl": "https://x"}
            )
        if url.endswith("/drives") and SITE_ID in url:
            return httpx.Response(200, json={"value": [{"id": "drive1", "name": "Documents"}]})
        if url.endswith("/drives/drive1/root/children"):
            return httpx.Response(
                200,
                json={
                    "value": [
                        {"id": "doc1", "name": "spec.docx", "size": 1234,
                         "lastModifiedDateTime": "2026-09-01T00:00:00Z",
                         "file": {"mimeType": "application/vnd.openxmlformats"}},
                        {"id": "folder1", "name": "Archive", "folder": {}},
                    ]
                },
            )
        if url.endswith("/drives/drive1/items/doc1?%24select=name%2Csize") or (
            "/drives/drive1/items/doc1" in url and "select" in url
        ):
            return httpx.Response(200, json={"name": "spec.docx", "size": 1234})
        if url.endswith("/drives/drive1/items/doc1/content"):
            return httpx.Response(200, content=_docx("Adapter drill content"))
        if "/drives/drive1/items/sheet1" in url and "select" in url:
            return httpx.Response(200, json={"name": "numbers.xlsx", "size": 999})
        if url.endswith("/drives/drive1/items/sheet1/content"):
            return httpx.Response(200, content=b"PK\x03\x04not-a-docx")
        return httpx.Response(404, json={"error": "not found"})

    return httpx.MockTransport(route)


@pytest.fixture()
def client() -> TestClient:
    state = AdapterState(
        graph=GraphClient("tenant", "client", "secret", transport=_mock_graph()),
        allowed_sites=[SITE],
        api_key=API_KEY,
        auth_header="Authorization",
    )
    return TestClient(create_app(state))


def _rpc(client: TestClient, method: str, params: dict | None = None, *, key: str = API_KEY):
    return client.post(
        "/",
        json={"jsonrpc": "2.0", "method": method, "params": params or {}, "id": 1},
        headers={"Authorization": key},
    )


def test_static_key_is_required(client):
    assert _rpc(client, "initialize", key="wrong").status_code == 401
    ok = _rpc(client, "initialize")
    assert ok.status_code == 200
    body = ok.json()["result"]
    assert body["protocolVersion"] == "2025-03-26"
    assert body["serverInfo"]["name"] == "marshal-sharepoint-adapter"
    assert ok.headers.get("mcp-session-id")
    # Bearer prefix tolerated (the C1 composite may configure either)
    bearer = _rpc(client, "initialize", key=f"Bearer {API_KEY}")
    assert bearer.status_code == 200


def test_handshake_lists_four_tools(client):
    assert _rpc(client, "notifications/initialized").status_code == 202
    tools = _rpc(client, "tools/list").json()["result"]["tools"]
    assert [t["name"] for t in tools] == [
        "list_sites", "list_documents", "search_documents", "get_document_text",
    ]
    assert all(t["inputSchema"]["type"] == "object" for t in tools)


def test_list_sites_and_documents(client):
    sites = json.loads(
        _rpc(client, "tools/call", {"name": "list_sites", "arguments": {}})
        .json()["result"]["content"][0]["text"]
    )
    assert sites["sites"][0]["name"] == "Engineering"

    docs = json.loads(
        _rpc(client, "tools/call", {"name": "list_documents", "arguments": {"site": SITE}})
        .json()["result"]["content"][0]["text"]
    )
    names = [d["name"] for d in docs["documents"]]
    assert names == ["spec.docx"]  # folders excluded
    assert docs["documents"][0]["drive_id"] == "drive1"


def test_allowlist_refusal_is_honest(client):
    result = _rpc(
        client, "tools/call",
        {"name": "list_documents", "arguments": {"site": "evil.sharepoint.com:/sites/other"}},
    ).json()["result"]
    assert result.get("isError") is True
    assert "allowlist" in result["content"][0]["text"]


def test_get_document_text_docx_and_refusals(client):
    # Prime the drive→site trace through a listing first (the normal flow)
    _rpc(client, "tools/call", {"name": "list_documents", "arguments": {"site": SITE}})

    text = _rpc(
        client, "tools/call",
        {"name": "get_document_text", "arguments": {"drive_id": "drive1", "item_id": "doc1"}},
    ).json()["result"]["content"][0]["text"]
    assert text == "Adapter drill content"

    refused = _rpc(
        client, "tools/call",
        {"name": "get_document_text", "arguments": {"drive_id": "drive1", "item_id": "sheet1"}},
    ).json()["result"]
    assert refused.get("isError") is True
    assert "not a text-native format" in refused["content"][0]["text"]


def test_unknown_method_gets_jsonrpc_error(client):
    resp = _rpc(client, "resources/list")
    assert resp.status_code == 200
    assert resp.json()["error"]["code"] == -32601
