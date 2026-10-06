"""C0 connector registry (spec: .kiro/specs/external-import-connectors).

Typed references to external systems — data sources, HTTP APIs, agent
registries, MCP server listings — registered, credential-custodied and
probed from the control plane. Clones the S17 custom-model-endpoints
pattern exactly: entries in the platform-settings singleton, credentials
write-only in Secrets Manager (marshal/connectors/<slug>), probe results
stored on the entry.

Honesty contract (B-C0.4): registered connectors are NOT consumed by
generated agents yet. Consumption is the decision-gated C1 phase; until it
ships, this registry is inventory + reachability verification only.
"""

import asyncio
import json
import logging
import re
import time
import uuid
from datetime import UTC, datetime

import boto3
import httpx
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import PlatformSettings

logger = logging.getLogger("marshal.connectors")

SECRET_PREFIX = "marshal/connectors/"
CONNECTOR_TYPES = ("data_source", "http_api", "agent_registry", "mcp_server")

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,47}$")
_HEADER_RE = re.compile(r"^[A-Za-z0-9-]{1,64}$")
PROBE_TIMEOUT_S = 5


class ConnectorValidationError(Exception):
    """Invalid connector payload — surfaces as 422."""


class ConnectorNotFound(Exception):
    """Unknown slug — surfaces as 404."""


class ProbeBusy(Exception):
    """A probe for this connector is already running — surfaces as 409."""


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _validate_fields(data: dict, *, creating: bool) -> dict:
    fields: dict = {}
    if creating or "slug" in data:
        slug = str(data.get("slug") or "").strip().lower()
        if not _SLUG_RE.match(slug):
            raise ConnectorValidationError(
                "slug must be 2-48 chars of lowercase letters, digits and hyphens"
            )
        fields["slug"] = slug
    if creating or "name" in data:
        name = str(data.get("name") or "").strip()
        if not 1 <= len(name) <= 120:
            raise ConnectorValidationError("name must be 1-120 characters")
        fields["name"] = name
    if creating or "type" in data:
        ctype = str(data.get("type") or "").strip()
        if ctype not in CONNECTOR_TYPES:
            raise ConnectorValidationError(
                f"type must be one of: {', '.join(CONNECTOR_TYPES)}"
            )
        fields["type"] = ctype
    if creating or "base_url" in data:
        base_url = str(data.get("base_url") or "").strip().rstrip("/")
        if not base_url.startswith("https://") or len(base_url) > 512:
            raise ConnectorValidationError("base_url must be an https:// URL (≤512 chars)")
        fields["base_url"] = base_url
    if "description" in data and data["description"] is not None:
        description = str(data["description"]).strip()
        if len(description) > 500:
            raise ConnectorValidationError("description must be ≤500 characters")
        fields["description"] = description
    if "auth_header" in data and data["auth_header"] is not None:
        header = str(data["auth_header"]).strip()
        if not _HEADER_RE.match(header):
            raise ConnectorValidationError(
                "auth_header must be a valid header name (letters, digits, hyphens)"
            )
        fields["auth_header"] = header
    return fields


def public_view(entry: dict) -> dict:
    """What leaves the API: everything except any hint of the credential value."""
    return {
        "slug": entry["slug"],
        "name": entry.get("name"),
        "type": entry.get("type"),
        "base_url": entry.get("base_url"),
        "description": entry.get("description") or "",
        "auth_header": entry.get("auth_header") or "Authorization",
        "has_credential": bool(entry.get("has_credential")),
        "active": entry.get("active", True),
        "last_probe": entry.get("last_probe"),
        "mcp": entry.get("mcp"),  # C2: read-only handshake metadata
        "created_at": entry.get("created_at"),
        "updated_at": entry.get("updated_at"),
    }


async def _row(db: AsyncSession) -> PlatformSettings:
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(id=1)
        db.add(row)
        await db.flush()
    return row


def _find(entries: list, slug: str) -> dict:
    for entry in entries:
        if entry.get("slug") == slug:
            return entry
    raise ConnectorNotFound(f"No connector '{slug}'")


# ------------------------------------------------------------- secret store

_secrets_client = None


def _secrets():
    global _secrets_client
    if _secrets_client is None:
        _secrets_client = boto3.client(
            "secretsmanager", region_name=get_settings().aws_region
        )
    return _secrets_client


def _store_credential(slug: str, credential: str) -> None:
    name = f"{SECRET_PREFIX}{slug}"
    try:
        try:
            _secrets().create_secret(Name=name, SecretString=credential)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ResourceExistsException":
                raise
            _secrets().put_secret_value(SecretId=name, SecretString=credential)
    except (ClientError, BotoCoreError) as exc:
        raise ConnectorValidationError(f"Could not store the credential: {exc}") from exc


def _delete_credential(slug: str) -> None:
    try:
        _secrets().delete_secret(
            SecretId=f"{SECRET_PREFIX}{slug}", ForceDeleteWithoutRecovery=True
        )
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            logger.warning("connector secret delete failed for %s: %s", slug, exc)
    except BotoCoreError as exc:  # pragma: no cover — network-shape failure
        logger.warning("connector secret delete failed for %s: %s", slug, exc)


async def credential_for(slug: str) -> str | None:
    """Public read for platform-side consumers (C1 deployer copy step).

    STRICT: a credential-store failure propagates so the deploy fails closed —
    the copy step must never ship a ``null`` credential because Secrets
    Manager was unreachable. ``None`` here means "no credential registered"."""
    return await _credential_for(slug, degrade=False)


_credential_backend_warned = False


async def _credential_for(slug: str, *, degrade: bool = True) -> str | None:
    """Registry credential read. ``degrade=True`` (probe path) treats a
    credential-store failure as "no credential"; ``degrade=False`` (deploy
    path) re-raises it."""

    def read() -> str | None:
        global _credential_backend_warned
        try:
            resp = _secrets().get_secret_value(SecretId=f"{SECRET_PREFIX}{slug}")
            return resp.get("SecretString")
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                return None
            raise
        except BotoCoreError as exc:
            if not degrade:
                raise
            # No credentials / no region / endpoint unreachable: a misconfigured
            # installation probes without a credential instead of 500ing.
            # (NoCredentialsError is a BotoCoreError.) Logged once per process.
            if not _credential_backend_warned:
                _credential_backend_warned = True
                logger.warning(
                    "connector credential store unavailable (%s: %s) — probing "
                    "without credentials", type(exc).__name__, exc,
                )
            return None

    return await asyncio.to_thread(read)


# ---------------------------------------------------------------- lifecycle


async def list_connectors(db: AsyncSession) -> list[dict]:
    row = await db.get(PlatformSettings, 1)
    entries = list(getattr(row, "connectors", None) or []) if row else []
    return [public_view(e) for e in entries]


async def create_connector(db: AsyncSession, payload: dict, actor_id: uuid.UUID) -> dict:
    fields = _validate_fields(payload, creating=True)
    fields.setdefault("description", "")
    fields.setdefault("auth_header", "Authorization")
    fields["active"] = True
    fields["last_probe"] = None
    fields["created_at"] = _now()
    fields["updated_at"] = fields["created_at"]
    row = await _row(db)
    entries = list(row.connectors or [])
    if any(e.get("slug") == fields["slug"] for e in entries):
        raise ConnectorValidationError(f"Connector '{fields['slug']}' already exists")
    credential = (payload.get("credential") or "").strip()
    fields["has_credential"] = bool(credential)
    if credential:
        _store_credential(fields["slug"], credential)
    entries.append(fields)
    row.connectors = entries
    row.updated_by = actor_id
    await db.commit()
    logger.info("connector registered: %s (%s)", fields["slug"], fields["base_url"])
    return public_view(fields)


async def update_connector(
    db: AsyncSession, slug: str, payload: dict, actor_id: uuid.UUID
) -> tuple[dict, dict]:
    """Returns (public_view, {before, after}) for audit enrichment."""
    if "slug" in payload and payload["slug"] != slug:
        raise ConnectorValidationError("slug is immutable — register a new connector instead")
    fields = _validate_fields({k: v for k, v in payload.items() if k != "slug"}, creating=False)
    row = await _row(db)
    # Deep-copy entries before mutating: the loaded JSONB value and the new
    # value must not alias the same dicts, or SQLAlchemy sees no change.
    entries = [dict(e) for e in (row.connectors or [])]
    entry = _find(entries, slug)
    before = public_view(entry)
    entry.update(fields)
    credential = (payload.get("credential") or "").strip()
    if payload.get("clear_credential"):
        _delete_credential(slug)
        entry["has_credential"] = False
    elif credential:
        _store_credential(slug, credential)
        entry["has_credential"] = True
    entry["updated_at"] = _now()
    row.connectors = entries
    row.updated_by = actor_id
    await db.commit()
    return public_view(entry), {"before": before, "after": public_view(entry)}


async def set_connector_status(
    db: AsyncSession, slug: str, active: bool, actor_id: uuid.UUID
) -> dict:
    row = await _row(db)
    entries = [dict(e) for e in (row.connectors or [])]  # no-aliasing copy
    entry = _find(entries, slug)
    entry["active"] = active
    entry["updated_at"] = _now()
    row.connectors = entries
    row.updated_by = actor_id
    await db.commit()
    return public_view(entry)


async def delete_connector(db: AsyncSession, slug: str, actor_id: uuid.UUID) -> dict:
    row = await _row(db)
    entries = list(row.connectors or [])
    entry = _find(entries, slug)
    entries = [e for e in entries if e.get("slug") != slug]
    _delete_credential(slug)
    row.connectors = entries
    row.updated_by = actor_id
    await db.commit()
    logger.info("connector deleted: %s", slug)
    return public_view(entry)


# -------------------------------------------------------------------- probe

_probe_inflight: set[str] = set()


async def probe(db: AsyncSession, slug: str) -> dict:
    """Bounded HTTPS reachability check (B-C0.3) — stored on the entry.

    v0 proves reachability only (HEAD, GET fallback, credential header when
    present). An MCP handshake probe is C2 work; the UI says so. Errors are
    stored verbatim class + message and never raised to the caller.
    """
    if slug in _probe_inflight:
        raise ProbeBusy(f"A probe for '{slug}' is already running")
    _probe_inflight.add(slug)
    try:
        row = await _row(db)
        entries = [dict(e) for e in (row.connectors or [])]  # no-aliasing copy
        entry = _find(entries, slug)
        headers = {}
        credential = await _credential_for(slug)
        if credential:
            headers[entry.get("auth_header") or "Authorization"] = credential
        result: dict = {"ok": False, "status": None, "error": None, "error_class": None}
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                timeout=PROBE_TIMEOUT_S, follow_redirects=False
            ) as client:
                resp = await client.head(entry["base_url"], headers=headers)
                if resp.status_code == 405:  # HEAD refused — try GET
                    resp = await client.get(entry["base_url"], headers=headers)
                result["status"] = resp.status_code
                result["ok"] = resp.status_code < 500
                if not result["ok"]:
                    result["error_class"] = f"HTTP_{resp.status_code}"
                    result["error"] = (resp.text or "")[:300]
        except httpx.HTTPError as exc:
            result["error_class"] = type(exc).__name__
            result["error"] = str(exc)[:300]
        result["rtt_ms"] = int((time.monotonic() - started) * 1000)
        result["checked_at"] = _now()
        entry["last_probe"] = result
        # C2 (owner resolution, 7 Sep 2026): MCP servers additionally get a
        # read-only protocol handshake — metadata ONLY, never a tool call.
        if entry.get("type") == "mcp_server":
            entry["mcp"] = await _mcp_handshake(entry["base_url"], headers)
        row.connectors = entries
        await db.commit()
        return result
    finally:
        _probe_inflight.discard(slug)


# ------------------------------------------------------------- C2 MCP metadata

MCP_PROTOCOL_VERSION = "2025-03-26"
MAX_MCP_TOOLS = 40


def _parse_rpc_response(resp: httpx.Response) -> dict:
    """Streamable-HTTP servers may answer JSON or a single SSE frame."""
    if "text/event-stream" in resp.headers.get("content-type", ""):
        for line in (resp.text or "").splitlines():
            if line.startswith("data:"):
                return json.loads(line[5:].strip())
        raise ValueError("SSE response carried no data frame")
    return resp.json()


async def _mcp_handshake(base_url: str, headers: dict) -> dict:
    """Read-only MCP metadata (C2): initialize → initialized → tools/list
    over streamable HTTP. No tool CALLS — nothing but protocol metadata
    crosses, so redaction parity is satisfied by construction. Failures are
    stored honestly (the registry shows what the platform could not learn),
    never raised — reachability already lives in last_probe."""
    out: dict = {
        "ok": False, "protocol": None, "server": None,
        "tools": [], "error": None, "checked_at": _now(),
    }
    rpc_headers = {
        **headers,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    try:
        async with httpx.AsyncClient(
            timeout=PROBE_TIMEOUT_S, follow_redirects=False
        ) as client:
            init = await client.post(
                base_url,
                headers=rpc_headers,
                json={
                    "jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {
                        "protocolVersion": MCP_PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "marshal", "version": "1.0"},
                    },
                },
            )
            if session_id := init.headers.get("mcp-session-id"):
                rpc_headers["mcp-session-id"] = session_id
            body = _parse_rpc_response(init)
            if init.status_code >= 400 or body.get("error"):
                out["error"] = (
                    f"initialize refused: {str(body.get('error') or init.status_code)[:200]}"
                )
                return out
            result = body.get("result") or {}
            out["protocol"] = result.get("protocolVersion")
            info = result.get("serverInfo") or {}
            out["server"] = (
                f"{info.get('name', '?')} {info.get('version', '')}".strip() or None
            )
            await client.post(  # some servers require the initialized ack
                base_url, headers=rpc_headers,
                json={"jsonrpc": "2.0", "method": "notifications/initialized"},
            )
            tools_resp = await client.post(
                base_url, headers=rpc_headers,
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            )
            tools_body = _parse_rpc_response(tools_resp)
            tools = ((tools_body.get("result") or {}).get("tools") or [])[:MAX_MCP_TOOLS]
            out["tools"] = [
                {
                    "name": str(t.get("name", ""))[:80],
                    "description": str(t.get("description") or "")[:200],
                }
                for t in tools
                if isinstance(t, dict)
            ]
            out["ok"] = True
    except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
        out["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    return out


# --------------------------------------------------------- C2 spec grounding

GROUNDING_MAX_ENTRIES = 20
GROUNDING_MAX_CHARS = 4000


async def connector_grounding_block(db: AsyncSession) -> str:
    """Flag-gated, metadata-only connector context for the assistant (C2).

    Rides the chat system prompt and the spec-generation system prompts so
    the assistant can suggest real declarable connectors — names, types,
    descriptions and discovered MCP tools only, never credentials or probe
    internals. Empty string when the consumption flag is off (one switch
    with C1) or nothing active is registered.
    """
    from app.services.platform_settings import get_controls

    controls = await get_controls()
    if not controls.feature_flags.get("connectors_enabled", False):
        return ""
    row = await db.get(PlatformSettings, 1)
    entries = [
        e for e in ((row.connectors if row else None) or []) if e.get("active", True)
    ][:GROUNDING_MAX_ENTRIES]
    if not entries:
        return ""
    lines = []
    for e in entries:
        desc = (e.get("description") or "").strip()
        line = f"- {e['slug']} ({e.get('type')}): {desc}".rstrip(": ")[:200]
        tools = (e.get("mcp") or {}).get("tools") or []
        if tools:
            names = ", ".join(t.get("name", "") for t in tools[:12])
            line += f" [tools: {names}]"[:200]
        lines.append(line)
    block = (
        "\n\nREGISTERED CONNECTORS (external systems this installation can "
        "wire into generated agents; a requirements document opts in with "
        "the exact phrase: SHALL use connector <slug>):\n" + "\n".join(lines)
    )
    return block[:GROUNDING_MAX_CHARS]
