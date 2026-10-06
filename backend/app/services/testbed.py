"""Testbed credential vending (B20 R2).

A Testbed-mode deployment lets its owner mint LIMITED, time-boxed AWS
credentials for the leased account — the self-configuration the pipeline
cannot do for them (EULA/marketplace acceptance, service-quota requests,
enabling extra services). Custody story:

- The role (`MarshalTestbedUserRole`) is bootstrapped LAZILY on first mint,
  through the same assumed role the deployer uses. Full-Governance leases
  never carry it.
- Its policy is curated HERE (Allow * behind custody-critical explicit
  Denies); ISB's SCPs remain the outer wall. Re-put on every bootstrap so a
  policy fix in this file reaches existing roles on the next mint.
- Only the platform's own role may assume it (trust derived from the running
  identity); sessions are `testbed_session_hours` long (policy, 1..12),
  RoleSessionName carries the marshal user for CloudTrail attribution.
- NOTHING is stored platform-side: sessions expire on their own, the role
  dies with the account at lease termination, and every issuance is a
  SECURITY-audited event (the B13 reveal precedent).
- The direct provider IS the platform account — vending refuses anything
  but an isb lease.
"""

import asyncio
import json
import logging
import time
from urllib.parse import quote

import boto3
import httpx
from botocore.exceptions import ClientError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.models import Deployment, Lease, Project, User
from app.services.deployment import provider_for_lease
from app.services.policies import resolve_deployment_policies
from app.services.sandbox.base import LeaseInfo

logger = logging.getLogger("marshal.testbed")

TESTBED_ROLE_NAME = "MarshalTestbedUserRole"
FEDERATION_ENDPOINT = "https://signin.aws.amazon.com/federation"

# Allow-everything self-configuration surface behind custody-critical Denies.
# The POINT of a Testbed is services nobody predicted (EULA flows live under
# arbitrary consoles) — an allowlist would recreate the problem the mode
# solves. Deny wall: identity mutation, role chaining, org/account/billing,
# trail/config tampering, and marshal's own custody surfaces.
TESTBED_POLICY: dict = {
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "AllowSelfConfiguration",
            "Effect": "Allow",
            "Action": "*",
            "Resource": "*",
        },
        {
            # Identity is custody: no principal/policy mutation. Creating
            # SERVICE-LINKED roles stays possible (enabling many AWS services
            # implicitly creates one) because the deny is scoped off the
            # aws-service-role path.
            "Sid": "DenyIamMutation",
            "Effect": "Deny",
            "Action": [
                "iam:Add*", "iam:Attach*", "iam:Create*", "iam:Delete*",
                "iam:Detach*", "iam:Put*", "iam:Remove*", "iam:Tag*",
                "iam:Untag*", "iam:Update*", "iam:PassRole",
            ],
            "NotResource": "arn:aws:iam::*:role/aws-service-role/*",
        },
        {
            # No chaining out of the vended session, no re-federation.
            "Sid": "DenyStsEscalation",
            "Effect": "Deny",
            "Action": ["sts:AssumeRole", "sts:GetFederationToken"],
            "Resource": "*",
        },
        {
            "Sid": "DenyGovernanceTamper",
            "Effect": "Deny",
            "Action": [
                "cloudtrail:DeleteTrail", "cloudtrail:PutEventSelectors",
                "cloudtrail:StopLogging", "cloudtrail:UpdateTrail",
                "config:Delete*", "config:Stop*",
                "organizations:*", "account:*",
                "aws-portal:Modify*",
                "budgets:CreateBudgetAction", "budgets:DeleteBudgetAction",
                "budgets:ModifyBudget", "budgets:UpdateBudgetAction",
            ],
            "Resource": "*",
        },
        {
            # marshal's deployed stacks stay marshal's: no deleting/mutating
            # them or the staged-asset bucket from a vended session.
            "Sid": "DenyMarshalStackMutation",
            "Effect": "Deny",
            "Action": [
                "cloudformation:DeleteStack", "cloudformation:SetStackPolicy",
                "cloudformation:UpdateStack",
            ],
            "Resource": "arn:aws:cloudformation:*:*:stack/marshal-*",
        },
        {
            "Sid": "DenyMarshalAssetBucket",
            "Effect": "Deny",
            "Action": "s3:*",
            "Resource": [
                "arn:aws:s3:::marshal-assets-*",
                "arn:aws:s3:::marshal-assets-*/*",
            ],
        },
        {
            # Belt-and-braces: the vended role cannot touch itself even if
            # the SLR carve-out above ever loosens.
            "Sid": "DenyTestbedRoleTamper",
            "Effect": "Deny",
            "Action": "iam:*",
            "Resource": f"arn:aws:iam::*:role/{TESTBED_ROLE_NAME}",
        },
    ],
}


class TestbedUnavailable(Exception):
    """Vending refused for a stateful reason — surfaces as 409."""


class TestbedDisabled(Exception):
    """Vending disabled by platform policy — surfaces as 422."""


def _platform_principal_arn() -> str:
    """The IAM principal to trust: derived from the RUNNING identity so the
    trust policy names exactly the platform's task role (or dev identity)."""
    arn = boto3.client(
        "sts", region_name=get_settings().aws_region
    ).get_caller_identity()["Arn"]
    # arn:aws:sts::123:assumed-role/RoleName/session -> the underlying role
    if ":assumed-role/" in arn:
        account = arn.split(":")[4]
        role_name = arn.split(":assumed-role/")[1].split("/")[0]
        return f"arn:aws:iam::{account}:role/{role_name}"
    return arn  # dev identities (iam user) pass through unchanged


def _bootstrap_role(session, trust_arn: str) -> None:
    """Idempotent: ensure the role exists in the leased account and its
    policy matches THIS file (re-put every time — repo is the source)."""
    iam = session.client("iam")
    trust = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"AWS": trust_arn},
                "Action": "sts:AssumeRole",
            }
        ],
    }
    try:
        iam.get_role(RoleName=TESTBED_ROLE_NAME)
    except iam.exceptions.NoSuchEntityException:
        iam.create_role(
            RoleName=TESTBED_ROLE_NAME,
            AssumeRolePolicyDocument=json.dumps(trust),
            # NOTE: IAM restricts Description to a basic-latin charset —
            # ASCII only here (an em-dash failed live, drill run 2).
            Description="marshal Testbed self-configuration role (B20 R2): "
            "limited, time-boxed user sessions minted by the platform",
            MaxSessionDuration=12 * 3600,
            Tags=[{"Key": "marshal-ai:managed", "Value": "true"}],
        )
    iam.put_role_policy(
        RoleName=TESTBED_ROLE_NAME,
        PolicyName="MarshalTestbedLimits",
        PolicyDocument=json.dumps(TESTBED_POLICY),
    )


def _assume_testbed_role(account_id: str, session_name: str, hours: int) -> dict:
    """AssumeRole with propagation patience: a role created moments ago is not
    yet visible to STS everywhere (IAM eventual consistency), so the FIRST
    mint after bootstrap can AccessDenied spuriously — retry briefly rather
    than surface a refusal the user would just click through again."""
    sts = boto3.client("sts", region_name=get_settings().aws_region)
    arn = f"arn:aws:iam::{account_id}:role/{TESTBED_ROLE_NAME}"
    duration = hours * 3600
    for attempt in range(6):
        try:
            return sts.assume_role(
                RoleArn=arn,
                RoleSessionName=session_name,
                DurationSeconds=duration,
            )["Credentials"]
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            message = str(exc)
            # Role CHAINING ceiling (found live, drill run 4): the platform's
            # own credentials are an assumed-role session (ECS task role), so
            # this AssumeRole is chained and AWS hard-caps it at 1 hour no
            # matter the role's MaxSessionDuration. Honor the policy hours
            # where the platform runs on long-lived credentials; degrade to
            # the ceiling where it does not. The response expiry tells the
            # truth either way, and re-mint is one click.
            if code == "ValidationError" and "role chaining" in message and duration > 3600:
                logger.info(
                    "testbed session capped to 1h by role-chaining ceiling "
                    "(policy asked %dh)", hours,
                )
                duration = 3600
                continue
            if code not in ("AccessDenied", "AccessDeniedException") or attempt == 5:
                raise
            logger.info(
                "testbed assume not yet visible in %s (attempt %d) — waiting out "
                "IAM propagation", account_id, attempt + 1,
            )
            time.sleep(4)
    raise RuntimeError("unreachable")  # loop always returns or raises


async def _console_signin_url(creds: dict) -> str | None:
    """Federated console URL for the vended session (best-effort — the
    credentials are the product; the link is convenience)."""
    region = get_settings().aws_region
    session_json = json.dumps(
        {
            "sessionId": creds["AccessKeyId"],
            "sessionKey": creds["SecretAccessKey"],
            "sessionToken": creds["SessionToken"],
        }
    )
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(
                FEDERATION_ENDPOINT,
                params={"Action": "getSigninToken", "Session": session_json},
            )
            resp.raise_for_status()
            token = resp.json()["SigninToken"]
        destination = quote(
            f"https://{region}.console.aws.amazon.com/console/home?region={region}",
            safe="",
        )
        issuer = quote(get_settings().app_base_url, safe="")
        return (
            f"{FEDERATION_ENDPOINT}?Action=login&Issuer={issuer}"
            f"&Destination={destination}&SigninToken={token}"
        )
    except Exception:  # noqa: BLE001 — link is best-effort, creds still vend
        logger.warning("console federation URL failed", exc_info=True)
        return None


async def issue_credentials(db: AsyncSession, project: Project, user: User) -> dict:
    """Mint a limited Testbed session for the project's active deployment.

    Raises TestbedDisabled (policy) or TestbedUnavailable (state) — the
    caller maps to 422/409 and audits the issuance as SECURITY."""
    policies = await resolve_deployment_policies(db)
    if not policies.testbed_enabled:
        raise TestbedDisabled("Testbed access is disabled by platform policy")

    active = (
        await db.execute(
            select(Deployment).where(
                Deployment.project_id == project.id, Deployment.status == "active"
            )
        )
    ).scalar_one_or_none()
    if active is None:
        raise TestbedUnavailable("No active deployment to open a Testbed session on")
    if active.mode != "testbed":
        raise TestbedUnavailable(
            "This deployment runs in Full Governance mode — the platform holds "
            "sole custody of its Enclave. Deploy in Testbed mode to self-configure."
        )
    lease = await db.get(Lease, active.lease_id) if active.lease_id else None
    if lease is None or not lease.aws_account_id:
        raise TestbedUnavailable("Active deployment has no leased account")
    if (lease.provider or "") != "isb":
        # The direct provider deploys into the PLATFORM account. Never vend.
        raise TestbedUnavailable(
            "Testbed access requires a pooled sandbox account (ISB) — this "
            "deployment's provider does not vend user credentials"
        )

    provider = provider_for_lease(lease)
    deploy_session = await asyncio.to_thread(
        provider.deployment_session,
        LeaseInfo(
            external_lease_id=lease.external_lease_id,
            aws_account_id=lease.aws_account_id,
            status="active",
        ),
    )
    trust_arn = await asyncio.to_thread(_platform_principal_arn)
    try:
        await asyncio.to_thread(_bootstrap_role, deploy_session, trust_arn)
    except Exception as exc:  # noqa: BLE001 — name the blocked step
        logger.warning(
            "testbed role bootstrap failed in %s", lease.aws_account_id, exc_info=True
        )
        raise TestbedUnavailable(
            f"Testbed role bootstrap failed in account {lease.aws_account_id}: {exc}"
        ) from exc

    session_name = f"marshal-{str(user.id)[:8]}"
    try:
        creds = await asyncio.to_thread(
            _assume_testbed_role,
            lease.aws_account_id,
            session_name,
            policies.testbed_session_hours,
        )
    except Exception as exc:  # noqa: BLE001
        raise TestbedUnavailable(
            f"Credential mint failed (role exists; IAM propagation can take a "
            f"few seconds — retry): {exc}"
        ) from exc

    console_url = await _console_signin_url(creds)
    return {
        "deployment_id": str(active.id),
        "account_id": lease.aws_account_id,
        "session_name": session_name,
        "access_key_id": creds["AccessKeyId"],
        "secret_access_key": creds["SecretAccessKey"],
        "session_token": creds["SessionToken"],
        "expires_at": creds["Expiration"].isoformat()
        if hasattr(creds["Expiration"], "isoformat")
        else str(creds["Expiration"]),
        "region": get_settings().aws_region,
        "console_url": console_url,
    }
