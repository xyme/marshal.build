"""JWT validation unit tests (foundation spec R4) — local RSA keys, no network."""

import time
from unittest.mock import patch

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

from app.core import auth as auth_module
from app.core.auth import resolve_role, validate_token

KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)

POOL_ID = "us-east-1_TESTPOOL"
CLIENT_ID = "testclient123"
ISSUER = f"https://cognito-idp.us-east-1.amazonaws.com/{POOL_ID}"


def make_token(
    *,
    key=KEY,
    client_id: str = CLIENT_ID,
    token_use: str = "access",
    groups: list[str] | None = None,
    exp_delta: int = 3600,
) -> str:
    claims = {
        "sub": "user-sub-1",
        "iss": ISSUER,
        "client_id": client_id,
        "token_use": token_use,
        "username": "tester",
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
    }
    if groups is not None:
        claims["cognito:groups"] = groups
    return pyjwt.encode(claims, key, algorithm="RS256")


class FakeSigningKey:
    def __init__(self, key):
        self.key = key.public_key()


@pytest.fixture(autouse=True)
def patched_env():
    with (
        patch.object(auth_module, "_get_jwks_client") as mock_jwks,
        patch("app.core.auth.get_settings") as mock_settings,
    ):
        mock_jwks.return_value.get_signing_key_from_jwt.return_value = FakeSigningKey(KEY)
        mock_settings.return_value.cognito_user_pool_id = POOL_ID
        mock_settings.return_value.cognito_client_id = CLIENT_ID
        mock_settings.return_value.cognito_issuer = ISSUER
        yield


def test_valid_token_resolves_claims_and_role():
    claims = validate_token(make_token(groups=["power"]))
    assert claims.sub == "user-sub-1"
    assert claims.role == "power"


def test_expired_token_rejected():
    with pytest.raises(HTTPException) as excinfo:
        validate_token(make_token(exp_delta=-100))
    assert excinfo.value.status_code == 401


def test_wrong_signature_rejected():
    with pytest.raises(HTTPException):
        validate_token(make_token(key=OTHER_KEY))


def test_wrong_client_id_rejected():
    with pytest.raises(HTTPException):
        validate_token(make_token(client_id="other-client"))


def test_id_token_rejected():
    with pytest.raises(HTTPException):
        validate_token(make_token(token_use="id"))


def test_no_groups_defaults_to_business():
    claims = validate_token(make_token(groups=None))
    assert claims.role == "business"


def test_role_precedence_admin_wins():
    assert resolve_role(["business", "admin", "power"]) == "admin"
    assert resolve_role(["business", "power"]) == "power"
    assert resolve_role([]) == "business"


# ---------------------------------------------------------- S15-01 federation


def test_provider_from_username_detects_known_idps():
    from app.core.auth import provider_from_username

    assert provider_from_username("entraid_abc-123") == "entraid"
    assert provider_from_username("Okta_00u8xyz") == "okta"  # case-insensitive
    assert provider_from_username("tester") == "native"
    assert provider_from_username("power@marshal.demo") == "native"
    assert provider_from_username("") == "native"


def test_federated_role_flows_through_group_claims():
    """The pre-token trigger writes mapped IdP roles into cognito:groups —
    the backend's resolution path is identical for federated users."""
    claims = validate_token(make_token(groups=["power"]))
    assert claims.role == "power"  # no federation-specific branch to diverge
