"""Fetch a real Cognito access token for a seeded demo user (smoke tests).

Usage: cd backend && uv run python ../scripts/get_token.py power@marshal.demo
Requires DEMO_USER_PASSWORD in the environment. Prints the access token.
"""

import base64
import hashlib
import hmac
import os
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "backend"))
from app.core.config import get_settings  # noqa: E402


def secret_hash(username: str, client_id: str, client_secret: str) -> str:
    digest = hmac.new(
        client_secret.encode(), (username + client_id).encode(), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode()


def main() -> None:
    username = sys.argv[1] if len(sys.argv) > 1 else "power@marshal.demo"
    password = os.environ["DEMO_USER_PASSWORD"]
    settings = get_settings()
    cognito = boto3.client("cognito-idp", region_name=settings.aws_region)
    client_secret = cognito.describe_user_pool_client(
        UserPoolId=settings.cognito_user_pool_id, ClientId=settings.cognito_client_id
    )["UserPoolClient"]["ClientSecret"]
    response = cognito.initiate_auth(
        ClientId=settings.cognito_client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": username,
            "PASSWORD": password,
            "SECRET_HASH": secret_hash(username, settings.cognito_client_id, client_secret),
        },
    )
    print(response["AuthenticationResult"]["AccessToken"])


if __name__ == "__main__":
    main()
