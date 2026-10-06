"""IsbHttpProvider — client for the Innovation Sandbox on AWS REST API.

Contract verified against the deployed solution (July 2026 release) and its
source (github.com/aws-solutions/innovation-sandbox-on-aws):

- Auth: `Authorization: Bearer <HS256 JWT>` where the JWT payload carries
  `{"user": {"email", "roles": ["Admin"|"Manager"|"User"], ...}}` and is signed
  with the shared secret at Secrets Manager `/InnovationSandbox/<ns>/Auth/JwtSecret`
  (the same mechanism the ISB web UI uses after IDC sign-in). marshal mints a
  short-lived service token per call window.
- Envelope: JSend — `{"status": "success", "data": ...}`.
- Endpoints:
    GET  /leaseTemplates                     list (data.result[])
    POST /leaseTemplates                     create
    POST /leases        {leaseTemplateUuid, comments?}   -> 201 data: Lease
    GET  /leases/{uuid}                      data: Lease
    POST /leases/{uuid}/terminate            terminate lease (account -> cleanup)
    POST /accounts      {awsAccountId}       register account into the pool
    GET  /accounts?pageSize=...              list pool accounts (data.result[])
    GET  /blueprints/stacksets               list registrable StackSets
    POST /blueprints    {name, stackSetId, regions, ...}
- Lease statuses: PendingApproval | ApprovalDenied | Provisioning | Active |
  Frozen | Expired | BudgetExceeded | ManuallyTerminated | AccountQuarantined | Ejected

`request_lease` blocks until the lease is Active (ISB provisions the account and
deploys the attached blueprint StackSet during Provisioning), then returns the
leased `awsAccountId`. Deployment credentials come from assuming the
blueprint-provisioned deployment role in the leased account.
"""

import asyncio
import base64
import json
import logging
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import boto3
import httpx
import jwt

from app.core.config import get_settings
from app.services.sandbox.base import LeaseInfo, ProviderCapacity, refreshable_session

logger = logging.getLogger("marshal.sandbox.isb")

ACTIVATION_POLL_SECONDS = 5

_STATUS_MAP = {
    "PendingApproval": "requested",
    "Provisioning": "requested",
    "Active": "active",
    "Frozen": "failed",
    "ApprovalDenied": "failed",
    "AccountQuarantined": "failed",
    "Expired": "terminated",
    "BudgetExceeded": "terminated",
    "ManuallyTerminated": "terminated",
    "Ejected": "terminated",
}

SERVICE_USER = {
    "email": "orchestrator@marshal.ai",
    "displayName": "marshal Orchestrator",
    "roles": ["Admin"],
}


class IsbApiError(RuntimeError):
    def __init__(self, status_code: int, message: str):
        super().__init__(f"ISB API {status_code}: {message}")
        self.status_code = status_code


class IsbHttpProvider:
    name = "isb"

    def __init__(
        self,
        base_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        jwt_secret: str | None = None,
    ):
        settings = get_settings()
        self.base_url = (base_url or settings.isb_api_base_url).rstrip("/")
        if not self.base_url:
            raise ValueError(
                "SANDBOX_PROVIDER=isb requires ISB_API_BASE_URL (see docs/innovation-sandbox-setup.md)"
            )
        self.region = settings.aws_region
        self.jwt_secret_name = settings.isb_jwt_secret_name
        self.lease_template_name = settings.isb_lease_template_name
        self.deployment_role_name = settings.isb_deployment_role_name
        self.activation_timeout_s = settings.isb_lease_activation_timeout_s
        self._client = httpx.AsyncClient(transport=transport, timeout=30)
        self._jwt_secret: str | None = jwt_secret
        self._token: str | None = None
        self._token_exp: float = 0
        self._template_uuid: str | None = None

    # ------------------------------------------------------------ auth

    def _fetch_secret(self) -> str:
        sm = boto3.client("secretsmanager", region_name=self.region)
        return sm.get_secret_value(SecretId=self.jwt_secret_name)["SecretString"]

    async def _service_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_exp - 60:
            return self._token
        if self._jwt_secret is None:
            self._jwt_secret = await asyncio.to_thread(self._fetch_secret)
        exp = int(now) + 900
        self._token = jwt.encode(
            {"user": SERVICE_USER, "iat": int(now), "exp": exp},
            self._jwt_secret,
            algorithm="HS256",
        )
        self._token_exp = exp
        return self._token

    # ------------------------------------------------------------ http

    async def _request(self, method: str, path: str, payload: dict | None = None) -> Any:
        token = await self._service_token()
        response = await self._client.request(
            method,
            f"{self.base_url}{path}",
            headers={"Authorization": f"Bearer {token}"},
            json=payload,
        )
        if response.status_code >= 400:
            detail = response.text[:500]
            try:
                body = response.json()
                detail = str(body.get("data") or body.get("message") or detail)[:500]
            except Exception:  # noqa: BLE001
                pass
            raise IsbApiError(response.status_code, detail)
        if not response.content:
            return {}
        body = response.json()
        return body.get("data", body)

    # ------------------------------------------------------------ lease template

    async def _ensure_lease_template(self) -> str:
        """Find (or create) the marshal lease template; returns its uuid."""
        if self._template_uuid:
            return self._template_uuid
        settings = get_settings()
        data = await self._request("GET", "/leaseTemplates?pageSize=200")
        for item in data.get("result", []):
            if item.get("name") == self.lease_template_name:
                self._template_uuid = item["uuid"]
                return self._template_uuid
        payload: dict[str, Any] = {
            "name": self.lease_template_name,
            "description": "marshal managed lease template (auto-created)",
            "requiresApproval": False,
            "visibility": "PUBLIC",
            "maxSpend": settings.isb_lease_budget_usd,
            "leaseDurationInHours": settings.isb_lease_duration_hours,
        }
        if settings.isb_blueprint_id:
            payload["blueprintId"] = settings.isb_blueprint_id
        created = await self._request("POST", "/leaseTemplates", payload)
        self._template_uuid = created["uuid"]
        logger.info("Created ISB lease template %s (%s)", self.lease_template_name, self._template_uuid)
        return self._template_uuid

    # ------------------------------------------------------------ provider API

    @staticmethod
    def _lease_path_id(data: dict[str, Any]) -> str | None:
        """ISB path ids are base64(JSON({userEmail, uuid})) — the DynamoDB composite key."""
        if not (data.get("userEmail") and data.get("uuid")):
            return data.get("uuid")
        composite = json.dumps(
            {"userEmail": data["userEmail"], "uuid": data["uuid"]}, separators=(",", ":")
        )
        return base64.b64encode(composite.encode()).decode()

    @classmethod
    def _to_lease(cls, data: dict[str, Any]) -> LeaseInfo:
        return LeaseInfo(
            external_lease_id=cls._lease_path_id(data),
            aws_account_id=data.get("awsAccountId"),
            status=_STATUS_MAP.get(str(data.get("status")), "requested"),
        )

    async def request_lease(
        self,
        *,
        project_id: str,
        user_id: str,
        on_created: Callable[[LeaseInfo], Awaitable[None]] | None = None,
    ) -> LeaseInfo:
        template_uuid = await self._ensure_lease_template()
        created = await self._request(
            "POST",
            "/leases",
            {
                "leaseTemplateUuid": template_uuid,
                "comments": f"marshal-ai project={project_id} user={user_id}",
            },
        )
        lease = self._to_lease(created)
        if on_created is not None:
            # B20 R0.2: hand the external id over BEFORE the activation wait —
            # a crash during activation must leave a terminable record, not an
            # orphan ISB lease with no platform row.
            await on_created(lease)
        deadline = time.monotonic() + self.activation_timeout_s
        while lease.status == "requested" and time.monotonic() < deadline:
            await asyncio.sleep(ACTIVATION_POLL_SECONDS)
            lease = await self.get_lease(lease.external_lease_id)
        if lease.status != "active":
            raise RuntimeError(
                f"ISB lease {lease.external_lease_id} did not activate in time (status={lease.status})"
            )
        logger.info("ISB lease %s active on account %s", lease.external_lease_id, lease.aws_account_id)
        return lease

    async def get_lease(self, lease_ref: str) -> LeaseInfo:
        return self._to_lease(await self._request("GET", f"/leases/{lease_ref}"))

    async def capacity_snapshot(self) -> ProviderCapacity:
        """Strict, sanitized parse of the deployed ISB `/accounts` contract.

        Account identifiers never leave this adapter. Malformed/truncated
        pagination raises so cloud admission fails closed instead of treating
        an untrusted response as spare capacity.
        """
        statuses: Counter[str] = Counter()
        total = 0
        page_identifier: str | None = None
        seen_pages: set[str] = set()
        while True:
            path = "/accounts?pageSize=200"
            if page_identifier:
                path += f"&pageIdentifier={quote(page_identifier, safe='')}"
            data = await self._request("GET", path)
            if not isinstance(data, dict) or not isinstance(data.get("result"), list):
                raise IsbApiError(502, "malformed /accounts result envelope")
            for account in data["result"]:
                if not isinstance(account, dict) or not isinstance(
                    account.get("status"), str
                ):
                    raise IsbApiError(502, "malformed /accounts account status")
                statuses[account["status"]] += 1
                total += 1
            next_identifier = data.get("nextPageIdentifier")
            if not next_identifier:
                break
            page_identifier = str(next_identifier)
            if page_identifier in seen_pages:
                raise IsbApiError(502, "repeated /accounts page identifier")
            seen_pages.add(page_identifier)
        return ProviderCapacity(
            total_accounts=total,
            available_accounts=statuses.get("Available", 0),
            status_counts=dict(sorted(statuses.items())),
        )

    async def lease_state(self, lease_ref: str) -> dict | None:
        """Budget/TTL from the ISB lease record (S11 R6) — best-effort."""
        try:
            data = await self._request("GET", f"/leases/{lease_ref}")
            body = data.get("data", data) if isinstance(data, dict) else {}
            return {
                "budget_used_usd": body.get("totalCostAccrued"),
                "budget_cap_usd": body.get("maxSpend"),
                "expires_at": body.get("expirationDate"),
                "status": body.get("status"),
            }
        except Exception:  # noqa: BLE001 — surfacing is best-effort (R6.2)
            return None

    async def terminate_lease(self, lease_ref: str) -> None:
        await self._request("POST", f"/leases/{lease_ref}/terminate")

    def deployment_session(self, lease: LeaseInfo) -> boto3.Session:
        """Assume the blueprint-provisioned deployment role in the leased account.

        The credentials re-assume themselves ahead of expiry, so a client held
        across a long stack poll never fails with ExpiredToken."""
        sts = boto3.client("sts", region_name=self.region)
        role_arn = f"arn:aws:iam::{lease.aws_account_id}:role/{self.deployment_role_name}"
        return refreshable_session(
            self.region,
            lambda: sts.assume_role(RoleArn=role_arn, RoleSessionName="marshal-deploy")[
                "Credentials"
            ],
        )
