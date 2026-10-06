"""Rate limiting at the model seam (cost-caps-alerts spec R5; S12 R3.1).

Fixed 60-second windows on SHARED atomic counters (DynamoDB across N tasks;
in-process twin otherwise) — the S12 externalization of the S5 in-process
buckets. §4.6.7 semantics hold: queue up to max_wait_s, then 429 with
Retry-After. Admission counts once per admitted call; a small transient
overshoot under cross-task races is acceptable governance (fail-open family).
"""

import asyncio
import logging
import time
import uuid

from app.services.shared_state import STATE

logger = logging.getLogger("marshal.ratelimit")

WINDOW_S = 60


class RateLimited(Exception):
    def __init__(self, scope: str, retry_after_s: float):
        self.scope = scope
        self.retry_after_s = max(1, round(retry_after_s))
        super().__init__(f"rate limited ({scope}); retry in {self.retry_after_s}s")


def _window() -> tuple[int, float]:
    now = time.time()
    window = int(now // WINDOW_S)
    seconds_left = WINDOW_S - (now - window * WINDOW_S)
    return window, seconds_left


class TokenBuckets:
    """Name kept for the S5 call sites; semantics are shared fixed windows."""

    async def acquire(
        self,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        *,
        max_wait_s: float,
    ) -> None:
        """Admit or raise RateLimited. Fail-open on settings/state trouble."""
        try:
            from app.services.platform_settings import get_controls

            limits = (await get_controls()).rate_limits or {}
        except Exception:  # noqa: BLE001 — fail open
            logger.exception("rate limit settings read failed; skipping")
            return

        scopes: list[tuple[str, str, int]] = []
        if limits.get("platform_rpm"):
            scopes.append(("platform", "platform", int(limits["platform_rpm"])))
        if user_id and limits.get("per_user_rpm"):
            scopes.append(("user", f"user:{user_id}", int(limits["per_user_rpm"])))
        if project_id and limits.get("per_project_rpm"):
            scopes.append(("project", f"project:{project_id}", int(limits["per_project_rpm"])))
        if not scopes:
            return

        deadline = time.monotonic() + max_wait_s
        while True:
            window, seconds_left = _window()
            blocked: tuple[str, float] | None = None
            for scope, key, rpm in scopes:
                current = await STATE.get(f"rate:{key}:{window}")
                if current is None:  # shared state down → fail open
                    return
                if current >= rpm:
                    blocked = (scope, seconds_left)
                    break
            if blocked is None:
                for _scope, key, _rpm in scopes:
                    await STATE.add_and_get(f"rate:{key}:{window}", 1, ttl_s=WINDOW_S * 2)
                return
            scope, wait = blocked
            if time.monotonic() + wait > deadline:
                raise RateLimited(scope, wait)
            await asyncio.sleep(min(wait + 0.05, max(deadline - time.monotonic(), 0.05)))


BUCKETS = TokenBuckets()
