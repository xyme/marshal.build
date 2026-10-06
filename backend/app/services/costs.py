"""Cost dashboard aggregates over model_invocations (FSD §4.6.5, S4-01).

On-request SQL aggregation is deliberate for Alpha volume; S5's real-time
tracking swaps the internals of THIS module while the API shape (dashboard +
group_by pivots) stays put. "Model spend" only — sandbox infra spend arrives
with ISB cost surfacing ≥S5 (labeled in the UI).
"""

import calendar
from collections import defaultdict
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ModelInvocation, PlatformSettings, Project, User


def _period_bounds(period: str | None) -> tuple[datetime, datetime, int, int]:
    """(start, end, days_in_month, elapsed_days) for YYYY-MM (default: now)."""
    now = datetime.now(UTC)
    if period:
        year, month = int(period[:4]), int(period[5:7])
    else:
        year, month = now.year, now.month
    start = datetime(year, month, 1, tzinfo=UTC)
    days_in_month = calendar.monthrange(year, month)[1]
    end = start + timedelta(days=days_in_month)
    if start <= now < end:
        elapsed = max((now - start).days + 1, 1)
    elif now >= end:
        elapsed = days_in_month
    else:  # future period
        elapsed = 1
    return start, end, days_in_month, elapsed


async def dashboard(db: AsyncSession, period: str | None) -> dict:
    start, end, days_in_month, elapsed = _period_bounds(period)
    in_period = (ModelInvocation.created_at >= start, ModelInvocation.created_at < end)

    totals = (
        await db.execute(
            select(
                func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                func.count(),
                func.coalesce(func.sum(ModelInvocation.input_tokens), 0),
                func.coalesce(func.sum(ModelInvocation.output_tokens), 0),
            ).where(*in_period)
        )
    ).one()
    total_usd, calls, tokens_in, tokens_out = (
        float(totals[0]), totals[1], totals[2], totals[3],
    )
    unpriced = (
        await db.execute(
            select(func.count())
            .select_from(ModelInvocation)
            .where(*in_period, ModelInvocation.cost_usd.is_(None))
        )
    ).scalar_one()

    # Daily series: Python bucketing keeps it portable across PG/sqlite (test parity)
    rows = await db.execute(
        select(ModelInvocation.created_at, ModelInvocation.cost_usd).where(
            ModelInvocation.created_at >= start,
            ModelInvocation.created_at < end,
            ModelInvocation.cost_usd.is_not(None),
        )
    )
    daily: dict[str, float] = defaultdict(float)
    for created_at, cost in rows.all():
        daily[created_at.date().isoformat()] += float(cost)
    series = [
        {"date": (start + timedelta(days=i)).date().isoformat(),
         "usd": round(daily.get((start + timedelta(days=i)).date().isoformat(), 0.0), 6)}
        for i in range(days_in_month)
    ]

    by_model_rows = await db.execute(
        select(ModelInvocation.model_id, func.coalesce(func.sum(ModelInvocation.cost_usd), 0))
        .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
        .group_by(ModelInvocation.model_id)
        .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
    )
    by_model = [
        {"model_id": model_id, "usd": round(float(usd), 6)}
        for model_id, usd in by_model_rows.all()
    ]

    top = await _pivot_users(db, start, end, limit=10)

    settings_row = await db.get(PlatformSettings, 1)
    budget = (settings_row.cost or {}).get("platform_budget_usd") if settings_row else None

    # Enclave infra spend for the period (S12 R4) — None while CE is dark
    enclave_usd = None
    from app.core.config import get_settings as _get_settings

    if _get_settings().cost_explorer_enabled:
        from app.models import SandboxSpend

        enclave_total = (
            await db.execute(
                select(func.coalesce(func.sum(SandboxSpend.usd), 0)).where(
                    SandboxSpend.date >= start, SandboxSpend.date < end
                )
            )
        ).scalar_one()
        enclave_usd = round(float(enclave_total), 2)

    return {
        "period": start.strftime("%Y-%m"),
        "total_usd": round(total_usd, 6),
        "enclave_usd": enclave_usd,
        "calls": calls,
        "input_tokens": tokens_in,
        "output_tokens": tokens_out,
        "unpriced_calls": unpriced,
        "budget_usd": float(budget) if budget is not None else None,
        "projected_usd": round(total_usd / elapsed * days_in_month, 2),
        "daily": series,
        "by_model": by_model,
        "top_spenders": top,
    }


async def _pivot_users(db: AsyncSession, start, end, limit: int | None = None) -> list[dict]:
    stmt = (
        select(
            ModelInvocation.user_id,
            func.count(func.distinct(ModelInvocation.project_id)),
            func.count(),
            func.coalesce(
                func.sum(ModelInvocation.input_tokens + ModelInvocation.output_tokens), 0
            ),
            func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
        )
        .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
        .group_by(ModelInvocation.user_id)
        .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = (await db.execute(stmt)).all()
    user_ids = {r[0] for r in rows if r[0]}
    emails: dict = {}
    if user_ids:
        pairs = await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))
        emails = dict(pairs.all())
    return [
        {
            "user_id": str(uid) if uid else None,
            "email": emails.get(uid, "system"),
            "projects": proj_count,
            "calls": calls,
            "tokens": int(tokens),
            "usd": round(float(usd), 6),
        }
        for uid, proj_count, calls, tokens, usd in rows
    ]


async def breakdown(db: AsyncSession, period: str | None, group_by: str) -> dict:
    start, end, _days, _elapsed = _period_bounds(period)
    if group_by == "user":
        items = await _pivot_users(db, start, end)
    elif group_by == "project":
        rows = (
            await db.execute(
                select(
                    ModelInvocation.project_id,
                    func.count(),
                    func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                )
                .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
                .group_by(ModelInvocation.project_id)
                .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
            )
        ).all()
        project_ids = {r[0] for r in rows if r[0]}
        names: dict = {}
        if project_ids:
            pairs = await db.execute(
                select(Project.id, Project.name).where(Project.id.in_(project_ids))
            )
            names = dict(pairs.all())
        items = [
            {
                "project_id": str(pid) if pid else None,
                "name": names.get(pid, "(no project)"),
                "calls": calls,
                "usd": round(float(usd), 6),
            }
            for pid, calls, usd in rows
        ]
    elif group_by == "team":
        # Model spend rolled to the owning project's team (the 5 Sep
        # team-budget dimension); "(personal)" = projects without a team.
        from app.models import Team

        rows = (
            await db.execute(
                select(
                    ModelInvocation.project_id,
                    func.count(),
                    func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                )
                .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
                .group_by(ModelInvocation.project_id)
            )
        ).all()
        project_ids = {r[0] for r in rows if r[0]}
        project_teams: dict = {}
        if project_ids:
            project_teams = dict(
                (
                    await db.execute(
                        select(Project.id, Project.team_id).where(
                            Project.id.in_(project_ids)
                        )
                    )
                ).all()
            )
        team_ids = {t for t in project_teams.values() if t}
        team_names: dict = {}
        if team_ids:
            team_names = dict(
                (
                    await db.execute(select(Team.id, Team.name).where(Team.id.in_(team_ids)))
                ).all()
            )
        buckets: dict[str, dict] = {}
        for pid, calls, usd in rows:
            team = team_names.get(project_teams.get(pid)) or "(personal)"
            bucket = buckets.setdefault(team, {"team": team, "calls": 0, "usd": 0.0})
            bucket["calls"] += calls
            bucket["usd"] += float(usd)
        items = [
            {"team": b["team"], "calls": b["calls"], "usd": round(b["usd"], 6)}
            for b in sorted(buckets.values(), key=lambda b: -b["usd"])
        ]
    elif group_by == "model":
        rows = (
            await db.execute(
                select(
                    ModelInvocation.model_id,
                    func.count(),
                    func.coalesce(
                        func.sum(ModelInvocation.input_tokens + ModelInvocation.output_tokens), 0
                    ),
                    func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                )
                .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
                .group_by(ModelInvocation.model_id)
                .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
            )
        ).all()
        items = [
            {"model_id": model_id, "calls": calls, "tokens": int(tokens), "usd": round(float(usd), 6)}
            for model_id, calls, tokens, usd in rows
        ]
    elif group_by == "purpose":
        # chat|requirements|design|tasks|title|classification|risk|codegen (S8 R7.4)
        rows = (
            await db.execute(
                select(
                    ModelInvocation.purpose,
                    func.count(),
                    func.coalesce(
                        func.sum(ModelInvocation.input_tokens + ModelInvocation.output_tokens), 0
                    ),
                    func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
                )
                .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
                .group_by(ModelInvocation.purpose)
                .order_by(func.coalesce(func.sum(ModelInvocation.cost_usd), 0).desc())
            )
        ).all()
        items = [
            {"purpose": purpose, "calls": calls, "tokens": int(tokens), "usd": round(float(usd), 6)}
            for purpose, calls, tokens, usd in rows
        ]
    else:  # day
        rows = (
            await db.execute(
                select(ModelInvocation.created_at, ModelInvocation.cost_usd, ModelInvocation.id)
                .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
            )
        ).all()
        buckets: dict[str, dict] = defaultdict(lambda: {"calls": 0, "usd": 0.0})
        for created_at, cost, _id in rows:
            bucket = buckets[created_at.date().isoformat()]
            bucket["calls"] += 1
            bucket["usd"] += float(cost) if cost is not None else 0.0
        items = [
            {"date": date, "calls": b["calls"], "usd": round(b["usd"], 6)}
            for date, b in sorted(buckets.items())
        ]
    return {"period": start.strftime("%Y-%m"), "group_by": group_by, "items": items}


# ---------------------------------------------------------------- S16-03
# Chargeback/showback: one report that answers "who spent what, on which
# project, in which team" — model spend always, Enclave infra spend folded in
# when Cost Explorer is live [D3]. This is the Preview pricing substrate.


async def chargeback(db: AsyncSession, period: str | None, months: int = 1) -> dict:
    """Cost allocation by (user, project, team) + monthly rollups.

    Allocation rule: model spend attributes to the CALLER (user_id on the
    invocation); Enclave spend attributes to the project OWNER — the owner
    holds the lease, collaborators do not pay for shared infrastructure.
    """
    from app.core.config import get_settings
    from app.models import SandboxSpend, Team

    months = max(1, min(months, 12))
    start, end, _days, _elapsed = _period_bounds(period)
    ce_enabled = get_settings().cost_explorer_enabled

    # ---- allocation rows for the requested period
    model_rows = (
        await db.execute(
            select(
                ModelInvocation.user_id,
                ModelInvocation.project_id,
                func.count(),
                func.coalesce(func.sum(ModelInvocation.cost_usd), 0),
            )
            .where(ModelInvocation.created_at >= start, ModelInvocation.created_at < end)
            .group_by(ModelInvocation.user_id, ModelInvocation.project_id)
        )
    ).all()

    cells: dict[tuple, dict] = {}
    for user_id, project_id, calls, usd in model_rows:
        cell = cells.setdefault(
            (user_id, project_id),
            {"user_id": user_id, "project_id": project_id,
             "calls": 0, "model_usd": 0.0, "enclave_usd": 0.0},
        )
        cell["calls"] += calls
        cell["model_usd"] += float(usd)

    if ce_enabled:
        enclave_rows = (
            await db.execute(
                select(
                    SandboxSpend.project_id,
                    func.coalesce(func.sum(SandboxSpend.usd), 0),
                )
                .where(SandboxSpend.date >= start, SandboxSpend.date < end)
                .group_by(SandboxSpend.project_id)
            )
        ).all()
        owners: dict = {}
        enclave_project_ids = {pid for pid, _ in enclave_rows if pid}
        if enclave_project_ids:
            owners = dict(
                (
                    await db.execute(
                        select(Project.id, Project.user_id).where(
                            Project.id.in_(enclave_project_ids)
                        )
                    )
                ).all()
            )
        for project_id, usd in enclave_rows:
            owner = owners.get(project_id)
            cell = cells.setdefault(
                (owner, project_id),
                {"user_id": owner, "project_id": project_id,
                 "calls": 0, "model_usd": 0.0, "enclave_usd": 0.0},
            )
            cell["enclave_usd"] += float(usd)

    # ---- decorate with names (email, project, team)
    user_ids = {c["user_id"] for c in cells.values() if c["user_id"]}
    project_ids = {c["project_id"] for c in cells.values() if c["project_id"]}
    emails: dict = {}
    projects: dict = {}
    team_names: dict = {}
    if user_ids:
        emails = dict(
            (await db.execute(select(User.id, User.email).where(User.id.in_(user_ids)))).all()
        )
    if project_ids:
        projects = {
            pid: (name, team_id)
            for pid, name, team_id in (
                await db.execute(
                    select(Project.id, Project.name, Project.team_id).where(
                        Project.id.in_(project_ids)
                    )
                )
            ).all()
        }
        team_ids = {t for _, t in projects.values() if t}
        if team_ids:
            team_names = dict(
                (await db.execute(select(Team.id, Team.name).where(Team.id.in_(team_ids)))).all()
            )

    items = []
    for cell in cells.values():
        name, team_id = projects.get(cell["project_id"], ("(no project)", None))
        total = cell["model_usd"] + cell["enclave_usd"]
        items.append(
            {
                "user_id": str(cell["user_id"]) if cell["user_id"] else None,
                "email": emails.get(cell["user_id"], "system"),
                "project_id": str(cell["project_id"]) if cell["project_id"] else None,
                "project": name,
                "team": team_names.get(team_id) if team_id else None,
                "calls": cell["calls"],
                "model_usd": round(cell["model_usd"], 6),
                "enclave_usd": round(cell["enclave_usd"], 2) if ce_enabled else None,
                "total_usd": round(total, 6),
            }
        )
    items.sort(key=lambda i: -i["total_usd"])

    # ---- per-team subtotal (the S15-02 dimension this report rides on)
    by_team: dict[str, dict] = {}
    for item in items:
        key = item["team"] or "(personal)"
        agg = by_team.setdefault(
            key, {"team": key, "model_usd": 0.0, "enclave_usd": 0.0, "total_usd": 0.0}
        )
        agg["model_usd"] = round(agg["model_usd"] + item["model_usd"], 6)
        agg["enclave_usd"] = round(agg["enclave_usd"] + (item["enclave_usd"] or 0.0), 2)
        agg["total_usd"] = round(agg["total_usd"] + item["total_usd"], 6)

    # ---- monthly rollups walking back from the period
    rollups = []
    year, month = int(start.strftime("%Y")), int(start.strftime("%m"))
    for _ in range(months):
        m_start = datetime(year, month, 1, tzinfo=UTC)
        m_end = m_start + timedelta(days=calendar.monthrange(year, month)[1])
        model_total = (
            await db.execute(
                select(func.coalesce(func.sum(ModelInvocation.cost_usd), 0)).where(
                    ModelInvocation.created_at >= m_start, ModelInvocation.created_at < m_end
                )
            )
        ).scalar_one()
        enclave_total = 0.0
        if ce_enabled:
            enclave_total = float(
                (
                    await db.execute(
                        select(func.coalesce(func.sum(SandboxSpend.usd), 0)).where(
                            SandboxSpend.date >= m_start, SandboxSpend.date < m_end
                        )
                    )
                ).scalar_one()
            )
        rollups.append(
            {
                "period": m_start.strftime("%Y-%m"),
                "model_usd": round(float(model_total), 6),
                "enclave_usd": round(enclave_total, 2) if ce_enabled else None,
                "total_usd": round(float(model_total) + enclave_total, 6),
            }
        )
        month -= 1
        if month == 0:
            year, month = year - 1, 12

    return {
        "period": start.strftime("%Y-%m"),
        "cost_explorer_enabled": ce_enabled,
        "items": items,
        "by_team": sorted(by_team.values(), key=lambda t: -t["total_usd"]),
        "monthly": rollups,
    }


async def team_costs(db: AsyncSession, period: str | None) -> dict:
    """Per-team budget vs spend for the cost dashboard (5 Sep 2026).

    Reuses the chargeback aggregation (names are tenant-unique, so the name
    join is safe) and joins every team — budgeted teams appear even with
    zero spend, and unattributed spend surfaces as the "(personal)" row.
    Visibility only: team budgets are not enforced at the model seam.
    """
    from app.models import Team

    report = await chargeback(db, period, months=1)
    ce_enabled = report["cost_explorer_enabled"]
    spend_by_team = {t["team"]: t for t in report["by_team"]}

    teams = (await db.execute(select(Team))).scalars().all()
    items = []
    for team in teams:
        spend = spend_by_team.get(team.name, {})
        budget = float(team.budget_usd) if team.budget_usd is not None else None
        total = float(spend.get("total_usd", 0.0))
        items.append(
            {
                "team_id": str(team.id),
                "team": team.name,
                "status": team.status,
                "budget_usd": budget,
                "model_usd": float(spend.get("model_usd", 0.0)),
                "enclave_usd": float(spend.get("enclave_usd", 0.0)) if ce_enabled else None,
                "total_usd": total,
                "pct_used": (
                    round(total / budget * 100, 1) if budget and budget > 0 else None
                ),
            }
        )
    personal = spend_by_team.get("(personal)")
    if personal:
        items.append(
            {
                "team_id": None,
                "team": "(personal)",
                "status": "—",
                "budget_usd": None,
                "model_usd": personal["model_usd"],
                "enclave_usd": personal["enclave_usd"] if ce_enabled else None,
                "total_usd": personal["total_usd"],
                "pct_used": None,
            }
        )
    # Most-consumed budgets first; unbudgeted rows follow, by spend.
    items.sort(
        key=lambda i: (
            -(i["pct_used"] if i["pct_used"] is not None else -1.0),
            -i["total_usd"],
        )
    )
    return {
        "period": report["period"],
        "cost_explorer_enabled": ce_enabled,
        "items": items,
    }


def chargeback_csv(report: dict) -> str:
    """Flatten the allocation rows for the exportable report (S16-03 AC)."""
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["period", "email", "project", "team", "calls", "model_usd", "enclave_usd", "total_usd"]
    )
    for item in report["items"]:
        writer.writerow(
            [
                report["period"],
                item["email"],
                item["project"],
                item["team"] or "",
                item["calls"],
                f"{item['model_usd']:.6f}",
                "" if item["enclave_usd"] is None else f"{item['enclave_usd']:.2f}",
                f"{item['total_usd']:.6f}",
            ]
        )
    return buf.getvalue()
