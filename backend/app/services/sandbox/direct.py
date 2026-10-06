"""DirectAccountProvider — the generic default (SANDBOX_PROVIDER=direct).

"Leases" the installation's own AWS account: real CloudFormation deployments
into the account the control plane runs in. Deployment credentials come from
the scoped in-account role AppStack provisions (MarshalDirectDeployRole,
``DIRECT_DEPLOY_ROLE_ARN``), assumed per deployment so CloudTrail attributes
every stack operation to the deployment that caused it. Without the role
(local/dev) the provider falls back to ambient credentials.
"""

import asyncio
import logging
import re
import threading
import uuid
from collections.abc import Awaitable, Callable

import boto3
from botocore.credentials import RefreshableCredentials

from app.core.config import get_settings
from app.services.sandbox.base import (
    LeaseInfo,
    ProviderCapacity,
    refreshable_session,
    session_from_credentials,
)

logger = logging.getLogger("marshal.sandbox.direct")

# STS RoleSessionName: 2..64 chars of [\w+=,.@-]; prefix + attribution key.
SESSION_NAME_PREFIX = "marshal-deploy-"
SESSION_NAME_MAX = 64
_SESSION_NAME_UNSAFE = re.compile(r"[^\w+=,.@-]")
# Role sessions are 1h (MaxSessionDuration on the role). The credentials are
# refreshable: botocore re-assumes ahead of expiry, so a client held across a
# long stack poll never dies mid-deployment.
SESSION_DURATION_S = 3600


def deploy_session_name(deployment_ref: str | None) -> str:
    """``marshal-deploy-<deployment id>`` clipped to the STS limit (64)."""
    ref = _SESSION_NAME_UNSAFE.sub("-", deployment_ref or "adhoc")
    return f"{SESSION_NAME_PREFIX}{ref}"[:SESSION_NAME_MAX]


class DirectAccountProvider:
    name = "direct"

    def __init__(self) -> None:
        self._account_id: str | None = None
        # One refreshable credential object per deployment (keyed by the
        # attribution ref): deploy, poll and teardown of one deployment share
        # it, and it re-assumes itself ahead of expiry. Expired entries are
        # pruned on insert. Guarded: deployment_session runs on worker threads.
        self._credentials: dict[str, RefreshableCredentials] = {}
        self._lock = threading.Lock()
        self._fallback_warned = False

    async def _resolve_account(self) -> str:
        if self._account_id is None:
            def call() -> str:
                sts = boto3.client("sts", region_name=get_settings().aws_region)
                return sts.get_caller_identity()["Account"]

            self._account_id = await asyncio.to_thread(call)
        return self._account_id

    async def request_lease(
        self,
        *,
        project_id: str,
        user_id: str,
        on_created: Callable[[LeaseInfo], Awaitable[None]] | None = None,
    ) -> LeaseInfo:
        account_id = await self._resolve_account()
        info = LeaseInfo(
            external_lease_id=f"direct-{uuid.uuid4().hex[:12]}",
            aws_account_id=account_id,
            status="active",
        )
        if on_created is not None:  # uniform contract (B20 R0.2); no wait here
            await on_created(info)
        return info

    async def get_lease(self, lease_ref: str) -> LeaseInfo:
        return LeaseInfo(
            external_lease_id=lease_ref,
            aws_account_id=await self._resolve_account(),
            status="active",
        )

    async def lease_state(self, lease_ref: str) -> dict | None:
        return None  # the direct provider has no lease economics to report

    async def capacity_snapshot(self) -> ProviderCapacity | None:
        return None  # local/direct mode is not a managed account pool

    async def terminate_lease(self, lease_ref: str) -> None:
        # Nothing to release — account is not pooled. Record-keeping happens in DB.
        return None

    def deployment_session(self, lease: LeaseInfo) -> boto3.Session:
        settings = get_settings()
        role_arn = (settings.direct_deploy_role_arn or "").strip()
        if not role_arn:
            if not self._fallback_warned:
                self._fallback_warned = True
                logger.warning(
                    "DIRECT_DEPLOY_ROLE_ARN unset — direct deployments run on the "
                    "control plane's own credentials (local/dev fallback)"
                )
            return boto3.Session(region_name=settings.aws_region)
        ref = lease.deployment_id or lease.external_lease_id or "adhoc"
        with self._lock:
            cached = self._credentials.get(ref)
        if cached is not None:
            return session_from_credentials(settings.aws_region, cached)
        # The STS call runs outside the lock; a racing duplicate assume for the
        # same deployment is harmless (last writer wins, both are valid).
        sts = boto3.client("sts", region_name=settings.aws_region)
        session_name = deploy_session_name(ref)

        def assume() -> dict:
            return sts.assume_role(
                RoleArn=role_arn,
                RoleSessionName=session_name,
                DurationSeconds=SESSION_DURATION_S,
            )["Credentials"]

        session = refreshable_session(settings.aws_region, assume)
        with self._lock:
            # Prune deployments whose credentials have lapsed (torn down or
            # idle past one session) so the cache stays bounded.
            self._credentials = {
                k: c for k, c in self._credentials.items() if not c.refresh_needed(refresh_in=0)
            }
            self._credentials[ref] = session.get_credentials()
        return session
