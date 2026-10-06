"""Smoke test — app factory + health endpoint."""

import pytest


async def test_healthz(client):
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert "status" in response.json()


# --------------------------------------------------------- S16-06 build info


@pytest.mark.asyncio
async def test_build_info_reports_heads(test_user, client_for, audit_db):
    async with client_for(test_user) as ac:
        resp = await ac.get("/api/v1/meta/build-info")

    assert resp.status_code == 200
    body = resp.json()
    # The migration head the CODE ships must always resolve (alembic dir in-tree)
    assert body["migration_head_code"], "script-directory head missing"
    # Test schema is model-built, so the DB has no alembic_version → honest None
    assert body["migration_head_db"] is None
    assert body["migrations_in_sync"] is False
    assert body["environment"]
    assert set(body) >= {
        "git_sha", "build_time", "migration_head_code", "migration_head_db",
        "migrations_in_sync", "environment",
    }
