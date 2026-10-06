"""Enclave (sandbox) spend attribution — S5 code, first tested when S13 lit
the Cost Explorer flag live and short leases attributed nothing."""

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from app.models import Lease, Project, SandboxSpend
from app.services import sandbox_costs

pytestmark = pytest.mark.asyncio

ACCOUNT = "123456789012"
# Cost days are anchored to YESTERDAY: fully in the past regardless of run
# time, so "still active" leases (lease_end = now) always span the whole day
# and windows never land in the future.
YESTERDAY = (datetime.now(UTC) - timedelta(days=1)).replace(
    hour=0, minute=0, second=0, microsecond=0
)


def _ce_response(days: list[tuple[str, str]]) -> dict:
    """[(day, usd)] → a CE GetCostAndUsage response grouped by linked account."""
    return {
        "ResultsByTime": [
            {
                "TimePeriod": {"Start": day, "End": day},
                "Groups": [
                    {
                        "Keys": [ACCOUNT],
                        "Metrics": {"UnblendedCost": {"Amount": usd, "Unit": "USD"}},
                    }
                ],
            }
            for day, usd in days
        ]
    }


class _FakeCe:
    def __init__(self, response: dict):
        self._response = response
        self.calls: list[dict] = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


@pytest.fixture
def wire(monkeypatch, db_engine):
    """Point poll_once at the test DB + a fake CE client."""
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    maker = async_sessionmaker(db_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr("app.core.db.SessionLocal", maker)

    def _install(response: dict) -> _FakeCe:
        fake = _FakeCe(response)
        monkeypatch.setattr(sandbox_costs, "_ce_client", lambda: fake)
        return fake

    return _install


async def _lease(db, project, *, activated, terminated=None) -> Lease:
    lease = Lease(
        provider="isb",
        external_lease_id=f"lease-{uuid.uuid4().hex[:8]}",
        aws_account_id=ACCOUNT,
        status="terminated" if terminated else "active",
        project_id=project.id,
        user_id=project.user_id,
        requested_at=activated,
        activated_at=activated,
        terminated_at=terminated,
    )
    db.add(lease)
    await db.commit()
    await db.refresh(lease)
    return lease


@pytest.fixture
async def project(db_session, test_user) -> Project:
    row = Project(user_id=test_user.id, name="Enclave Spend", status="deployed")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def test_short_lease_still_attributed(db_session, project, wire):
    """A 30-minute lease never contains a day boundary — the original
    containment test attributed nothing (live S13 finding)."""
    start = YESTERDAY + timedelta(hours=10)
    lease = await _lease(db_session, project, activated=start, terminated=start + timedelta(minutes=30))
    wire(_ce_response([(YESTERDAY.date().isoformat(), "1.25")]))

    assert await sandbox_costs.poll_once() == 1
    rows = (await db_session.execute(select(SandboxSpend))).scalars().all()
    assert len(rows) == 1
    assert rows[0].lease_id == lease.id
    assert float(rows[0].usd) == 1.25
    assert rows[0].project_id == project.id


async def test_day_goes_to_the_most_overlapping_lease(db_session, project, wire):
    brief = await _lease(
        db_session, project,
        activated=YESTERDAY + timedelta(hours=1), terminated=YESTERDAY + timedelta(hours=2),
    )
    long_lease = await _lease(
        db_session, project,
        activated=YESTERDAY + timedelta(hours=3), terminated=YESTERDAY + timedelta(hours=20),
    )
    wire(_ce_response([(YESTERDAY.date().isoformat(), "9.00")]))

    assert await sandbox_costs.poll_once() == 1
    row = (await db_session.execute(select(SandboxSpend))).scalar_one()
    assert row.lease_id == long_lease.id and row.lease_id != brief.id


async def test_upsert_is_idempotent_and_refreshes_amount(db_session, project, wire):
    # Still-active lease (terminated_at NULL → window runs to "now")
    await _lease(db_session, project, activated=YESTERDAY + timedelta(hours=5))
    day = YESTERDAY.date().isoformat()

    wire(_ce_response([(day, "2.00")]))
    assert await sandbox_costs.poll_once() == 1
    wire(_ce_response([(day, "3.50")]))  # CE revises the figure
    assert await sandbox_costs.poll_once() == 1

    rows = (await db_session.execute(select(SandboxSpend))).scalars().all()
    assert len(rows) == 1 and float(rows[0].usd) == 3.50


async def test_unknown_account_and_no_lease_are_noops(db_session, project, wire):
    # No leases at all → early return, CE never called
    fake = wire(_ce_response([(YESTERDAY.date().isoformat(), "5.00")]))
    assert await sandbox_costs.poll_once() == 0
    assert fake.calls == []

    # Lease exists but the day's costs belong to another account
    await _lease(db_session, project, activated=YESTERDAY + timedelta(hours=2))
    other = _ce_response([(YESTERDAY.date().isoformat(), "5.00")])
    other["ResultsByTime"][0]["Groups"][0]["Keys"] = ["999999999999"]
    wire(other)
    assert await sandbox_costs.poll_once() == 0
    assert (await db_session.execute(select(SandboxSpend))).first() is None


async def test_days_outside_the_lease_window_are_skipped(db_session, project, wire):
    two_days_ago = YESTERDAY - timedelta(days=1)
    await _lease(
        db_session, project,
        activated=YESTERDAY + timedelta(hours=8), terminated=YESTERDAY + timedelta(hours=9),
    )
    wire(_ce_response([
        (two_days_ago.date().isoformat(), "4.00"),  # before the lease existed
        (YESTERDAY.date().isoformat(), "1.00"),
    ]))

    assert await sandbox_costs.poll_once() == 1
    row = (await db_session.execute(select(SandboxSpend))).scalar_one()
    assert float(row.usd) == 1.00
