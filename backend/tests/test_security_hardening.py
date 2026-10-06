"""S14 security hardening: admin MFA gate, PII guardrail wiring, audit
retention (archive-then-prune), audit export, prompt-injection resistance."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.core.auth import TokenClaims
from app.models import AuditLog, PlatformSettings
from app.services import platform_settings as settings_svc

pytestmark = pytest.mark.asyncio


def _claims(amr: list[str], role: str = "admin") -> TokenClaims:
    return TokenClaims(
        sub="sub-1", username="u", groups=[role], role=role, amr=amr, raw={"sub": "sub-1"}
    )


# ------------------------------------------------------------------ MFA gate


async def test_mfa_satisfied_recognises_cognito_variants():
    assert _claims(["pwd", "mfa"]).mfa_satisfied
    assert _claims(["pwd", "swmfa"]).mfa_satisfied
    assert _claims(["software_token_mfa"]).mfa_satisfied
    assert _claims(["pwd", "SMS_MFA"]).mfa_satisfied
    assert not _claims(["pwd"]).mfa_satisfied
    assert not _claims([]).mfa_satisfied


async def _set_security(db, **flags) -> None:
    row = await db.get(PlatformSettings, 1)
    if row is None:
        row = PlatformSettings(
            id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={}
        )
        db.add(row)
    row.security = flags
    await db.commit()
    settings_svc.invalidate_cache()


async def test_admin_route_allowed_without_mfa_when_policy_off(
    admin_user, client_for, db_session, audit_db
):
    await _set_security(db_session, admin_mfa_required=False)
    async with client_for(admin_user) as ac:
        # The test client's auth override sets no auth_claims — the gate must
        # not block when it cannot see a token (fail-open on unknown, closed
        # only on a KNOWN non-MFA session with the policy ON).
        assert (await ac.get("/api/v1/admin/alerts")).status_code == 200


async def test_admin_mfa_enforced_when_policy_on(admin_user, client_for, db_session, audit_db):
    from app.core import auth as auth_module

    await _set_security(db_session, admin_mfa_required=True)
    async with client_for(admin_user) as ac:
        # Simulate a password-only admin session
        app = ac._transport.app  # noqa: SLF001 — test seam

        from fastapi import Request

        async def override(request: Request):
            request.state.auth_claims = _claims(["pwd"])
            request.state.user = admin_user
            return admin_user

        app.dependency_overrides[auth_module.get_current_user] = override
        resp = await ac.get("/api/v1/admin/alerts")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "admin_mfa_required"

        # Same policy, MFA-backed session → allowed
        async def override_mfa(request: Request):
            request.state.auth_claims = _claims(["pwd", "swmfa"])
            request.state.user = admin_user
            return admin_user

        app.dependency_overrides[auth_module.get_current_user] = override_mfa
        assert (await ac.get("/api/v1/admin/alerts")).status_code == 200
    settings_svc.invalidate_cache()


async def test_security_settings_validation(admin_user, client_for, db_session, audit_db):
    db_session.add(
        PlatformSettings(id=1, model_allowlist=["m"], param_bounds={}, rate_limits={}, cost={})
    )
    await db_session.commit()
    async with client_for(admin_user) as ac:
        current = (await ac.get("/api/v1/admin/model-controls")).json()
        body = {
            "model_allowlist": ["us.anthropic.claude-sonnet-5"],
            "param_bounds": current["param_bounds"],
            "rate_limits": current["rate_limits"],
            "cost": current["cost"],
        }
        ok = await ac.put(
            "/api/v1/admin/model-controls",
            json={**body, "security": {"admin_mfa_required": True, "pii_redaction": True}},
        )
        assert ok.status_code == 200
        assert ok.json()["security"]["pii_redaction"] is True

        # The preceding write activated mandatory admin MFA. Continue the
        # validation checks with an MFA-backed request rather than relying on
        # the test client's claim-less shortcut (production now fails closed
        # when claims are absent).
        from fastapi import Request

        from app.core import auth as auth_module

        async def override_mfa(request: Request):
            request.state.auth_claims = _claims(["pwd", "swmfa"])
            request.state.user = admin_user
            return admin_user

        app = ac._transport.app  # noqa: SLF001 — test seam
        app.dependency_overrides[auth_module.get_current_user] = override_mfa

        bad_key = await ac.put(
            "/api/v1/admin/model-controls", json={**body, "security": {"mfa": True}}
        )
        assert bad_key.status_code == 422
        bad_type = await ac.put(
            "/api/v1/admin/model-controls",
            json={**body, "security": {"admin_mfa_required": "yes"}},
        )
        assert bad_type.status_code == 422
    settings_svc.invalidate_cache()


# ------------------------------------------------------------ PII guardrail


async def test_guardrail_config_off_by_default(db_session, audit_db, monkeypatch):
    from app.core.config import get_settings
    from app.services.bedrock import guardrail_config

    get_settings.cache_clear()
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
    get_settings.cache_clear()
    await _set_security(db_session, pii_redaction=False)
    assert await guardrail_config(streaming=False) == {}
    get_settings.cache_clear()


async def test_guardrail_config_attaches_when_enabled(db_session, audit_db, monkeypatch):
    from app.core.config import get_settings
    from app.services.bedrock import guardrail_config

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "1")
    get_settings.cache_clear()
    await _set_security(db_session, pii_redaction=True)

    non_stream = await guardrail_config(streaming=False)
    assert non_stream["guardrailConfig"]["guardrailIdentifier"] == "gr-123"
    assert non_stream["guardrailConfig"]["guardrailVersion"] == "1"
    assert "streamProcessingMode" not in non_stream["guardrailConfig"]

    # Streaming must be synchronous: async mode would let un-redacted tokens
    # reach the client before the guardrail verdict.
    streaming = await guardrail_config(streaming=True)
    assert streaming["guardrailConfig"]["streamProcessingMode"] == "sync"
    get_settings.cache_clear()
    settings_svc.invalidate_cache()


async def test_guardrail_scope_non_interactive_exempts_chat_only(
    db_session, audit_db, monkeypatch
):
    """S14-04 middle path: scope=non_interactive masks everything except live
    chat; unknown purpose fails toward masking."""
    from app.core.config import get_settings
    from app.services.bedrock import guardrail_config

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-123")
    get_settings.cache_clear()
    await _set_security(
        db_session, pii_redaction=True, pii_redaction_scope="non_interactive"
    )
    assert await guardrail_config(streaming=True, purpose="chat") == {}
    for purpose in ("requirements", "design", "title", "classification", None):
        cfg = await guardrail_config(streaming=False, purpose=purpose)
        assert cfg["guardrailConfig"]["guardrailIdentifier"] == "gr-123", purpose

    # scope=all (and the default when scope is absent) masks chat too
    await _set_security(db_session, pii_redaction=True, pii_redaction_scope="all")
    assert (await guardrail_config(streaming=True, purpose="chat"))["guardrailConfig"]
    await _set_security(db_session, pii_redaction=True)
    assert (await guardrail_config(streaming=True, purpose="chat"))["guardrailConfig"]
    get_settings.cache_clear()
    settings_svc.invalidate_cache()


async def test_redaction_scope_validation(db_session, audit_db, admin_user, client_for):
    """PUT model-controls rejects unknown scope values, accepts valid ones."""
    await _set_security(db_session)  # seeds the settings row
    async with client_for(admin_user) as ac:
        current = (await ac.get("/api/v1/admin/model-controls")).json()
        payload = {
            "model_allowlist": ["us.anthropic.claude-sonnet-5"],
            "param_bounds": current["param_bounds"],
            "rate_limits": current["rate_limits"],
            "cost": current["cost"],
            "security": {"pii_redaction": True, "pii_redaction_scope": "sometimes"},
        }
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 422 and "pii_redaction_scope" in r.json()["detail"]
        payload["security"]["pii_redaction_scope"] = "non_interactive"
        r = await ac.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 200
        assert r.json()["security"]["pii_redaction_scope"] == "non_interactive"


async def test_external_prepare_honors_scope(db_session, audit_db, monkeypatch):
    """S17 pre-egress redaction follows the same scope rule (one source)."""
    from app.services.model_providers import openai_compat

    async def fake_endpoint_config(slug):
        return {"enabled": True, "base_url": "https://x", "model_name": "m", "label": slug}

    async def no_key(slug):
        return None

    monkeypatch.setattr(
        "app.services.model_endpoints.endpoint_config", fake_endpoint_config
    )
    monkeypatch.setattr("app.services.model_endpoints.api_key_for", no_key)
    await _set_security(
        db_session, pii_redaction=True, pii_redaction_scope="non_interactive"
    )
    msgs = [{"role": "user", "content": [{"text": "card 4111 1111 1111 1111"}]}]
    # chat: exempt — the card number survives to egress prep
    _, _, out_msgs, _, _ = await openai_compat._prepare("ext/x", msgs, "sys", purpose="chat")
    assert "4111 1111 1111 1111" in out_msgs[0]["content"][0]["text"]
    # non-interactive purpose: masked before egress
    _, _, out_msgs, _, _ = await openai_compat._prepare(
        "ext/x", msgs, "sys", purpose="requirements"
    )
    assert "4111 1111 1111 1111" not in out_msgs[0]["content"][0]["text"]
    assert "[REDACTED:CARD_NUMBER]" in out_msgs[0]["content"][0]["text"]
    settings_svc.invalidate_cache()


async def test_guardrail_absent_without_configured_id(db_session, audit_db, monkeypatch):
    from app.core.config import get_settings
    from app.services.bedrock import guardrail_config

    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "")
    get_settings.cache_clear()
    await _set_security(db_session, pii_redaction=True)
    assert await guardrail_config(streaming=False) == {}
    get_settings.cache_clear()


# ------------------------------------------------------- audit retention


class _FakeS3:
    def __init__(self):
        self.objects: list[dict] = []

    def put_object(self, **kwargs):
        self.objects.append(kwargs)
        return {}


@pytest.fixture
def fake_s3(monkeypatch, db_engine):
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from app.core.config import get_settings

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.core.db.SessionLocal", maker)
    monkeypatch.setenv("CODEGEN_WORKSPACE_BUCKET", "marshal-test-bucket")
    get_settings.cache_clear()
    fake = _FakeS3()
    monkeypatch.setattr("boto3.client", lambda *a, **k: fake)
    yield fake
    get_settings.cache_clear()


async def _audit_row(db, *, days_old: int, archived=False) -> AuditLog:
    row = AuditLog(
        actor_id=None,
        category="user",
        action="test.action",
        created_at=datetime.now(UTC) - timedelta(days=days_old),
        archived_at=datetime.now(UTC) if archived else None,
        detail={"n": days_old},
    )
    db.add(row)
    await db.commit()
    return row


async def test_retention_archives_then_prunes(db_session, fake_s3):
    from app.services.audit_retention import archive_tick

    old = await _audit_row(db_session, days_old=200)
    recent = await _audit_row(db_session, days_old=5)

    first = await archive_tick()  # archives, does not yet prune this row
    assert first["archived"] == 1
    assert fake_s3.objects and fake_s3.objects[0]["Bucket"] == "marshal-test-bucket"
    assert b"test.action" in fake_s3.objects[0]["Body"]
    assert fake_s3.objects[0]["Key"].startswith("audit-archive/")

    second = await archive_tick()  # nothing new to archive; prunes the marked row
    assert second["archived"] == 0
    remaining = (await db_session.execute(select(AuditLog.id))).scalars().all()
    assert old.id not in remaining and recent.id in remaining


async def test_retention_never_prunes_unarchived_rows(db_session, fake_s3, monkeypatch):
    """The load-bearing invariant: an S3 failure must not cost audit evidence."""
    from app.services import audit_retention

    row = await _audit_row(db_session, days_old=300)

    def explode(**_kwargs):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(fake_s3, "put_object", explode)
    with pytest.raises(RuntimeError):
        await audit_retention.archive_tick()

    still_there = (await db_session.execute(select(AuditLog.id))).scalars().all()
    assert row.id in still_there
    fresh = await db_session.get(AuditLog, row.id)
    assert fresh.archived_at is None


async def test_retention_leaves_hot_window_alone(db_session, fake_s3):
    from app.services.audit_retention import archive_tick

    await _audit_row(db_session, days_old=179)
    result = await archive_tick()
    assert result == {"archived": 0, "pruned": 0}
    assert fake_s3.objects == []


# --------------------------------------------------------------- export


async def test_audit_export_supports_ndjson_and_csv(
    admin_user, client_for, db_session, audit_db
):
    """S14-06 added a machine-readable format to the EXISTING export endpoint
    rather than a second one, so both formats share filters and caps."""
    import json

    db_session.add(
        AuditLog(
            actor_id=admin_user.id, category="admin", action="model_controls_updated",
            resource_type="settings", http_status=200, detail={"k": "v"},
        )
    )
    await db_session.commit()
    async with client_for(admin_user) as ac:
        nd = await ac.post("/api/v1/admin/audit-logs/export?format=ndjson")
        csv_resp = await ac.post("/api/v1/admin/audit-logs/export")

    assert nd.status_code == 200
    assert nd.headers["content-type"].startswith("application/x-ndjson")
    assert ".jsonl" in nd.headers["content-disposition"]
    rows = [json.loads(line) for line in nd.text.strip().split("\n") if line]
    assert any(r.get("action") == "model_controls_updated" for r in rows)

    assert csv_resp.status_code == 200
    assert csv_resp.headers["content-type"].startswith("text/csv")
    assert ".csv" in csv_resp.headers["content-disposition"]
    assert "model_controls_updated" in csv_resp.text


# ------------------------------------------- codegen prompt-injection (S14-05)


async def test_generated_template_with_injected_resources_is_rejected():
    """Spec text is untrusted input. Even if a model is steered into emitting
    privileged resources, the S8 validation gate must refuse the build."""
    import json

    from app.services.codegen.validate import validate_artifacts

    hostile = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Resources": {
            # Attempt at persistence + privilege escalation via generated IaC
            "Backdoor": {
                "Type": "AWS::IAM::User",
                "Properties": {"UserName": "attacker"},
            },
            "OpenBucket": {
                "Type": "AWS::S3::Bucket",
                "Properties": {"AccessControl": "PublicReadWrite"},
            },
        },
        "Outputs": {"ApiUrl": {"Value": "http://example.com"}},
    }
    findings, _unenforceable = validate_artifacts(
        {"template.json": json.dumps(hostile)},
        ["hardcoded_credentials", "public_internet_access_to_db"],
    )
    assert findings, "hostile template must produce findings"
    blob = " ".join(f"{f.check} {f.message}" for f in findings).lower()
    assert "iam::user" in blob or "not allowed" in blob or "allowlist" in blob, blob


# ------------------------------------------------- view-only admin (viewers)
# Owner decision (4 Sep 2026): flagged human accounts read every admin
# surface under the admin-MFA bar; they never mutate, and semantically-read
# POSTs (exports, test-sends, probes) stay admin-only via the method rule.


async def test_viewer_reads_admin_surfaces_but_never_mutates(
    business_user, client_for, db_session, audit_db
):
    await _set_security(db_session, admin_mfa_required=False)
    business_user.admin_readonly = True
    await db_session.commit()
    async with client_for(business_user) as ac:
        assert (await ac.get("/api/v1/admin/alerts")).status_code == 200
        assert (await ac.get("/api/v1/admin/users")).status_code == 200
        assert (await ac.get("/api/v1/admin/audit-logs")).status_code == 200

        put = await ac.put("/api/v1/admin/model-controls", json={})
        assert put.status_code == 403
        assert put.json()["detail"]["code"] == "admin_read_only"

        export = await ac.post("/api/v1/admin/audit-logs/export")
        assert export.status_code == 403
        assert export.json()["detail"]["code"] == "admin_read_only"
    settings_svc.invalidate_cache()


async def test_unflagged_user_still_blocked_from_admin_reads(
    business_user, client_for, db_session, audit_db
):
    await _set_security(db_session, admin_mfa_required=False)
    async with client_for(business_user) as ac:
        resp = await ac.get("/api/v1/admin/alerts")
        assert resp.status_code == 403
        assert "admin role" in resp.json()["detail"]
    settings_svc.invalidate_cache()


async def test_viewer_held_to_admin_mfa_bar(
    business_user, client_for, db_session, audit_db
):
    from fastapi import Request

    from app.core import auth as auth_module

    await _set_security(db_session, admin_mfa_required=True)
    business_user.admin_readonly = True
    await db_session.commit()
    async with client_for(business_user) as ac:
        app = ac._transport.app  # noqa: SLF001 — test seam

        async def override_pwd(request: Request):
            request.state.auth_claims = _claims(["pwd"], role="business")
            request.state.user = business_user
            return business_user

        app.dependency_overrides[auth_module.get_current_user] = override_pwd
        resp = await ac.get("/api/v1/admin/alerts")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "admin_mfa_required"

        async def override_mfa(request: Request):
            request.state.auth_claims = _claims(["pwd", "swmfa"], role="business")
            request.state.user = business_user
            return business_user

        app.dependency_overrides[auth_module.get_current_user] = override_mfa
        assert (await ac.get("/api/v1/admin/alerts")).status_code == 200
    settings_svc.invalidate_cache()


async def test_admin_manages_viewer_flag(
    admin_user, business_user, client_for, db_session, audit_db
):
    await _set_security(db_session, admin_mfa_required=False)
    async with client_for(admin_user) as ac:
        on = await ac.put(
            f"/api/v1/admin/users/{business_user.id}", json={"admin_readonly": True}
        )
        assert on.status_code == 200
        assert on.json()["admin_readonly"] is True

        off = await ac.put(
            f"/api/v1/admin/users/{business_user.id}", json={"admin_readonly": False}
        )
        assert off.status_code == 200
        assert off.json()["admin_readonly"] is False
    settings_svc.invalidate_cache()


# ------------------------------------------- S14-02 fix: temporal MFA seam
# Cognito access tokens carry no amr claim (FSD §13.5M), so the gate compares
# the session's auth_time with users.mfa_enrolled_at. The amr scan survives
# only as a forward-compatible disjunct.


def _timed_claims(auth_time, role: str = "admin", amr: list[str] | None = None) -> TokenClaims:
    raw: dict = {"sub": "sub-1"}
    if auth_time is not None:
        raw["auth_time"] = auth_time
    return TokenClaims(
        sub="sub-1", username="u", groups=[role], role=role, amr=amr or ["pwd"], raw=raw
    )


async def test_session_mfa_satisfied_temporal_seam(admin_user, db_session):
    from app.core.auth import session_mfa_satisfied

    enrolled = datetime.now(UTC) - timedelta(minutes=10)
    admin_user.mfa_enrolled_at = enrolled
    await db_session.commit()

    after = int((enrolled + timedelta(minutes=5)).timestamp())
    before = int((enrolled - timedelta(minutes=5)).timestamp())

    # Session authenticated AFTER enrollment → necessarily passed the challenge
    assert session_mfa_satisfied(_timed_claims(after), admin_user)
    # Boundary: authenticated at the enrollment instant counts (>=)
    assert session_mfa_satisfied(_timed_claims(int(enrolled.timestamp())), admin_user)
    # Pre-enrollment session → password-only, refused
    assert not session_mfa_satisfied(_timed_claims(before), admin_user)
    # Missing/absent signals fail closed
    assert not session_mfa_satisfied(_timed_claims(None), admin_user)
    assert not session_mfa_satisfied(None, admin_user)
    assert not session_mfa_satisfied(_timed_claims(after), None)

    # Never-enrolled user → always False regardless of auth_time
    admin_user.mfa_enrolled_at = None
    await db_session.commit()
    assert not session_mfa_satisfied(_timed_claims(after), admin_user)
    # amr disjunct still honored if Cognito ever emits markers
    assert session_mfa_satisfied(_timed_claims(after, amr=["pwd", "swmfa"]), admin_user)


async def test_admin_gate_uses_enrollment_timestamp(admin_user, client_for, db_session, audit_db):
    """Policy ON: post-enrollment session passes, pre-enrollment session 403s —
    no amr claim involved (production shape)."""
    from fastapi import Request

    from app.core import auth as auth_module

    await _set_security(db_session, admin_mfa_required=True)
    enrolled = datetime.now(UTC) - timedelta(minutes=10)
    admin_user.mfa_enrolled_at = enrolled
    await db_session.commit()

    async with client_for(admin_user) as ac:
        app = ac._transport.app  # noqa: SLF001 — test seam

        async def override_fresh(request: Request):
            request.state.auth_claims = _timed_claims(
                int((enrolled + timedelta(minutes=2)).timestamp())
            )
            request.state.user = admin_user
            return admin_user

        app.dependency_overrides[auth_module.get_current_user] = override_fresh
        assert (await ac.get("/api/v1/admin/alerts")).status_code == 200

        async def override_stale(request: Request):
            request.state.auth_claims = _timed_claims(
                int((enrolled - timedelta(minutes=2)).timestamp())
            )
            request.state.user = admin_user
            return admin_user

        app.dependency_overrides[auth_module.get_current_user] = override_stale
        resp = await ac.get("/api/v1/admin/alerts")
        assert resp.status_code == 403
        assert resp.json()["detail"]["code"] == "admin_mfa_required"
    settings_svc.invalidate_cache()


async def test_mfa_confirm_records_and_disable_clears_timestamp(
    admin_user, client_for, db_session, audit_db, monkeypatch
):
    from app.services import mfa as mfa_svc

    monkeypatch.setattr(mfa_svc, "confirm_enrollment", lambda token, code: None)
    monkeypatch.setattr(mfa_svc, "disable", lambda token: None)
    await _set_security(db_session, admin_mfa_required=False)

    headers = {"authorization": "Bearer test-access-token"}
    async with client_for(admin_user) as ac:
        assert admin_user.mfa_enrolled_at is None
        ok = await ac.post(
            "/api/v1/users/me/mfa/confirm", json={"code": "123456"}, headers=headers
        )
        assert ok.status_code == 200
        await db_session.refresh(admin_user)
        assert admin_user.mfa_enrolled_at is not None

        gone = await ac.delete("/api/v1/users/me/mfa", headers=headers)
        assert gone.status_code == 200
        await db_session.refresh(admin_user)
        assert admin_user.mfa_enrolled_at is None
