"""SandboxProvider abstraction (FSD OQ-2: wrap Innovation Sandbox)."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC
from typing import Protocol

import boto3
import botocore.session
from botocore.credentials import RefreshableCredentials

# How an assumed-role session identifies itself to botocore's refresh logic.
ASSUMED_ROLE_METHOD = "sts-assume-role"


def refreshable_session(region: str, assume: Callable[[], dict]) -> boto3.Session:
    """boto3 Session whose credentials re-run ``assume`` before they expire.

    ``assume`` performs the STS AssumeRole and returns its ``Credentials``
    dict. botocore refreshes ahead of expiry (15 min advisory / 10 min
    mandatory), so a CloudFormation client held across an unbounded stack
    poll never fails with ExpiredToken — the role's MaxSessionDuration caps
    one session, not the deployment. Blocking (real STS on first call)."""

    def refresh() -> dict:
        creds = assume()
        expiration = creds["Expiration"]
        if expiration.tzinfo is None:  # stubbed/naive timestamps
            expiration = expiration.replace(tzinfo=UTC)
        return {
            "access_key": creds["AccessKeyId"],
            "secret_key": creds["SecretAccessKey"],
            "token": creds["SessionToken"],
            "expiry_time": expiration.isoformat(),
        }

    return session_from_credentials(
        region,
        RefreshableCredentials.create_from_metadata(
            metadata=refresh(), refresh_using=refresh, method=ASSUMED_ROLE_METHOD
        ),
    )


def session_from_credentials(region: str, credentials: RefreshableCredentials) -> boto3.Session:
    """Wrap an existing (shared) refreshable credential object in a new
    Session — every client created from it shares one refresh cycle."""
    botocore_session = botocore.session.get_session()
    # The same slot boto3 fills via set_credentials(); a refreshable object
    # here is the documented pattern for self-renewing assumed-role sessions.
    botocore_session._credentials = credentials
    return boto3.Session(botocore_session=botocore_session, region_name=region)


@dataclass
class LeaseInfo:
    external_lease_id: str | None
    aws_account_id: str | None
    status: str  # requested | active | terminating | terminated | failed
    # Attribution only (optional): the platform deployment this session serves.
    # Providers that mint STS sessions use it for the RoleSessionName so
    # CloudTrail in the target account names the deployment.
    deployment_id: str | None = None


@dataclass(frozen=True)
class ProviderCapacity:
    """Sanitized point-in-time provider pool capacity (no account ids)."""

    total_accounts: int
    available_accounts: int
    status_counts: dict[str, int]


class SandboxProvider(Protocol):
    """Provisions/terminates sandbox leases and supplies deployment credentials."""

    name: str

    async def request_lease(
        self,
        *,
        project_id: str,
        user_id: str,
        on_created: Callable[[LeaseInfo], Awaitable[None]] | None = None,
    ) -> LeaseInfo:
        """Provision a lease. `on_created` (B20 R0.2) fires the moment the
        provider knows the external lease id — BEFORE any activation wait —
        so the platform persists its record ahead of the blocking window."""
        ...

    async def get_lease(self, lease_ref: str) -> LeaseInfo: ...

    async def terminate_lease(self, lease_ref: str) -> None: ...

    async def lease_state(self, lease_ref: str) -> dict | None:
        """Budget/TTL surface for the deployment card (S11 R6) — None when the
        provider has nothing to report (direct provider, API trouble)."""
        ...

    async def capacity_snapshot(self) -> ProviderCapacity | None:
        """Trusted pool capacity, or None for a non-pooled local provider."""
        ...

    def deployment_session(self, lease: LeaseInfo) -> boto3.Session:
        """boto3 session with credentials able to deploy into the leased account.

        Blocking (real STS) — callers on the event loop wrap it in a thread
        (B20 R0.4)."""
        ...
