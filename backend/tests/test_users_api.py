"""Persona validation matrix + profile API tests (user-persona-profile spec)."""


async def test_get_me(client, test_user):
    response = await client.get("/api/v1/users/me")
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == "power@marshal.demo"
    assert body["role"] == "power"


async def test_power_user_can_switch_persona_both_ways(client):
    r1 = await client.put("/api/v1/users/me", json={"persona": "business"})
    assert r1.status_code == 200
    assert r1.json()["user"]["persona"] == "business"
    assert r1.json()["pending_approval"] is False

    r2 = await client.put("/api/v1/users/me", json={"persona": "power"})
    assert r2.json()["user"]["persona"] == "power"


async def test_business_role_requesting_power_flags_pending(client, test_user, db_session):
    test_user.role = "business"
    test_user.persona = "business"
    await db_session.commit()

    response = await client.put("/api/v1/users/me", json={"persona": "power"})
    body = response.json()
    assert body["pending_approval"] is True
    assert body["user"]["persona"] == "business"  # unchanged
    assert body["user"]["persona_upgrade_requested"] is True


async def test_update_profile_fields(client):
    response = await client.put(
        "/api/v1/users/me",
        json={"name": "Avery", "onboarding_completed": True, "tour_completed": True},
    )
    body = response.json()["user"]
    assert body["name"] == "Avery"
    assert body["onboarding_completed"] is True
    assert body["tour_completed"] is True


async def test_stats_counts(client):
    response = await client.get("/api/v1/users/me/stats")
    assert response.status_code == 200
    assert response.json() == {"projects": 0, "specs": 0, "sessions": 0, "deployments": 0}
