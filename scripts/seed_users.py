"""Seed internal demo users into the Cognito pool (foundation spec R5).

Idempotent: safe to run repeatedly. Password comes from DEMO_USER_PASSWORD.
Usage:  cd backend && uv run python ../scripts/seed_users.py

The presenter account is seeded only into Cognito's power group. Its local
`account_class=demo` classification is intentionally applied by an admin only
after ordinary JIT provisioning; this script never mutates application users.
"""

import os
import sys

import boto3
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.core.config import get_settings  # noqa: E402

DEMO_USERS = [
    {"email": "business@marshal.demo", "name": "Bella Business", "group": "business"},
    {"email": "power@marshal.demo", "name": "Priya Power", "group": "power"},
    {"email": "demo@marshal.demo", "name": "Marshal Demo Presenter", "group": "power"},
    {"email": "admin@marshal.demo", "name": "Ade Admin", "group": "admin"},
]
ROLE_GROUPS = ("business", "power", "admin")


def main() -> None:
    settings = get_settings()
    password = os.environ.get("DEMO_USER_PASSWORD")
    if not password:
        sys.exit("Set DEMO_USER_PASSWORD (12+ chars, upper+lower+digit) before seeding.")
    if not settings.cognito_user_pool_id:
        sys.exit("COGNITO_USER_POOL_ID missing — run scripts/sync-env.sh first.")

    cognito = boto3.client("cognito-idp", region_name=settings.aws_region)
    pool_id = settings.cognito_user_pool_id

    for user in DEMO_USERS:
        email = user["email"]
        try:
            cognito.admin_create_user(
                UserPoolId=pool_id,
                Username=email,
                UserAttributes=[
                    {"Name": "email", "Value": email},
                    {"Name": "email_verified", "Value": "true"},
                    {"Name": "name", "Value": user["name"]},
                ],
                MessageAction="SUPPRESS",
            )
            print(f"created {email}")
        except ClientError as exc:
            if exc.response["Error"]["Code"] != "UsernameExistsException":
                raise
            print(f"exists  {email}")

        cognito.admin_set_user_password(
            UserPoolId=pool_id, Username=email, Password=password, Permanent=True
        )
        # Converge seeded identities to exactly one authorization role. Cognito
        # group assignment is additive, and auth resolves admin before power;
        # without removing stale role groups the presenter could retain real
        # admin authority even though this source declares it as power.
        current_groups = {
            group["GroupName"]
            for group in cognito.admin_list_groups_for_user(
                UserPoolId=pool_id, Username=email
            ).get("Groups", [])
            if group.get("GroupName") in ROLE_GROUPS
        }
        role_changed = current_groups != {user["group"]}
        for group in ROLE_GROUPS:
            if group == user["group"]:
                continue
            try:
                cognito.admin_remove_user_from_group(
                    UserPoolId=pool_id, Username=email, GroupName=group
                )
            except ClientError as exc:
                if exc.response["Error"]["Code"] != "ResourceNotFoundException":
                    raise
        cognito.admin_add_user_to_group(
            UserPoolId=pool_id, Username=email, GroupName=user["group"]
        )
        if role_changed:
            # Group changes do not invalidate already-issued access tokens. A
            # stale presenter token carrying `admin` could otherwise restore
            # real authority through the backend's token-derived role sync.
            cognito.admin_user_global_sign_out(UserPoolId=pool_id, Username=email)
        print(
            f"        password set, role group={user['group']}, "
            f"sessions_revoked={role_changed}"
        )

    print("\nSeed complete. Sign in at http://localhost:3000 with any of:")
    for user in DEMO_USERS:
        print(f"  {user['email']}  (role: {user['group']})")


if __name__ == "__main__":
    main()
