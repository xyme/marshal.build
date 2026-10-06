"""marshal control plane API entrypoint."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.config import get_settings
from app.core.db import engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("marshal")


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logger.info("Starting %s (env=%s, region=%s)", settings.app_name, settings.environment, settings.aws_region)
    # Resume/fail work that was mid-flight when the process stopped.
    from app.services.codegen.runner import rehydrate_inflight_builds
    from app.services.deployment import rehydrate_inflight_deployments
    from app.services.specgen import rehydrate_inflight_generations
    from app.services.spend import TRACKER

    await rehydrate_inflight_deployments()
    await rehydrate_inflight_generations()
    await rehydrate_inflight_builds()  # S8: fail builds orphaned by restarts
    await TRACKER.hydrate()  # real-time spend counters (S5 R1)

    # Cross-task event relay (S12 R3.2): SSE events reach subscribers on
    # every task; sqlite/local runs in local-only mode automatically.
    from app.services import specgen
    from app.services.codegen import runner as codegen_runner
    from app.services.deployment import event_bus as deployment_bus
    from app.services.event_relay import RELAY

    RELAY.wire("deploy", deployment_bus)
    RELAY.wire("codegen", codegen_runner.codegen_bus)
    RELAY.wire("specgen", specgen.generation_bus)
    await RELAY.start()

    import asyncio

    # Periodic ticks run under per-tick advisory-lock election (S12 R3.3):
    # exactly one task fires each interval; ticks stay idempotent by design.
    from app.services.scheduler import (
        AUDIT_RETENTION_LOCK,
        ESCALATION_LOCK,
        LIFECYCLE_LOCK,
        SANDBOX_SPEND_LOCK,
        WEBHOOK_DELIVERY_LOCK,
        elected_loop,
    )

    background: list = []
    if settings.cost_explorer_enabled:
        from app.services.sandbox_costs import POLL_INTERVAL_S, poll_once

        background.append(
            asyncio.create_task(
                elected_loop("sandbox_spend", SANDBOX_SPEND_LOCK, poll_once, POLL_INTERVAL_S)
            )
        )

    async def escalation_tick_once() -> None:
        # Review escalation sweep (S6 R4): idempotent, timestamp-derived
        from app.services.risk_review import escalation_tick

        await escalation_tick()

    async def lifecycle_tick_once() -> None:
        # Deployment health probes + expiry sweep (S11)
        from app.services.deployment_lifecycle import expiry_tick, health_tick

        await health_tick()
        await expiry_tick()

    background.append(
        asyncio.create_task(
            elected_loop("escalation", ESCALATION_LOCK, escalation_tick_once, 900)
        )
    )
    background.append(
        asyncio.create_task(
            elected_loop("lifecycle", LIFECYCLE_LOCK, lifecycle_tick_once, 900)
        )
    )

    async def webhook_delivery_tick_once() -> None:
        # B10: retry sweep over durable delivery rows (integration-wave R3.3)
        from app.services.webhooks import SWEEP_INTERVAL_S as _  # noqa: F401
        from app.services.webhooks import delivery_tick

        await delivery_tick()

    background.append(
        asyncio.create_task(
            elected_loop("webhook_delivery", WEBHOOK_DELIVERY_LOCK, webhook_delivery_tick_once, 60)
        )
    )

    async def audit_retention_tick_once() -> None:
        # S14-06: archive past-window audit rows to S3, then prune (6h cadence;
        # the window is 180 days, so there is no urgency — only certainty).
        from app.services.audit_retention import archive_tick

        await archive_tick()

    background.append(
        asyncio.create_task(
            elected_loop(
                "audit_retention", AUDIT_RETENTION_LOCK, audit_retention_tick_once, 6 * 3600
            )
        )
    )
    yield
    for task in background:
        task.cancel()
    await RELAY.stop()
    await engine.dispose()


class AuditMiddleware:
    """Raw ASGI wrapper (BaseHTTPMiddleware would buffer SSE streams).

    Captures the response status, then — after the response has fully gone out —
    hands the request to the audit seam (audit-logging spec R1.4). The router
    has populated scope[route/path_params/state] by then, and post-response
    means zero added latency on the user path.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status: dict[str, int] = {}

        async def send_wrapper(message):
            if message["type"] == "http.response.start":
                status["code"] = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            from starlette.requests import Request

            from app.services import audit

            code = status.get("code")
            if code is not None:
                try:
                    await audit.capture_request(Request(scope), code)
                except Exception:  # noqa: BLE001 — never disturb the connection
                    logger.exception("audit capture failed")


def create_app() -> FastAPI:
    from app.core.limits import BodySizeLimitMiddleware

    app = FastAPI(title="marshal API", version="0.1.0", lifespan=lifespan)
    # Pre-Beta hardening: coarse body-size backstop (413) — field caps do the
    # fine-grained work. Added FIRST so AuditMiddleware (added after, therefore
    # outermost) still observes refused requests.
    app.add_middleware(BodySizeLimitMiddleware)
    app.add_middleware(AuditMiddleware)

    # 429 mappers for cost caps + rate limits (S5; FSD §4.6.7/§4.6.9 AC-4)
    from fastapi.responses import JSONResponse

    from app.services.ratelimit import RateLimited
    from app.services.spend import CostCapExceeded

    @app.exception_handler(CostCapExceeded)
    async def _cost_cap_handler(request, exc: CostCapExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": {
                "code": "cost_cap", "scope": exc.scope,
                "cap_usd": float(exc.cap), "mtd_usd": float(exc.mtd),
                "message": "Monthly cost cap reached. An admin can raise the cap "
                           "under Admin → Model Controls.",
            }},
        )

    @app.exception_handler(RateLimited)
    async def _rate_limited_handler(request, exc: RateLimited):
        return JSONResponse(
            status_code=429,
            headers={"Retry-After": str(exc.retry_after_s)},
            content={"detail": {
                "code": "rate_limited", "scope": exc.scope,
                "retry_after_s": exc.retry_after_s,
                "message": f"Too many model requests — retry in {exc.retry_after_s}s.",
            }},
        )

    from app.services.platform_settings import SecuritySettingsUnavailable

    @app.exception_handler(SecuritySettingsUnavailable)
    async def _security_settings_handler(request, exc: SecuritySettingsUnavailable):
        return JSONResponse(
            status_code=503,
            headers={"Retry-After": "5"},
            content={"detail": {
                "code": "security_settings_unavailable",
                "message": "Security settings are temporarily unavailable; "
                           "no privileged action or model request was performed.",
            }},
        )

    from app.services.model_providers.openai_compat import ExternalEndpointError

    @app.exception_handler(ExternalEndpointError)
    async def _external_endpoint_handler(request, exc: ExternalEndpointError):
        # S17-04 fail-closed [D16]: a named 502, never a silent Bedrock fallback.
        return JSONResponse(
            status_code=502,
            content={"detail": {
                "code": "external_endpoint_failure",
                "endpoint": exc.label,
                "slug": exc.slug,
                "message": str(exc),
            }},
        )

    from app.api.admin_audit import router as admin_audit_router
    from app.api.admin_governance import router as admin_governance_router
    from app.api.admin_integrations import router as admin_integrations_router
    from app.api.admin_service_accounts import router as admin_service_accounts_router
    from app.api.admin_users import router as admin_users_router
    from app.api.analytics import admin_router as analytics_admin_router
    from app.api.builds import router as builds_router
    from app.api.chat import router as chat_router
    from app.api.collab import router as collab_router
    from app.api.deployments import router as deployments_router
    from app.api.health import meta_router
    from app.api.health import router as health_router
    from app.api.marketplace import admin_router as marketplace_admin_router
    from app.api.marketplace import admin_submissions_router, submissions_router
    from app.api.marketplace import router as marketplace_router
    from app.api.notifications import router as notifications_router
    from app.api.projects import router as projects_router
    from app.api.reviews import router as reviews_router
    from app.api.search import router as search_router
    from app.api.specs import router as specs_router
    from app.api.teams import admin_router as teams_admin_router
    from app.api.teams import router as teams_router
    from app.api.templates import admin_router as templates_admin_router
    from app.api.templates import router as templates_router
    from app.api.users import router as users_router

    app.include_router(health_router)
    app.include_router(users_router, prefix="/api/v1")
    app.include_router(search_router, prefix="/api/v1")
    app.include_router(chat_router, prefix="/api/v1")
    app.include_router(projects_router, prefix="/api/v1")
    app.include_router(specs_router, prefix="/api/v1")
    app.include_router(deployments_router, prefix="/api/v1")
    app.include_router(collab_router, prefix="/api/v1")
    app.include_router(builds_router, prefix="/api/v1")
    app.include_router(templates_router, prefix="/api/v1")
    app.include_router(templates_admin_router, prefix="/api/v1")
    app.include_router(admin_audit_router, prefix="/api/v1")
    app.include_router(marketplace_router, prefix="/api/v1")
    app.include_router(marketplace_admin_router, prefix="/api/v1")
    app.include_router(submissions_router, prefix="/api/v1")
    app.include_router(admin_submissions_router, prefix="/api/v1")
    app.include_router(admin_users_router, prefix="/api/v1")
    app.include_router(admin_service_accounts_router, prefix="/api/v1")
    app.include_router(admin_integrations_router, prefix="/api/v1")
    app.include_router(admin_governance_router, prefix="/api/v1")
    app.include_router(notifications_router, prefix="/api/v1")
    app.include_router(reviews_router, prefix="/api/v1")
    app.include_router(teams_router, prefix="/api/v1")
    app.include_router(teams_admin_router, prefix="/api/v1")
    app.include_router(meta_router, prefix="/api/v1")
    app.include_router(analytics_admin_router, prefix="/api/v1")
    return app


app = create_app()
