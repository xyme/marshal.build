"""Notifications: prefs matrix, dedupe, suppression, scoping (notifications spec)."""


import pytest
from sqlalchemy import select

from app.models import Notification
from app.services import notifications as svc

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def email_off(monkeypatch):
    """Default: email globally disabled (as deployed until owner action)."""

    class S:
        email_enabled = False
        email_from = "t@marshal.build"
        app_base_url = "https://app.test"
        aws_region = "us-east-1"

    monkeypatch.setattr(svc, "get_settings", lambda: S())
    return S


async def test_defaults_follow_fsd_matrix(db_session, test_user):
    assert svc.effective_prefs(test_user, "generation_complete") == {"in_app": True, "email": False}
    assert svc.effective_prefs(test_user, "deploy_failed") == {"in_app": True, "email": True}
    assert svc.effective_prefs(test_user, "sample_published") == {"in_app": True, "email": False}


async def test_overrides_and_mute(db_session, test_user):
    test_user.notification_prefs = {"generation_complete": {"in_app": False, "email": False}}
    await db_session.commit()
    row = await svc.notify(
        db_session, test_user, type="generation_complete", title="t", body="b"
    )
    assert row is None  # fully muted
    rows = (await db_session.execute(select(Notification))).scalars().all()
    assert rows == []


async def test_forced_in_app_for_governance_events(db_session, test_user):
    test_user.notification_prefs = {"risk_review_requested": {"in_app": False, "email": False}}
    await db_session.commit()
    row = await svc.notify(
        db_session, test_user, type="risk_review_requested", title="review", body="b"
    )
    assert row is not None  # FORCED_IN_APP wins


async def test_dedupe_key_suppresses_duplicates(db_session, test_user):
    first = await svc.notify(
        db_session, test_user, type="cost_threshold", title="75%", dedupe_key="cost:u:75:2026-07"
    )
    second = await svc.notify(
        db_session, test_user, type="cost_threshold", title="75%", dedupe_key="cost:u:75:2026-07"
    )
    assert first is not None and second is None
    count = len((await db_session.execute(select(Notification))).scalars().all())
    assert count == 1


async def test_email_suppression_rules(db_session, test_user, monkeypatch):
    class S:
        email_enabled = True
        email_from = "t@marshal.build"
        app_base_url = "https://app.test"
        aws_region = "us-east-1"

    monkeypatch.setattr(svc, "get_settings", lambda: S())
    sent: list[str] = []
    monkeypatch.setattr(svc, "_send_email", lambda to, *a, **k: (sent.append(to), "sent")[1])

    # demo mailbox → skipped even with email enabled + pref on
    row = await svc.notify(db_session, test_user, type="deploy_failed", title="t", body="b")
    assert row.email_status == "skipped" and sent == []

    # real mailbox → sent
    test_user.email = "real@example.com"
    await db_session.commit()
    row = await svc.notify(db_session, test_user, type="deploy_failed", title="t2", body="b")
    assert row.email_status == "sent" and sent == ["real@example.com"]

    # suspended → skipped
    test_user.status = "suspended"
    await db_session.commit()
    row = await svc.notify(db_session, test_user, type="deploy_failed", title="t3", body="b")
    assert row.email_status == "skipped"


async def test_api_scoping_and_read_flow(client, client_for, db_session, test_user, business_user, audit_db):
    await svc.notify(db_session, test_user, type="generation_complete", title="mine")
    await svc.notify(db_session, business_user, type="generation_complete", title="theirs")

    listed = (await client.get("/api/v1/notifications")).json()
    assert listed["total"] == 1 and listed["items"][0]["title"] == "mine"

    count = (await client.get("/api/v1/notifications/unread-count")).json()
    assert count["unread"] == 1

    # Cannot read someone else's notification
    theirs = (
        await db_session.execute(
            select(Notification).where(Notification.user_id == business_user.id)
        )
    ).scalar_one()
    assert (await client.post(f"/api/v1/notifications/{theirs.id}/read")).status_code == 404

    mine = listed["items"][0]["id"]
    read = (await client.post(f"/api/v1/notifications/{mine}/read")).json()
    assert read["read_at"] is not None
    assert (await client.get("/api/v1/notifications/unread-count")).json()["unread"] == 0

    await svc.notify(db_session, test_user, type="deploy_failed", title="x")
    await svc.notify(db_session, test_user, type="deploy_failed", title="y")
    marked = (await client.post("/api/v1/notifications/read-all")).json()
    assert marked["marked"] == 2


async def test_group_filter(client, db_session, test_user, audit_db):
    await svc.notify(db_session, test_user, type="deploy_failed", title="d")
    await svc.notify(db_session, test_user, type="cost_threshold", title="c")
    cost_only = (await client.get("/api/v1/notifications?group=cost")).json()
    assert cost_only["total"] == 1 and cost_only["items"][0]["type"] == "cost_threshold"


async def test_prefs_endpoint_validation_and_echo(client, audit_db):
    bad = await client.put(
        "/api/v1/users/me/notifications", json={"nonsense_event": {"in_app": True}}
    )
    assert bad.status_code == 422
    good = await client.put(
        "/api/v1/users/me/notifications",
        json={"generation_complete": {"in_app": True, "email": True}},
    )
    assert good.status_code == 200
    body = good.json()
    assert body["prefs"]["generation_complete"] == {"in_app": True, "email": True}
    assert body["email_enabled"] is False


async def test_notify_many_skips_suspended(db_session, test_user, business_user, admin_user):
    business_user.status = "suspended"
    await db_session.commit()
    count = await svc.notify_many(
        db_session, [test_user.id, business_user.id, admin_user.id],
        type="risk_review_requested", title="review please",
    )
    assert count == 2


# ------------------------------------------------- S15-07 bounce/complaint


def _client_error(code: str, message: str):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": message}}, "SendEmail")


def test_send_classifies_suppressed_destination(monkeypatch):
    """A bounce/complaint suppression reject is 'suppressed', not 'failed' —
    the row must record WHY the mail never arrived (S15-07)."""

    class FakeSES:
        def send_email(self, **_kwargs):
            raise _client_error(
                "MessageRejected",
                "Email address is on the account-level suppression list",
            )

    monkeypatch.setattr(svc, "_ses_client", lambda: FakeSES())
    assert svc._send_email("bounced@example.com", "t", "b", None) == "suppressed"


def test_send_classifies_transient_failure(monkeypatch):
    class FakeSES:
        def send_email(self, **_kwargs):
            raise _client_error("ThrottlingException", "Rate exceeded")

    monkeypatch.setattr(svc, "_ses_client", lambda: FakeSES())
    assert svc._send_email("x@example.com", "t", "b", None) == "failed"


def test_email_channel_status_reports_flag_and_suppressions(monkeypatch, email_off):
    """Admin status: flag state + SES account + suppression list summary."""
    from datetime import UTC, datetime

    class FakeSES:
        def get_account(self):
            return {
                "ProductionAccessEnabled": False,
                "SendingEnabled": True,
                "SendQuota": {"Max24HourSend": 200.0, "SentLast24Hours": 3.0},
                "SuppressionAttributes": {"SuppressedReasons": ["BOUNCE", "COMPLAINT"]},
            }

        def get_email_identity(self, EmailIdentity):  # noqa: N803 — boto casing
            return {
                "VerifiedForSendingStatus": False,
                "DkimAttributes": {"Status": "PENDING"},
            }

        def list_suppressed_destinations(self, PageSize):  # noqa: N803
            return {
                "SuppressedDestinationSummaries": [
                    {"EmailAddress": "gone@example.com", "Reason": "BOUNCE",
                     "LastUpdateTime": datetime.now(UTC)},
                ]
            }

    monkeypatch.setattr(svc, "_ses_client", lambda: FakeSES())
    status = svc.email_channel_status()

    assert status["email_enabled"] is False
    assert status["production_access"] is False
    assert status["suppression_reasons"] == ["BOUNCE", "COMPLAINT"]
    assert status["identity"] == {
        "domain": "marshal.build", "verified": False, "dkim_status": "PENDING",
    }
    assert status["suppressed"]["count"] == 1
    assert status["suppressed"]["recent"][0]["email"] == "gone@example.com"
