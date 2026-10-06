"""Real-time spend tracking + cost cap enforcement (cost-caps-alerts spec R1-R3).

In-process month-to-date counters, hydrated from model_invocations at startup —
correct for the single-task Alpha (all traffic flows through this process) and
designed to swap to shared storage with the S-scale event-bus externalization.
Fail-open discipline: tracker trouble logs and skips enforcement, never breaks
the request path.
"""

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Alert, ModelInvocation, PlatformSettings, Project, User

logger = logging.getLogger("marshal.spend")

# Platform-internal purposes exempt from user/project caps (R2.4): governance
# must not die of its own budget. They still charge the platform total.
CAP_EXEMPT_PURPOSES = {"classification", "title"}


class CostCapExceeded(Exception):
    def __init__(self, scope: str, cap: Decimal, mtd: Decimal):
        self.scope, self.cap, self.mtd = scope, cap, mtd
        super().__init__(f"{scope} cap {cap} reached (MTD {mtd})")


@dataclass
class Crossing:
    scope: str  # user|project|platform
    scope_id: uuid.UUID | None
    threshold_pct: int
    cap: Decimal
    mtd: Decimal


def _cents(value: Decimal) -> int:
    return int((value * 100).to_integral_value())


def _usd(cents: int) -> Decimal:
    return Decimal(cents) / 100


@dataclass
class SpendTracker:
    """Month-to-date spend on SHARED counters (S12 R3.1): correct across N
    backend tasks. Keys: spend:{YYYY-MM}:{user|proj|platform}[:{id}], cents.

    First boot of a month seeds counters from model_invocations truth
    (seed-if-absent — idempotent across tasks racing at startup).
    """

    month: str = ""
    hydrated: bool = False

    def _current_month(self) -> str:
        return datetime.now(UTC).strftime("%Y-%m")

    @staticmethod
    def _key(month: str, scope: str, scope_id=None) -> str:
        return f"spend:{month}:{scope}" + (f":{scope_id}" if scope_id else "")

    async def hydrate(self) -> None:
        """Seed this month's counters from DB truth where absent (idempotent)."""
        from app.core.db import SessionLocal
        from app.services.shared_state import STATE

        month = self._current_month()
        start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        try:
            async with SessionLocal() as db:
                rows = await db.execute(
                    select(
                        ModelInvocation.user_id,
                        ModelInvocation.project_id,
                        func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                    )
                    .where(
                        ModelInvocation.created_at >= start,
                        ModelInvocation.cost_usd.is_not(None),
                    )
                    .group_by(ModelInvocation.user_id, ModelInvocation.project_id)
                )
                user_totals: dict = {}
                project_totals: dict = {}
                platform_total = Decimal("0")
                for user_id, project_id, total in rows.all():
                    total = Decimal(str(total))
                    if user_id:
                        user_totals[user_id] = user_totals.get(user_id, Decimal("0")) + total
                    if project_id:
                        project_totals[project_id] = (
                            project_totals.get(project_id, Decimal("0")) + total
                        )
                    platform_total += total
            for user_id, total in user_totals.items():
                await STATE.seed_if_absent(self._key(month, "user", user_id), _cents(total))
            for project_id, total in project_totals.items():
                await STATE.seed_if_absent(self._key(month, "proj", project_id), _cents(total))
            await STATE.seed_if_absent(self._key(month, "platform"), _cents(platform_total))
            self.month = month
            self.hydrated = True
            logger.info(
                "spend tracker seeded: month=%s platform_mtd=%s users=%d projects=%d",
                month, platform_total, len(user_totals), len(project_totals),
            )
        except Exception:  # noqa: BLE001 — fail open (R1.3)
            logger.exception("spend tracker hydration failed; enforcement will fail open")
            self.hydrated = False

    async def _ensure_month(self) -> None:
        if self.month != self._current_month():
            await self.hydrate()

    async def _mtd(self, scope: str, scope_id=None) -> Decimal | None:
        from app.services.shared_state import STATE

        cents = await STATE.get(self._key(self._current_month(), scope, scope_id))
        return None if cents is None else _usd(cents)

    # Read helpers used by dispatch pre-flights (S9) and tests
    @property
    def user_mtd(self) -> "_ScopeView":
        return _ScopeView(self, "user")

    @property
    def project_mtd(self) -> "_ScopeView":
        return _ScopeView(self, "proj")

    @property
    def platform_mtd(self) -> Decimal:
        """Sync best-effort platform MTD (in-process backend answers inline;
        DynamoDB-backed deployments read via async _mtd — this returns 0)."""
        return _sync_read(self._key(self._current_month(), "platform"))

    @platform_mtd.setter
    def platform_mtd(self, value: Decimal) -> None:
        _sync_write(self._key(self._current_month(), "platform"), value)

    async def add(
        self,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        cost: Decimal,
        caps: "EffectiveCaps",
    ) -> list[Crossing]:
        """Record spend atomically; return thresholds crossed by this increment."""
        from app.services.shared_state import STATE

        await self._ensure_month()
        month = self._current_month()
        cost_cents = _cents(cost)
        crossings: list[Crossing] = []

        async def bump(scope: str, scope_id, cap: Decimal | None, label: str) -> None:
            after_cents = await STATE.add_and_get(self._key(month, scope, scope_id), cost_cents)
            if after_cents is None or not cap or cap <= 0:
                return
            after = _usd(after_cents)
            before = _usd(after_cents - cost_cents)
            for pct in caps.thresholds:
                line = cap * pct / 100
                if before < line <= after:
                    crossings.append(Crossing(label, scope_id, pct, cap, after))

        if user_id:
            await bump("user", user_id, caps.user_cap, "user")
        if project_id:
            await bump("proj", project_id, caps.project_cap, "project")
        await bump("platform", None, caps.platform_cap, "platform")
        return crossings

    async def check(
        self,
        user_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        caps: "EffectiveCaps",
        *,
        exempt_user_scopes: bool,
    ) -> None:
        """Raise CostCapExceeded when block mode should refuse the call (R2.2)."""
        await self._ensure_month()
        if not self.hydrated:
            logger.warning("spend tracker not hydrated — cap check skipped (fail open)")
            return
        if caps.at_cap != "block":
            return
        if not exempt_user_scopes:
            if user_id and caps.user_cap:
                mtd = await self._mtd("user", user_id)
                if mtd is not None and mtd >= caps.user_cap:
                    raise CostCapExceeded("user", caps.user_cap, mtd)
            if project_id and caps.project_cap:
                mtd = await self._mtd("proj", project_id)
                if mtd is not None and mtd >= caps.project_cap:
                    raise CostCapExceeded("project", caps.project_cap, mtd)
        if caps.platform_cap:
            mtd = await self._mtd("platform")
            if mtd is not None and mtd >= caps.platform_cap:
                raise CostCapExceeded("platform", caps.platform_cap, mtd)


def _sync_read(key: str, default: Decimal = Decimal("0")) -> Decimal:
    """Best-effort synchronous counter read. The in-process backend answers
    inline; on DynamoDB (cross-task) this returns the default — callers use
    these reads only for ESTIMATES (S9 dispatch headroom, tests). Authoritative
    enforcement paths (check/add) go through the async STATE API."""
    from app.services.shared_state import STATE, _InProcessBackend

    try:
        backend = STATE.backend
        if isinstance(backend, _InProcessBackend):
            return _usd(backend.get(key))
    except Exception:  # noqa: BLE001 — fail open
        pass
    return default


def _sync_write(key: str, value: Decimal) -> None:
    """Overwrite a counter (test seam; in-process backend only)."""
    from app.services.shared_state import STATE, _InProcessBackend

    backend = STATE.backend
    if isinstance(backend, _InProcessBackend):
        backend.put(key, _cents(value))


class _ScopeView:
    """Dict-shaped view over shared counters (S9 dispatch pre-flight and the
    S5-era test seams read/write items like a dict)."""

    def __init__(self, tracker: SpendTracker, scope: str):
        self._tracker = tracker
        self._scope = scope

    def _key(self, scope_id) -> str:
        return SpendTracker._key(self._tracker._current_month(), self._scope, scope_id)

    def get(self, scope_id, default=Decimal("0")) -> Decimal:
        return _sync_read(self._key(scope_id), default)

    def __getitem__(self, scope_id) -> Decimal:
        return _sync_read(self._key(scope_id))

    def __setitem__(self, scope_id, value: Decimal) -> None:
        _sync_write(self._key(scope_id), value)


TRACKER = SpendTracker()


@dataclass(frozen=True)
class EffectiveCaps:
    user_cap: Decimal | None
    project_cap: Decimal | None
    platform_cap: Decimal | None
    thresholds: list[int]
    at_cap: str  # alert|block


async def resolve_caps(
    db: AsyncSession, user_id: uuid.UUID | None, project_id: uuid.UUID | None
) -> EffectiveCaps:
    """Effective caps per R2.1: overrides win, else platform defaults."""
    settings_row = await db.get(PlatformSettings, 1)
    cost = (settings_row.cost or {}) if settings_row else {}

    def dec(value) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    user_cap = dec(cost.get("default_user_cap_usd"))
    if user_id:
        user = await db.get(User, user_id)
        if user is not None and user.budget_override_usd is not None:
            user_cap = Decimal(str(user.budget_override_usd))
    project_cap = dec(cost.get("default_project_cap_usd"))
    if project_id:
        project = await db.get(Project, project_id)
        if project is not None and project.budget_override_usd is not None:
            project_cap = Decimal(str(project.budget_override_usd))
    return EffectiveCaps(
        user_cap=user_cap,
        project_cap=project_cap,
        platform_cap=dec(cost.get("platform_budget_usd")),
        thresholds=sorted(int(t) for t in cost.get("alert_thresholds", [50, 75, 90, 100])),
        at_cap=str(cost.get("at_cap", "alert")),
    )


async def record_crossings(db: AsyncSession, crossings: list[Crossing]) -> None:
    """Crossings → alerts rows (monthly dedupe) + notifications fan-out (R3)."""
    from sqlalchemy.exc import IntegrityError

    from app.services import notifications as notif

    month = datetime.now(UTC).strftime("%Y-%m")
    admin_ids = [
        row[0]
        for row in (await db.execute(select(User.id).where(User.role == "admin"))).all()
    ]
    for crossing in crossings:
        severity = "critical" if crossing.threshold_pct >= 100 else (
            "warning" if crossing.threshold_pct >= 90 else "info"
        )
        scope_label = crossing.scope
        subject = ""
        recipient_ids: list[uuid.UUID] = []
        if crossing.scope == "user":
            user = await db.get(User, crossing.scope_id)
            subject = user.email if user else "user"
            recipient_ids = [crossing.scope_id]
        elif crossing.scope == "project":
            project = await db.get(Project, crossing.scope_id)
            subject = project.name if project else "project"
            recipient_ids = [project.user_id] if project else []
        else:
            subject = "platform budget"
        message = (
            f"{subject} reached {crossing.threshold_pct}% of monthly cap "
            f"(${crossing.mtd:.2f}/${crossing.cap:.2f})"
        )
        dedupe_key = f"cost_{scope_label}:{crossing.scope_id}:{crossing.threshold_pct}:{month}"
        # Check-first dedupe (single-task Alpha: no insert race); the partial
        # unique index remains the backstop. Avoids mid-loop rollbacks that
        # would expire the caller's session state.
        existing = await db.execute(select(Alert.id).where(Alert.dedupe_key == dedupe_key))
        if existing.first() is not None:
            continue
        alert = Alert(
            kind=f"cost_{scope_label}",
            severity=severity,
            scope_user_id=crossing.scope_id if crossing.scope == "user" else None,
            scope_project_id=crossing.scope_id if crossing.scope == "project" else None,
            threshold_pct=crossing.threshold_pct,
            message=message,
            month=month,
            dedupe_key=dedupe_key,
        )
        db.add(alert)
        try:
            await db.commit()
        except IntegrityError:  # race backstop
            await db.rollback()
            continue
        event = "cost_cap_reached" if crossing.threshold_pct >= 100 else "cost_threshold"
        targets = set(recipient_ids)
        if crossing.threshold_pct >= 100 or crossing.scope == "platform":
            targets.update(admin_ids)
        for target_id in targets:
            target = await db.get(User, target_id)
            if target is not None:
                await notif.notify(
                    db, target,
                    type=event, title="Cost alert", body=message, link="/profile",
                    dedupe_key=f"{event}:{crossing.scope}:{crossing.scope_id}:{crossing.threshold_pct}:{month}",
                )


async def enclave_mtd(db: AsyncSession, project_id: uuid.UUID | None = None) -> Decimal | None:
    """Month-to-date Enclave infrastructure spend (S12 R4): CE-sourced
    sandbox_spend rows folded into budget surfaces. None while the Cost
    Explorer integration is dark (owner action pending) — callers hide the
    figure rather than show a misleading $0.
    """
    from app.core.config import get_settings
    from app.models import SandboxSpend

    if not get_settings().cost_explorer_enabled:
        return None
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    stmt = select(func.coalesce(func.sum(SandboxSpend.usd), 0)).where(
        SandboxSpend.date >= start
    )
    if project_id is not None:
        stmt = stmt.where(SandboxSpend.project_id == project_id)
    total = (await db.execute(stmt)).scalar_one()
    return Decimal(str(total))


async def usage_summary(db: AsyncSession, user: User) -> dict:
    """MTD spend vs cap + per-project rows (R7 /users/me/usage)."""
    start = datetime.now(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    caps = await resolve_caps(db, user.id, None)
    total = (
        await db.execute(
            select(func.coalesce(func.sum(ModelInvocation.cost_usd), 0)).where(
                ModelInvocation.user_id == user.id,
                ModelInvocation.created_at >= start,
                ModelInvocation.cost_usd.is_not(None),
            )
        )
    ).scalar_one()
    rows = await db.execute(
        select(
            ModelInvocation.project_id,
            func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
        )
        .where(
            ModelInvocation.user_id == user.id,
            ModelInvocation.created_at >= start,
            ModelInvocation.project_id.is_not(None),
            ModelInvocation.cost_usd.is_not(None),
        )
        .group_by(ModelInvocation.project_id)
        .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
        .limit(20)
    )
    project_rows = []
    for project_id, usd in rows.all():
        project = await db.get(Project, project_id)
        if project is None:
            continue
        project_caps = await resolve_caps(db, None, project_id)
        project_enclave = await enclave_mtd(db, project_id)
        project_rows.append(
            {
                "project_id": str(project_id),
                "name": project.name,
                "usd": float(usd),
                "enclave_usd": float(project_enclave) if project_enclave is not None else None,
                "budget_usd": float(project_caps.project_cap) if project_caps.project_cap else None,
            }
        )
    total_enclave = await enclave_mtd(db)
    return {
        "month": start.strftime("%Y-%m"),
        "mtd_usd": float(total),
        "enclave_usd": float(total_enclave) if total_enclave is not None else None,
        "cap_usd": float(caps.user_cap) if caps.user_cap else None,
        "at_cap": caps.at_cap,
        "projects": project_rows,
    }
