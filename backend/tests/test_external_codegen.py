"""External codegen tests (external-codegen spec): contract, lifecycle,
governance, gate parity."""

import asyncio
import hashlib
import json
import re
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models import CodegenBuild, ModelInvocation, Project, Spec, User
from app.services.codegen import contract, runner
from app.services.codegen.workspace_runner import ExternalStatus
from tests.test_codegen import GOOD_ARTIFACTS

pytestmark = pytest.mark.asyncio


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _files_for(artifacts: dict[str, str]) -> list[contract.ResultFile]:
    return [
        contract.ResultFile(path=p, sha256=_sha(c), bytes=len(c.encode()))
        for p, c in sorted(artifacts.items())
    ]


def _results(artifacts: dict[str, str], *, usage=None, status="succeeded", error=None):
    return contract.Results(
        status=status,
        files=_files_for(artifacts) if status == "succeeded" else [],
        usage=usage or [contract.ResultUsage("us.anthropic.claude-sonnet-5", 4000, 2000)],
        engine={"name": "fake-engine", "version": "t"},
        app_name="probe-app",
        architecture_notes="fake",
        error=error,
    )


class FakeWorkspaceProvider:
    """Scriptable external provider: poll() pops from a queue of observations."""

    name = "runner"
    mode = "external"

    def __init__(self, observations=None, artifacts=None):
        self.observations = list(observations or [])
        self.artifacts = artifacts if artifacts is not None else dict(GOOD_ARTIFACTS)
        self.dispatches: list[int] = []
        self.cancelled = False

    async def dispatch(self, ctx, *, slug, artifact_profile, attempt):
        self.dispatches.append(attempt)
        return f"marshal-codegen-runner:job-{attempt}"

    async def poll(self, build_id, attempt):
        if not self.observations:
            return ExternalStatus(started=False, files_seen=[], results=None)
        item = self.observations.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    async def fetch_artifacts(self, build_id, attempt, results):
        return dict(self.artifacts)

    async def cancel(self, build_id, attempt, external_job_id):
        self.cancelled = True


async def _project_with_docs(db, owner: User) -> Project:
    project = Project(user_id=owner.id, name="External Buildable", status="spec_complete")
    db.add(project)
    await db.flush()
    for doc_type in ("requirements", "design"):
        db.add(
            Spec(
                project_id=project.id, version=1, type=doc_type,
                content=(
                    f"# {doc_type}\ncontent\n"
                    + ("Endpoint authentication: PUBLIC\n" if doc_type == "requirements" else "")
                ),
                created_by=owner.id,
            )
        )
    await db.commit()
    await db.refresh(project)
    return project


@pytest.fixture()
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner, "POLL_INTERVAL_S", 0.02)


def _wire(monkeypatch, fake):
    monkeypatch.setattr(runner, "get_provider", lambda name=None: fake)

    async def name():
        return "runner"

    monkeypatch.setattr(runner, "resolve_provider_name", name)


async def _wait_terminal(db, build_id, timeout=5.0) -> CodegenBuild:
    for _ in range(int(timeout * 50)):
        await asyncio.sleep(0.02)
        build = await db.get(CodegenBuild, build_id)
        await db.refresh(build)
        if build.status in runner.TERMINAL_STATES:
            return build
    raise AssertionError("build did not reach a terminal state")


# ------------------------------------------------------------------ contract


def test_contract_round_trip_and_doc_examples():
    request = contract.build_request(
        build_id=str(uuid.uuid4()), project_slug="x", project_name="X",
        artifact_profile="inline-cfn", model_id="m", token_budget=1000,
        allowlist=[], guardrail_rules=[], response_prefix="builds/x/response/",
        dispatched_at=datetime.now(UTC).isoformat(),
    )
    assert request["contract_version"] == contract.CONTRACT_VERSION
    assert request["endpoint_auth"] == {"mode": "key_required", "source": "default"}
    assert request["literal_contract"] == {"version": 1, "entries": []}
    assert request["require_web_console"] is False

    manifest = contract.results_manifest(
        status="succeeded",
        files=[{"path": "a.py", "sha256": "0" * 64, "bytes": 5}],
        usage=[{"model_id": "m", "input_tokens": 1, "output_tokens": 2}],
        engine={"name": "e", "version": "1"},
        app_name="app",
    )
    parsed = contract.parse_results(manifest)
    assert parsed.status == "succeeded" and parsed.files[0].path == "a.py"

    # The doc's JSON examples must parse against the schema (R1.3 anti-drift)
    doc = Path(__file__).parent.parent.parent / "docs" / "codegen-workspace-contract.md"
    blocks = re.findall(r"```json\n(.*?)```", doc.read_text(), re.S)
    assert len(blocks) >= 2
    for block in blocks:
        data = json.loads(block.replace("<64-hex>", "0" * 64))
        if "status" in data:
            contract.parse_results(data)  # must not raise


def test_contract_violation_matrix():
    base = contract.results_manifest(
        status="succeeded",
        files=[{"path": "a.py", "sha256": "0" * 64, "bytes": 5}],
        usage=[], engine={"name": "e"},
    )
    cases = [
        ("not a dict", "junk"),
        ("bad version", {**base, "contract_version": 99}),
        ("bad status", {**base, "status": "maybe"}),
        ("path escape", {**base, "files": [{"path": "../evil.py", "sha256": "0" * 64, "bytes": 1}]}),
        ("absolute path", {**base, "files": [{"path": "/etc/passwd", "sha256": "0" * 64, "bytes": 1}]}),
        ("short sha", {**base, "files": [{"path": "a.py", "sha256": "abc", "bytes": 1}]}),
        ("negative bytes", {**base, "files": [{"path": "a.py", "sha256": "0" * 64, "bytes": -1}]}),
        ("empty success", {**base, "files": []}),
        ("negative tokens", {**base, "usage": [{"model_id": "m", "input_tokens": -1, "output_tokens": 0}]}),
    ]
    for label, payload in cases:
        with pytest.raises(contract.ContractViolation):
            contract.parse_results(payload)
        assert label  # keeps the loop honest


# ------------------------------------------------------------------ lifecycle


async def test_external_build_happy_path(db_session, test_user, client_for, audit_db, monkeypatch, fast_poll):
    fake = FakeWorkspaceProvider(
        observations=[
            ExternalStatus(started=True, files_seen=[], results=None),
            ExternalStatus(started=True, files_seen=["src/handler.py"], results=None),
            ExternalStatus(started=True, files_seen=sorted(GOOD_ARTIFACTS), results=_results(GOOD_ARTIFACTS)),
        ]
    )
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)

    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        assert r.status_code == 202, r.text
        body = r.json()
        assert body["provider"] == "runner"
        build = await _wait_terminal(db_session, uuid.UUID(body["id"]))

    assert build.status == "ready", build.error
    assert build.external_job_id == "marshal-codegen-runner:job-1"
    assert build.manifest["engine"]["name"] == "fake-engine"
    assert build.manifest["reconciliation"]["reported_output_tokens"] == 2000
    assert build.manifest["validation"]["findings"] == []

    # Priced usage row with source=runner charged to the acting user (R6.2)
    rows = (
        (await db_session.execute(
            select(ModelInvocation).where(ModelInvocation.generation_id == build.id)
        )).scalars().all()
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "runner" and row.purpose == "codegen"
    assert row.user_id == test_user.id and row.cost_usd is not None
    expected = Decimal("0.003") * 4000 / 1000 + Decimal("0.015") * 2000 / 1000
    assert abs(Decimal(str(row.cost_usd)) - expected) < Decimal("0.0001")


async def test_external_engine_failure_surfaces(db_session, test_user, client_for, audit_db, monkeypatch, fast_poll):
    fake = FakeWorkspaceProvider(
        observations=[
            ExternalStatus(
                started=True, files_seen=[],
                results=_results({}, status="failed",
                                 error={"code": "token_budget_exhausted", "message": "budget gone"}),
            )
        ]
    )
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed"
    assert build.error["code"] == "token_budget_exhausted"


async def test_contract_violation_retries_once_then_fails(db_session, test_user, client_for, audit_db, monkeypatch, fast_poll):
    fake = FakeWorkspaceProvider(
        observations=[
            contract.ContractViolation("hash mismatch on attempt 1"),
            contract.ContractViolation("hash mismatch on attempt 2"),
        ]
    )
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed"
    assert build.error["code"] == "contract_violation"
    assert build.retried is True
    assert fake.dispatches == [1, 2]  # re-dispatch used the -r2 attempt


async def test_engine_timeout_retry_then_fail(db_session, test_user, client_for, audit_db, monkeypatch, fast_poll):
    fake = FakeWorkspaceProvider(observations=None)  # never returns results
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build_id = uuid.UUID(r.json()["id"])

    # Backdate both dispatches so each poll sees an expired deadline
    for _ in range(2):
        for _ in range(200):
            await asyncio.sleep(0.02)
            build = await db_session.get(CodegenBuild, build_id)
            await db_session.refresh(build)
            if build.dispatched_at is not None:
                build.dispatched_at = datetime.now(UTC) - timedelta(hours=1)
                await db_session.commit()
                break
        build = await db_session.get(CodegenBuild, build_id)
        await db_session.refresh(build)
        if build.status in runner.TERMINAL_STATES:
            break
        await asyncio.sleep(0.1)

    build = await _wait_terminal(db_session, build_id, timeout=8.0)
    assert build.status == "failed"
    assert build.error["code"] == "engine_timeout"
    assert build.retried is True


async def test_cancel_external_tombstones_and_ignores_late_results(
    db_session, test_user, client_for, audit_db, monkeypatch, fast_poll
):
    fake = FakeWorkspaceProvider(observations=None)
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build_id = uuid.UUID(r.json()["id"])
        await asyncio.sleep(0.1)  # let dispatch land
        r = await client.post(f"/api/v1/builds/{build_id}/cancel")
        assert r.status_code == 200

    build = await db_session.get(CodegenBuild, build_id)
    await db_session.refresh(build)
    assert build.status == "cancelled"
    assert fake.cancelled is True

    # Late results now arrive; the poll task must exit without resurrecting it
    fake.observations.append(
        ExternalStatus(started=True, files_seen=[], results=_results(GOOD_ARTIFACTS))
    )
    await asyncio.sleep(0.3)
    await db_session.refresh(build)
    assert build.status == "cancelled"


async def test_rehydrate_reattaches_external_fails_internal(
    db_session, test_user, audit_db, monkeypatch, fast_poll
):
    fake = FakeWorkspaceProvider(
        observations=[ExternalStatus(started=True, files_seen=[], results=_results(GOOD_ARTIFACTS))]
    )
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    external = CodegenBuild(
        project_id=project.id, status="dispatched", provider="runner",
        external_job_id="job-x", dispatched_at=datetime.now(UTC),
        spec_snapshot={
            "requirements": {
                "version": 1,
                "content": "# r\nEndpoint authentication: PUBLIC",
            },
            "design": {"version": 1, "content": "# d"},
        },
        created_by=test_user.id,
    )
    db_session.add(external)
    await db_session.commit()
    await db_session.refresh(external)

    project2 = Project(user_id=test_user.id, name="Internal orphan", status="draft")
    db_session.add(project2)
    await db_session.flush()
    internal = CodegenBuild(
        project_id=project2.id, status="generating", provider="internal",
        created_by=test_user.id,
    )
    db_session.add(internal)
    await db_session.commit()

    await runner.rehydrate_inflight_builds()

    await db_session.refresh(internal)
    assert internal.status == "failed" and internal.error["code"] == "restarted"

    build = await _wait_terminal(db_session, external.id)
    assert build.status == "ready"  # re-attached, polled, ingested


async def test_gate_parity_for_external_artifacts(
    db_session, test_user, client_for, audit_db, monkeypatch, fast_poll
):
    """R5: a disallowed resource fails ingest with the SAME finding the
    internal path produces (shared _finalize — pinned here)."""
    bad_template = json.dumps(
        {"Resources": {"Bad": {"Type": "AWS::EC2::Instance", "Properties": {}}}, "Outputs": {}}
    )
    artifacts = dict(GOOD_ARTIFACTS, **{"template.json": bad_template})
    fake = FakeWorkspaceProvider(
        observations=[ExternalStatus(started=True, files_seen=[], results=_results(artifacts))],
        artifacts=artifacts,
    )
    _wire(monkeypatch, fake)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed"
    assert build.error["code"] == "validation_failed"
    checks = {f["check"] for f in build.error["findings"]}
    assert "service_allowlist" in checks


async def test_dispatch_cap_preflight_blocks(db_session, test_user, client_for, audit_db, monkeypatch, fast_poll):
    """R6.1: over-cap at dispatch → failed cost_cap before anything runs."""
    fake = FakeWorkspaceProvider()
    _wire(monkeypatch, fake)

    async def blow_up(ctx, streaming=False):
        from app.services.spend import CostCapExceeded

        raise CostCapExceeded("user", Decimal("10"), Decimal("11"))

    monkeypatch.setattr("app.services.bedrock.preflight_model_call", blow_up)
    project = await _project_with_docs(db_session, test_user)
    async with client_for(test_user) as client:
        r = await client.post(f"/api/v1/projects/{project.id}/builds")
        build = await _wait_terminal(db_session, uuid.UUID(r.json()["id"]))
    assert build.status == "failed" and build.error["code"] == "cost_cap"
    assert fake.dispatches == []  # nothing was written to the workspace


# ------------------------------------------------------------------ settings


async def test_codegen_provider_setting_and_health(db_session, test_user, client_for, audit_db):
    admin = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="cg-admin@marshal.demo",
        role="admin", persona="power", onboarding_completed=True,
    )
    db_session.add(admin)
    await db_session.commit()
    from app.models import PlatformSettings

    if await db_session.get(PlatformSettings, 1) is None:
        db_session.add(PlatformSettings(id=1, model_allowlist=["us.anthropic.claude-sonnet-5"]))
        await db_session.commit()

    async with client_for(admin) as client:
        r = await client.get("/api/v1/admin/model-controls")
        payload = r.json()
        payload["codegen"] = {"provider": "runner"}
        r = await client.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["codegen"] == {"provider": "runner"}

        # invalid provider refused
        payload["codegen"] = {"provider": "skynet"}
        r = await client.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 422

        r = await client.get("/api/v1/admin/codegen-health")
        assert r.status_code == 200
        body = r.json()
        assert body["active_provider"] == "runner"  # platform setting wins (R6.5)
        assert body["runner"]["reachable"] is False  # no bucket configured in tests

    from app.services.codegen.provider import resolve_provider_name

    assert await resolve_provider_name() == "runner"


async def test_codegen_provider_legacy_alias_normalized(db_session, client_for, audit_db):
    """`kiro` is a legacy alias for `runner`: an admin PUT stores the canonical
    name, and a pre-rename settings row still resolves to the runner."""
    admin = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="cg-alias-admin@marshal.demo",
        role="admin", persona="power", onboarding_completed=True,
    )
    db_session.add(admin)
    await db_session.commit()
    from app.models import PlatformSettings

    if await db_session.get(PlatformSettings, 1) is None:
        db_session.add(PlatformSettings(id=1, model_allowlist=["us.anthropic.claude-sonnet-5"]))
        await db_session.commit()

    async with client_for(admin) as client:
        r = await client.get("/api/v1/admin/model-controls")
        payload = r.json()
        payload["codegen"] = {"provider": "kiro"}
        r = await client.put("/api/v1/admin/model-controls", json=payload)
        assert r.status_code == 200, r.text
        assert r.json()["codegen"] == {"provider": "runner"}  # stored normalized

    from app.services.codegen.provider import resolve_provider_name

    assert await resolve_provider_name() == "runner"

    # Historical row written before the rename still carries "kiro";
    # resolve_provider_name reads the row directly and must normalize it.
    row = await db_session.get(PlatformSettings, 1)
    row.codegen = {"provider": "kiro"}
    await db_session.commit()
    assert await resolve_provider_name() == "runner"


async def test_codegen_provider_non_string_is_422(db_session, client_for, audit_db):
    """A non-string `codegen.provider` is a validation error (422), never a 500
    from the normalizer's `.strip()` (wave-1 review finding 2)."""
    admin = User(
        cognito_sub=f"sub-{uuid.uuid4().hex[:8]}", email="cg-type-admin@marshal.demo",
        role="admin", persona="power", onboarding_completed=True,
    )
    db_session.add(admin)
    await db_session.commit()
    from app.models import PlatformSettings

    if await db_session.get(PlatformSettings, 1) is None:
        db_session.add(PlatformSettings(id=1, model_allowlist=["us.anthropic.claude-sonnet-5"]))
        await db_session.commit()

    async with client_for(admin) as client:
        r = await client.get("/api/v1/admin/model-controls")
        payload = r.json()
        for bad in (123, ["runner"], {"name": "runner"}, True):
            payload["codegen"] = {"provider": bad}
            r = await client.put("/api/v1/admin/model-controls", json=payload)
            assert r.status_code == 422, (bad, r.status_code, r.text)
            assert "codegen provider" in r.json()["detail"]


async def test_rehydrate_legacy_kiro_build_row_uses_real_get_provider(
    db_session, test_user, audit_db, monkeypatch, fast_poll
):
    """A historical `codegen_builds.provider == "kiro"` row (dispatched before
    the rename) re-attaches through the REAL `get_provider`, which must map the
    legacy name to the runner — `_wire`'s stub is deliberately NOT used here.
    Only the runner class is swapped for the scriptable fake."""
    from app.services.codegen import provider as provider_mod
    from app.services.codegen import workspace_runner

    fake = FakeWorkspaceProvider(
        observations=[ExternalStatus(started=True, files_seen=[], results=_results(GOOD_ARTIFACTS))]
    )
    monkeypatch.setattr(workspace_runner, "WorkspaceRunnerProvider", lambda: fake)
    resolved: list[str] = []

    def spy(name):
        resolved.append(name)
        return provider_mod.get_provider(name)

    monkeypatch.setattr(runner, "get_provider", spy)

    project = await _project_with_docs(db_session, test_user)
    legacy = CodegenBuild(
        project_id=project.id, status="dispatched", provider="kiro",
        external_job_id="job-legacy", dispatched_at=datetime.now(UTC),
        spec_snapshot={
            "requirements": {
                "version": 1,
                "content": "# r\nEndpoint authentication: PUBLIC",
            },
            "design": {"version": 1, "content": "# d"},
        },
        created_by=test_user.id,
    )
    db_session.add(legacy)
    await db_session.commit()
    await db_session.refresh(legacy)

    await runner.rehydrate_inflight_builds()

    build = await _wait_terminal(db_session, legacy.id)
    assert build.status == "ready"  # re-attached, polled via the runner, ingested
    assert build.provider == "kiro"  # the historical row itself is not rewritten
    assert resolved and set(resolved) == {"kiro"}  # the real resolver saw the legacy name
    assert fake.dispatches == []  # re-attach: polled, never re-dispatched
