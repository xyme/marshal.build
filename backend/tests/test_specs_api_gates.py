"""Spec editor API gates (spec-editor spec R3.2): owner 404 vs persona 403."""

from app.models import Project, Spec


async def _make_own_project(db_session, test_user) -> Project:
    project = Project(user_id=test_user.id, name="Mine")
    db_session.add(project)
    await db_session.flush()
    db_session.add(
        Spec(project_id=project.id, version=1, type="tasks", content="# Implementation Plan — x\n- [ ] 1. a\n  - Test: t")
    )
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def test_business_persona_gets_403_on_own_project(client, db_session, test_user):
    project = await _make_own_project(db_session, test_user)
    test_user.role = "business"
    test_user.persona = "business"
    await db_session.commit()

    response = await client.put(
        f"/api/v1/projects/{project.id}/specs/tasks",
        json={"content": "# Implementation Plan — hacked\n- [ ] 1. b\n  - Test: t"},
    )
    assert response.status_code == 403
    assert "Power User" in response.json()["detail"]


async def test_power_persona_can_edit_own_project(client, db_session, test_user):
    project = await _make_own_project(db_session, test_user)
    response = await client.put(
        f"/api/v1/projects/{project.id}/specs/tasks",
        json={"content": "# Implementation Plan — ok\n- [ ] 1. b _(Req: FR-1)_\n  - Test: t"},
    )
    assert response.status_code == 200
    assert response.json()["spec"]["version"] == 2


async def test_non_owner_gets_404_even_with_power_persona(client, db_session, test_user):
    import uuid

    from app.models import User

    other = User(cognito_sub=f"other-{uuid.uuid4().hex[:6]}", email="o@x.com", role="power", persona="power")
    db_session.add(other)
    await db_session.flush()
    foreign = Project(user_id=other.id, name="Not mine")
    db_session.add(foreign)
    await db_session.commit()

    response = await client.put(
        f"/api/v1/projects/{foreign.id}/specs/tasks", json={"content": "# x"}
    )
    assert response.status_code == 404


# ------------------------------------------- composable agents R1 (13.5R)


async def _mk_project(db_session, user, name):
    from app.models import Project

    p = Project(user_id=user.id, name=name, status="spec_complete")
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


def test_dependency_declaration_parsing():
    from app.services.composition import declared_dependencies

    slugs, orch = declared_dependencies(
        "The agent SHALL call agent support-bot-1a2b3c4d.\n"
        "It SHALL orchestrate agents intake-9f8e7d6c, triage-5a4b3c2d, "
        "support-bot-1a2b3c4d.\n"
    )
    # Orchestration order first, dedup across both phrases
    assert slugs == ["intake-9f8e7d6c", "triage-5a4b3c2d", "support-bot-1a2b3c4d"]
    assert orch is True
    assert declared_dependencies("we shall call agents eventually") == ([], False)
    assert declared_dependencies("") == ([], False)


async def test_composition_sync_validation_matrix(db_session, test_user, client_for):
    from app.services.export import project_slug

    a = await _mk_project(db_session, test_user, "Agent A")
    b = await _mk_project(db_session, test_user, "Agent B")
    slug_a, slug_b = project_slug(a), project_slug(b)

    async with client_for(test_user) as ac:
        # Unresolved slug → named 422
        r = await ac.put(
            f"/api/v1/projects/{a.id}/specs/requirements",
            json={"content": "# R\nSHALL call agent ghost-agent-deadbeef"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "composition_unresolved"

        # Self-dependency → named 422
        r = await ac.put(
            f"/api/v1/projects/{a.id}/specs/requirements",
            json={"content": f"# R\nSHALL call agent {slug_a}"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "composition_self"

        # Valid declaration stores the RESOLVED graph
        r = await ac.put(
            f"/api/v1/projects/{a.id}/specs/requirements",
            json={"content": f"# R\nSHALL call agent {slug_b}"},
        )
        assert r.status_code == 200
        await db_session.refresh(a)
        deps = a.composition["dependencies"]
        assert deps[0]["slug"] == slug_b and deps[0]["project_id"] == str(b.id)

        # Cycle: B declaring A closes the loop → named 422, B stays clean
        r = await ac.put(
            f"/api/v1/projects/{b.id}/specs/requirements",
            json={"content": f"# R\nSHALL call agent {slug_a}"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "composition_cycle"
        await db_session.refresh(b)
        assert b.composition is None

        # Undeclaring clears the stored graph (row stays truthful)
        r = await ac.put(
            f"/api/v1/projects/{a.id}/specs/requirements",
            json={"content": "# R\nNo dependencies anymore"},
        )
        assert r.status_code == 200
        await db_session.refresh(a)
        assert a.composition is None


async def test_composition_depth_cap(db_session, test_user, client_for):
    from app.services.export import project_slug

    a = await _mk_project(db_session, test_user, "Chain A")
    b = await _mk_project(db_session, test_user, "Chain B")
    c = await _mk_project(db_session, test_user, "Chain C")
    d = await _mk_project(db_session, test_user, "Chain D")

    async with client_for(test_user) as ac:
        # C→D (depth 1), B→C (depth 2) both fine
        r = await ac.put(
            f"/api/v1/projects/{c.id}/specs/requirements",
            json={"content": f"SHALL call agent {project_slug(d)}"},
        )
        assert r.status_code == 200
        r = await ac.put(
            f"/api/v1/projects/{b.id}/specs/requirements",
            json={"content": f"SHALL call agent {project_slug(c)}"},
        )
        assert r.status_code == 200
        # A→B→C→D reaches depth 3 → refused by name
        r = await ac.put(
            f"/api/v1/projects/{a.id}/specs/requirements",
            json={"content": f"SHALL call agent {project_slug(b)}"},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "composition_depth"


# ------------------------------ agent-substance R3.2: template capability rail


def test_declared_capabilities_parser():
    from app.services.capabilities import declared_capabilities

    text = (
        "# Req\nThe agent SHALL keep conversation memory.\n"
        "It SHALL use tools: convert_units.\n"
        "It SHALL use packaged dependencies.\n"
    )
    assert declared_capabilities(text) == ["memory", "packaged", "tools"]
    assert declared_capabilities("SHALL plan multi-step responses") == ["planning"]
    assert declared_capabilities("# plain requirements") == []


async def test_capability_rail_refusal_at_save(db_session, test_user, client):
    """AC-4: a forbidden rung declaration is a NAMED 422 at spec save."""
    from app.models import Template

    template = Template(
        name="Locked Down",
        category="custom",
        status="active",
        guardrails={"capabilities": {"allowed_capabilities": ["memory"]}},
        scaffolding={},
        created_by=test_user.id,
    )
    db_session.add(template)
    await db_session.flush()
    project = Project(user_id=test_user.id, name="Railed", template_id=template.id)
    db_session.add(project)
    await db_session.commit()

    # memory is allowed by the rail
    ok = await client.put(
        f"/api/v1/projects/{project.id}/specs/requirements",
        json={"content": "# Req\nThe agent SHALL keep conversation memory.\n"},
    )
    assert ok.status_code == 200, ok.text

    # planning is not — named refusal, save aborted
    refused = await client.put(
        f"/api/v1/projects/{project.id}/specs/requirements",
        json={"content": "# Req\nThe agent SHALL plan multi-step responses.\n"},
    )
    assert refused.status_code == 422
    body = refused.json()["detail"]
    assert body["code"] == "capability_not_allowed"
    assert "planning" in body["detail"] and "Locked Down" in body["detail"]

    # the refused content never became a version
    latest = await client.get(f"/api/v1/projects/{project.id}/specs")
    assert "planning" not in str(latest.json())


async def test_capability_rail_absent_means_all(db_session, test_user, client):
    from app.models import Template

    template = Template(
        name="Open", category="custom", status="active",
        guardrails={}, scaffolding={}, created_by=test_user.id,
    )
    db_session.add(template)
    await db_session.flush()
    project = Project(user_id=test_user.id, name="Unrailed", template_id=template.id)
    db_session.add(project)
    await db_session.commit()

    ok = await client.put(
        f"/api/v1/projects/{project.id}/specs/requirements",
        json={
            "content": "# Req\nSHALL use packaged dependencies. "
            "SHALL plan multi-step responses.\n"
        },
    )
    assert ok.status_code == 200, ok.text


async def test_capability_rail_admin_validation(admin_user, client_for, audit_db):
    async with client_for(admin_user) as ac:
        bad = await ac.post(
            "/api/v1/admin/templates",
            json={
                "name": "Bad Rail",
                "guardrails": {"capabilities": {"allowed_capabilities": ["memory", "teleport"]}},
            },
        )
        assert bad.status_code == 422
        assert "teleport" in bad.json()["detail"]

        bad_cap = await ac.post(
            "/api/v1/admin/templates",
            json={
                "name": "Bad Cap",
                "guardrails": {"capabilities": {"max_loop_iterations": 99}},
            },
        )
        assert bad_cap.status_code == 422
        assert "between 1 and 8" in bad_cap.json()["detail"]

        good = await ac.post(
            "/api/v1/admin/templates",
            json={
                "name": "Good Rail",
                "guardrails": {
                    "capabilities": {
                        "allowed_capabilities": ["memory", "tools"],
                        "max_loop_iterations": 4,
                    }
                },
            },
        )
        assert good.status_code == 201, good.text


def test_blocked_for_manifest_deploy_twin():
    from types import SimpleNamespace

    from app.services.capabilities import blocked_for_manifest

    rail = SimpleNamespace(
        guardrails={"capabilities": {"allowed_capabilities": ["memory"]}}
    )
    manifest = {"memory": True, "packaged": True, "tools": ["a"], "planning": False}
    assert blocked_for_manifest(manifest, rail) == ["packaged", "tools"]
    # absent rail = everything allowed; template-less = everything allowed
    open_tpl = SimpleNamespace(guardrails={})
    assert blocked_for_manifest(manifest, open_tpl) == []
    assert blocked_for_manifest(manifest, None) == []
