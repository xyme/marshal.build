"""Scheduled-work election (platform-scale-policies spec R3.3).

With desiredCount > 1 every task boots the same background loops; the periodic
ticks (escalation sweep, deployment health/expiry) must fire ONCE per interval
platform-wide. Election is per-tick via Postgres session advisory locks: the
task that wins the lock runs the tick, everyone else skips. Ticks stay
idempotent by design (S6/S11), so a rare double-fire under failover degrades
to harmless re-checks.

Non-Postgres databases (tests, local SQLite) are single-task: always elected.
"""

import asyncio
import logging
import random

from sqlalchemy import text

logger = logging.getLogger("marshal.scheduler")

# Stable platform-wide advisory lock keys (int64 space is ours alone)
ESCALATION_LOCK = 412_001
LIFECYCLE_LOCK = 412_002
SANDBOX_SPEND_LOCK = 412_003
AUDIT_RETENTION_LOCK = 412_004
WEBHOOK_DELIVERY_LOCK = 412_005  # B10 retry sweep (integration-wave R3.3)


async def _window_fenced(name: str, interval_s: int) -> bool:
    """Exactly-once-per-interval fence on the shared counter store: the first
    task to bump the window counter owns the window. Fail-open: counter store
    trouble returns True (duplicate idempotent ticks beat missed ticks)."""
    import time

    from app.services.shared_state import STATE

    window = int(time.time() // interval_s)
    count = await STATE.add_and_get(f"tick:{name}:{window}", 1, ttl_s=interval_s * 3)
    return count is None or count == 1


async def _run_elected(lock_key: int, tick) -> bool:
    """Run `tick` iff this task wins the advisory lock. Returns elected?"""
    from app.core.db import engine

    if engine.url.get_backend_name() != "postgresql":
        await tick()
        return True
    async with engine.connect() as conn:
        got = (
            await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": lock_key})
        ).scalar()
        if not got:
            return False
        try:
            await tick()
        finally:
            try:
                await conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": lock_key})
            except Exception:  # noqa: BLE001 — conn teardown releases session locks
                logger.warning("advisory unlock failed for %s (released on close)", lock_key)
    return True


async def elected_loop(name: str, lock_key: int, tick, interval_s: int) -> None:
    """Periodic loop, exactly-one-task semantics per interval (R3.3):

    - window fence (shared counter): only the FIRST task in each interval
      window proceeds — offset duplicate ticks across tasks are skipped;
    - advisory lock: overlap protection if a fence-passing tick collides
      with a straggler (fail-open fence) or a very long prior tick.

    Ticks stay idempotent by design, so rare duplicates degrade to re-checks.
    """
    await asyncio.sleep(random.uniform(0, 5))  # de-lockstep task fleet boots
    while True:
        try:
            if await _window_fenced(name, interval_s):
                elected = await _run_elected(lock_key, tick)
                logger.info(
                    "scheduler: %s tick %s",
                    name, "ran (elected)" if elected else "skipped (lock held by peer)",
                )
            else:
                logger.info("scheduler: %s tick skipped (window owned by peer)", name)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("scheduler: %s tick failed", name)
        await asyncio.sleep(interval_s)
