"""marshal SharePoint MCP adapter (sharepoint-connector spec R1/R2).

A small operator-run service speaking MCP (streamable HTTP, JSON-RPC) on
one side and Microsoft Graph on the other. marshal registers it as an
ordinary `mcp_server` connector: the C2 probe/handshake, C2 grounding and
C3 tool-loop agents consume it with ZERO platform changes, and marshal
never holds Microsoft credentials — the C1 copy custody carries only THIS
adapter's static API key.

Protocol subset (exactly what the platform's `_mcp_handshake` and the C3
scaffold's `_rpc` speak): POST / with JSON-RPC `initialize`,
`notifications/initialized`, `tools/list`, `tools/call`; the
`mcp-session-id` header is issued on initialize and echoed thereafter;
responses are plain JSON (the platform tolerates SSE frames but never
requires them).

Config (environment):
  SP_TENANT_ID, SP_CLIENT_ID, SP_CLIENT_SECRET  — Entra app (client credentials)
  SP_ALLOWED_SITES  — comma-separated site identifiers
                      ("contoso.sharepoint.com:/sites/engineering"), the
                      adapter-side allowlist ABOVE the Sites.Selected grants
  SP_API_KEY        — the static key marshal's registry custodies
  SP_AUTH_HEADER    — header carrying it (default "Authorization")

Logging: tool calls with the x-marshal-caller attribution header — never
document content, never credentials (R2.3).
"""

import logging
import os
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from extract import TEXT_EXTENSIONS, UnsupportedFormat, extract_text
from graph import GraphClient, GraphError

logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
logger = logging.getLogger("sharepoint-adapter")

PROTOCOL_VERSION = "2025-03-26"
SERVER_INFO = {"name": "marshal-sharepoint-adapter", "version": "1.0"}
MAX_FETCH_BYTES = 2 * 1024 * 1024  # R1.3 caps
MAX_TEXT_CHARS = 40_000
MAX_LIST_ITEMS = 50
MAX_SEARCH_ITEMS = 20

TOOLS = [
    {
        "name": "list_sites",
        "description": "List the SharePoint sites this adapter is allowed to reach.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "list_documents",
        "description": (
            "List documents in an allowed site's library. Optional folder "
            "path narrows the listing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "site": {"type": "string", "description": "Allowed site identifier"},
                "path": {"type": "string", "description": "Folder path (optional)"},
            },
            "required": ["site"],
        },
    },
    {
        "name": "search_documents",
        "description": "Search documents by keyword across the allowed sites.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "site": {"type": "string", "description": "Limit to one allowed site"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_document_text",
        "description": (
            "Fetch a document's text content (text-native formats: "
            + ", ".join(TEXT_EXTENSIONS)
            + "; 2MB / 40k-char caps; no OCR)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "drive_id": {"type": "string"},
                "item_id": {"type": "string"},
            },
            "required": ["drive_id", "item_id"],
        },
    },
]


class ToolError(Exception):
    """Honest tool-level failure — returned as an MCP error result."""


class AdapterState:
    """Config + caches. Site ids resolve lazily; drive→site mapping is the
    allowlist trace for get_document_text (defense in depth above
    Sites.Selected, R1.4)."""

    def __init__(self, *, graph: GraphClient, allowed_sites: list[str], api_key: str, auth_header: str):
        self.graph = graph
        self.allowed_sites = allowed_sites
        self.api_key = api_key
        self.auth_header = auth_header
        self.site_cache: dict[str, dict] = {}  # identifier -> site resource
        self.drive_sites: dict[str, str] = {}  # drive_id -> site identifier

    async def resolve_site(self, identifier: str) -> dict:
        if identifier not in self.allowed_sites:
            raise ToolError(
                f"Site '{identifier}' is not on this adapter's allowlist — "
                f"allowed: {', '.join(self.allowed_sites)}"
            )
        if identifier not in self.site_cache:
            self.site_cache[identifier] = await self.graph.get_json(f"/sites/{identifier}")
        return self.site_cache[identifier]


def state_from_env() -> AdapterState:
    missing = [
        name
        for name in ("SP_TENANT_ID", "SP_CLIENT_ID", "SP_CLIENT_SECRET", "SP_ALLOWED_SITES", "SP_API_KEY")
        if not os.environ.get(name)
    ]
    if missing:
        raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")
    return AdapterState(
        graph=GraphClient(
            os.environ["SP_TENANT_ID"],
            os.environ["SP_CLIENT_ID"],
            os.environ["SP_CLIENT_SECRET"],
        ),
        allowed_sites=[s.strip() for s in os.environ["SP_ALLOWED_SITES"].split(",") if s.strip()],
        api_key=os.environ["SP_API_KEY"],
        auth_header=os.environ.get("SP_AUTH_HEADER", "Authorization"),
    )


def create_app(state: AdapterState | None = None) -> FastAPI:
    app = FastAPI(title="marshal-sharepoint-adapter", docs_url=None, redoc_url=None)
    app.state.adapter = state or state_from_env()

    @app.get("/healthz")
    async def healthz() -> dict:
        return {"status": "ok"}

    @app.post("/")
    async def mcp(request: Request) -> JSONResponse:
        adapter: AdapterState = request.app.state.adapter
        supplied = request.headers.get(adapter.auth_header, "")
        if supplied.removeprefix("Bearer ").strip() != adapter.api_key:
            return JSONResponse({"error": "unauthorized"}, status_code=401)

        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "invalid JSON-RPC body"}, status_code=400)
        method = str(body.get("method") or "")
        rpc_id = body.get("id")
        params = body.get("params") or {}
        caller = request.headers.get("x-marshal-caller", "unknown")
        session = request.headers.get("mcp-session-id") or uuid.uuid4().hex

        def reply(result: dict | None, status: int = 200) -> JSONResponse:
            payload: dict = {"jsonrpc": "2.0"}
            if rpc_id is not None:
                payload["id"] = rpc_id
            if result is not None:
                payload["result"] = result
            return JSONResponse(
                payload, status_code=status, headers={"mcp-session-id": session}
            )

        if method == "initialize":
            return reply(
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "serverInfo": SERVER_INFO,
                    "capabilities": {"tools": {}},
                }
            )
        if method == "notifications/initialized":
            return reply(None, status=202)
        if method == "tools/list":
            return reply({"tools": TOOLS})
        if method == "tools/call":
            name = str(params.get("name") or "")
            arguments = params.get("arguments") or {}
            logger.info("tools/call %s caller=%s", name, caller)  # never content
            try:
                text = await _dispatch_tool(adapter, name, arguments)
                return reply({"content": [{"type": "text", "text": text}]})
            except (ToolError, GraphError, UnsupportedFormat) as exc:
                return reply(
                    {"content": [{"type": "text", "text": str(exc)}], "isError": True}
                )
        return JSONResponse(
            {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32601, "message": f"method '{method}' not supported"},
            },
            status_code=200,
            headers={"mcp-session-id": session},
        )

    return app


async def _dispatch_tool(state: AdapterState, name: str, args: dict) -> str:
    import json as _json

    if name == "list_sites":
        sites = []
        for identifier in state.allowed_sites:
            try:
                site = await state.resolve_site(identifier)
                sites.append(
                    {
                        "site": identifier,
                        "name": site.get("displayName"),
                        "web_url": site.get("webUrl"),
                    }
                )
            except GraphError as exc:
                sites.append({"site": identifier, "error": str(exc)})
        return _json.dumps({"sites": sites})

    if name == "list_documents":
        site_id = await _site_graph_id(state, str(args.get("site") or ""))
        drives = (await state.graph.get_json(f"/sites/{site_id}/drives")).get("value") or []
        path = str(args.get("path") or "").strip("/")
        documents = []
        for drive in drives[:5]:
            drive_id = str(drive.get("id"))
            state.drive_sites[drive_id] = str(args.get("site"))
            suffix = f"/root:/{path}:/children" if path else "/root/children"
            try:
                children = (
                    await state.graph.get_json(f"/drives/{drive_id}{suffix}")
                ).get("value") or []
            except GraphError:
                continue  # missing path in this drive — try the others
            for item in children:
                if "folder" in item:
                    continue
                documents.append(_item_out(item, drive_id))
                if len(documents) >= MAX_LIST_ITEMS:
                    return _json.dumps({"documents": documents, "truncated": True})
        return _json.dumps({"documents": documents})

    if name == "search_documents":
        query = str(args.get("query") or "").strip()
        if not query:
            raise ToolError("search_documents needs a non-empty query")
        targets = (
            [str(args["site"])] if args.get("site") else list(state.allowed_sites)
        )
        results = []
        for identifier in targets:
            site_id = await _site_graph_id(state, identifier)
            drives = (await state.graph.get_json(f"/sites/{site_id}/drives")).get("value") or []
            for drive in drives[:3]:
                drive_id = str(drive.get("id"))
                state.drive_sites[drive_id] = identifier
                safe_q = query.replace("'", "''")
                try:
                    hits = (
                        await state.graph.get_json(
                            f"/drives/{drive_id}/root/search(q='{safe_q}')"
                        )
                    ).get("value") or []
                except GraphError:
                    continue
                for item in hits:
                    if "folder" in item:
                        continue
                    results.append(_item_out(item, drive_id))
                    if len(results) >= MAX_SEARCH_ITEMS:
                        return _json.dumps({"results": results, "truncated": True})
        return _json.dumps({"results": results})

    if name == "get_document_text":
        drive_id = str(args.get("drive_id") or "")
        item_id = str(args.get("item_id") or "")
        if drive_id not in state.drive_sites:
            # Unknown drive: trace it to a site and hold it against the
            # allowlist before any content moves (R1.4 defense in depth).
            root = await state.graph.get_json(
                f"/drives/{drive_id}/root", params={"$select": "sharepointIds"}
            )
            site_id = ((root.get("sharepointIds") or {}).get("siteId") or "").lower()
            allowed_ids = set()
            for identifier in state.allowed_sites:
                site = await state.resolve_site(identifier)
                allowed_ids.update(part.lower() for part in str(site.get("id", "")).split(","))
            if not site_id or site_id not in allowed_ids:
                raise ToolError(
                    "This document's drive does not belong to an allowed site"
                )
            state.drive_sites[drive_id] = "resolved"
        meta = await state.graph.get_json(
            f"/drives/{drive_id}/items/{item_id}", params={"$select": "name,size"}
        )
        item_name = str(meta.get("name") or "document")
        if int(meta.get("size") or 0) > MAX_FETCH_BYTES:
            raise ToolError(
                f"'{item_name}' exceeds the {MAX_FETCH_BYTES // (1024 * 1024)}MB adapter limit"
            )
        raw = await state.graph.get_bytes(
            f"/drives/{drive_id}/items/{item_id}/content", max_bytes=MAX_FETCH_BYTES
        )
        return extract_text(item_name, raw, max_chars=MAX_TEXT_CHARS)

    raise ToolError(f"Unknown tool '{name}'")


async def _site_graph_id(state: AdapterState, identifier: str) -> str:
    site = await state.resolve_site(identifier)
    return str(site.get("id") or identifier)


def _item_out(item: dict, drive_id: str) -> dict:
    return {
        "name": item.get("name"),
        "item_id": item.get("id"),
        "drive_id": drive_id,
        "size": item.get("size"),
        "modified": item.get("lastModifiedDateTime"),
        "web_url": item.get("webUrl"),
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))  # noqa: S104
