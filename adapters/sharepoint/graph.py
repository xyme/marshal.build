"""Microsoft Graph client for the marshal SharePoint adapter (R2).

Owns the Entra client-credentials flow and token cache — marshal never
holds Microsoft credentials (sharepoint-connector R1.2). Token refreshes
are single-flight; the cached token is reused until ~5 minutes before
expiry. All Graph calls are READ-ONLY v1.0 endpoints.
"""

import asyncio
import logging
import time

import httpx

logger = logging.getLogger("sharepoint-adapter.graph")

GRAPH = "https://graph.microsoft.com/v1.0"
_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
_REFRESH_MARGIN_S = 300


class GraphError(Exception):
    """Surfaced to the MCP caller as an honest tool error."""


class GraphClient:
    def __init__(
        self,
        tenant_id: str,
        client_id: str,
        client_secret: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self._client = httpx.AsyncClient(timeout=20, transport=transport)
        self._token: str | None = None
        self._expires_at: float = 0.0
        self._refresh_lock = asyncio.Lock()

    async def _get_token(self) -> str:
        if self._token and time.monotonic() < self._expires_at - _REFRESH_MARGIN_S:
            return self._token
        async with self._refresh_lock:
            if self._token and time.monotonic() < self._expires_at - _REFRESH_MARGIN_S:
                return self._token  # single-flight: another waiter refreshed
            resp = await self._client.post(
                _TOKEN_URL.format(tenant=self.tenant_id),
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "scope": "https://graph.microsoft.com/.default",
                },
            )
            if resp.status_code != 200:
                logger.warning("token exchange failed: HTTP %s", resp.status_code)
                raise GraphError(
                    f"Microsoft token exchange failed (HTTP {resp.status_code}) — "
                    "check the Entra app credentials"
                )
            body = resp.json()
            self._token = str(body["access_token"])
            self._expires_at = time.monotonic() + int(body.get("expires_in", 3600))
            return self._token

    async def get_json(self, path: str, params: dict | None = None) -> dict:
        token = await self._get_token()
        resp = await self._client.get(
            f"{GRAPH}{path}",
            params=params,
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code == 403:
            raise GraphError(
                "Microsoft Graph refused the call (403) — the Entra app may "
                "lack a Sites.Selected grant for this site"
            )
        if resp.status_code == 404:
            raise GraphError("Not found on Microsoft Graph (404)")
        if resp.status_code != 200:
            raise GraphError(f"Microsoft Graph returned HTTP {resp.status_code}")
        return resp.json()

    async def get_bytes(self, path: str, *, max_bytes: int) -> bytes:
        """Streamed content download with a hard size cap (R1.3)."""
        token = await self._get_token()
        async with self._client.stream(
            "GET",
            f"{GRAPH}{path}",
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=True,
        ) as resp:
            if resp.status_code != 200:
                raise GraphError(f"Content download returned HTTP {resp.status_code}")
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > max_bytes:
                    raise GraphError(
                        f"Document exceeds the {max_bytes // (1024 * 1024)}MB "
                        "adapter fetch limit"
                    )
                chunks.append(chunk)
            return b"".join(chunks)

    async def aclose(self) -> None:
        await self._client.aclose()
