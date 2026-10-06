"""Template lifecycle + role gates (template-guardrails spec R1/R2/R5)."""

MODEL_RAILS = {
    "model": {"allowed_models": ["us.anthropic.claude-sonnet-5"], "max_tokens": 2048}
}


async def make_admin(db_session, test_user):
    test_user.role = "admin"
    await db_session.commit()


async def test_admin_api_forbidden_for_non_admin(client):
    response = await client.get("/api/v1/admin/templates")
    assert response.status_code == 403


async def test_lifecycle_create_publish_deprecate(client, db_session, test_user):
    await make_admin(db_session, test_user)

    created = await client.post(
        "/api/v1/admin/templates",
        json={"name": "RAG", "category": "chatbot", "guardrails": MODEL_RAILS},
    )
    assert created.status_code == 201
    template = created.json()
    assert template["status"] == "draft"
    tid = template["id"]

    published = await client.post(f"/api/v1/admin/templates/{tid}/publish")
    assert published.status_code == 200
    assert published.json()["status"] == "active"

    deprecated = await client.post(f"/api/v1/admin/templates/{tid}/deprecate")
    assert deprecated.status_code == 200
    assert deprecated.json()["status"] == "deprecated"


async def test_publish_requires_model_guardrails(client, db_session, test_user):
    await make_admin(db_session, test_user)
    created = await client.post(
        "/api/v1/admin/templates", json={"name": "Empty", "category": "custom"}
    )
    tid = created.json()["id"]
    response = await client.post(f"/api/v1/admin/templates/{tid}/publish")
    assert response.status_code == 422
    assert "guardrail" in response.json()["detail"].lower()


async def test_active_guardrail_edit_bumps_version(client, db_session, test_user):
    await make_admin(db_session, test_user)
    created = await client.post(
        "/api/v1/admin/templates",
        json={"name": "Bump", "category": "custom", "guardrails": MODEL_RAILS},
    )
    tid = created.json()["id"]
    await client.post(f"/api/v1/admin/templates/{tid}/publish")

    updated = await client.put(
        f"/api/v1/admin/templates/{tid}",
        json={"guardrails": {"model": {"allowed_models": ["us.anthropic.claude-haiku-4-5-20251001-v1:0"]}}},
    )
    assert updated.json()["version"] == 2


async def test_delete_only_drafts(client, db_session, test_user):
    await make_admin(db_session, test_user)
    created = await client.post(
        "/api/v1/admin/templates",
        json={"name": "Del", "category": "custom", "guardrails": MODEL_RAILS},
    )
    tid = created.json()["id"]
    await client.post(f"/api/v1/admin/templates/{tid}/publish")
    assert (await client.delete(f"/api/v1/admin/templates/{tid}")).status_code == 409


async def test_user_listing_shows_only_active_without_admin_fields(client, db_session, test_user):
    await make_admin(db_session, test_user)
    for name, publish in (("Live", True), ("Draft", False)):
        created = await client.post(
            "/api/v1/admin/templates",
            json={"name": name, "category": "chatbot", "guardrails": MODEL_RAILS,
                  "scaffolding": {"starter_prompts": ["Build me a bot"]}},
        )
        if publish:
            await client.post(f"/api/v1/admin/templates/{created.json()['id']}/publish")

    listing = await client.get("/api/v1/templates")
    assert listing.status_code == 200
    items = listing.json()
    assert [t["name"] for t in items] == ["Live"]
    assert "guardrails" not in items[0]
    assert items[0]["starter_prompts"] == ["Build me a bot"]
    assert items[0]["allowed_model_labels"] == ["Claude Sonnet 5"]


async def test_session_with_deprecated_template_rejected(client, db_session, test_user):
    await make_admin(db_session, test_user)
    created = await client.post(
        "/api/v1/admin/templates",
        json={"name": "Gone", "category": "custom", "guardrails": MODEL_RAILS},
    )
    tid = created.json()["id"]
    await client.post(f"/api/v1/admin/templates/{tid}/publish")
    await client.post(f"/api/v1/admin/templates/{tid}/deprecate")

    response = await client.post("/api/v1/chat/sessions", json={"template_id": tid})
    assert response.status_code == 422
