"""TOTP enrollment against Cognito (security-hardening spec S14-02).

Cognito's hosted UI cannot self-enrol a software token while the pool's MFA
setting is OPTIONAL, so enrolment runs through the user-context Cognito APIs
with the caller's own access token:

    AssociateSoftwareToken -> (user scans QR) -> VerifySoftwareToken
                           -> SetUserMFAPreference(software token, preferred)

The platform never stores the shared secret: it is handed to the browser once
and lives only in the user's authenticator app.
"""

import logging
from urllib.parse import quote

import boto3
from botocore.exceptions import ClientError

from app.core.config import get_settings

logger = logging.getLogger("marshal.mfa")

ISSUER = "marshal"


class MfaError(Exception):
    """Enrollment failure with a user-safe message (surfaces as 422)."""


def _client():
    return boto3.client("cognito-idp", region_name=get_settings().aws_region)


def _wrap(exc: ClientError) -> MfaError:
    code = exc.response.get("Error", {}).get("Code", "")
    if code == "EnableSoftwareTokenMFAException":
        return MfaError("That code did not match. Try the next code your app shows.")
    if code == "CodeMismatchException":
        return MfaError("That code did not match. Try the next code your app shows.")
    if code == "NotAuthorizedException":
        return MfaError("Your session expired. Sign in again and retry.")
    if code == "TooManyRequestsException":
        return MfaError("Too many attempts. Wait a moment and try again.")
    logger.warning("cognito MFA error %s", code)
    return MfaError("Multi-factor setup is temporarily unavailable.")


def start_enrollment(access_token: str, email: str) -> dict:
    """Associate a software token; returns the secret + an otpauth URI for QR."""
    try:
        secret = _client().associate_software_token(AccessToken=access_token)["SecretCode"]
    except ClientError as exc:
        raise _wrap(exc) from exc
    label = quote(f"{ISSUER}:{email}")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={quote(ISSUER)}&algorithm=SHA1&digits=6&period=30"
    return {"secret": secret, "otpauth_uri": uri}


def confirm_enrollment(access_token: str, code: str) -> None:
    """Verify the first TOTP code, then make the software token the preferred
    factor. Both steps are required: verification alone does not turn MFA on."""
    client = _client()
    try:
        result = client.verify_software_token(
            AccessToken=access_token, UserCode=code, FriendlyDeviceName="Authenticator"
        )
        if result.get("Status") != "SUCCESS":
            raise MfaError("That code did not match. Try the next code your app shows.")
        client.set_user_mfa_preference(
            AccessToken=access_token,
            SoftwareTokenMfaSettings={"Enabled": True, "PreferredMfa": True},
        )
    except ClientError as exc:
        raise _wrap(exc) from exc


def disable(access_token: str) -> None:
    try:
        _client().set_user_mfa_preference(
            AccessToken=access_token,
            SoftwareTokenMfaSettings={"Enabled": False, "PreferredMfa": False},
        )
    except ClientError as exc:
        raise _wrap(exc) from exc


def status(access_token: str) -> dict:
    """Enrolment state straight from Cognito (the platform stores none of it)."""
    try:
        user = _client().get_user(AccessToken=access_token)
    except ClientError as exc:
        raise _wrap(exc) from exc
    factors = user.get("UserMFASettingList") or []
    return {
        "enrolled": "SOFTWARE_TOKEN_MFA" in factors,
        "preferred": user.get("PreferredMfaSetting"),
    }
