"""Health/readiness endpoints + the build-info surface (S16-06)."""

import subprocess
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from sqlalchemy import text

from app.core.auth import get_current_user, require_admin_security_if_admin
from app.core.config import get_settings
from app.core.db import engine

router = APIRouter(tags=["health"])
# Mounted under /api/v1 (unlike /healthz): build info flows through the
# authenticated proxy like any other product surface (S16-06).
meta_router = APIRouter(prefix="/meta", tags=["meta"])


@router.get("/healthz")
async def healthz() -> dict:
    checks: dict[str, str] = {}
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover
        checks["database"] = f"error: {type(exc).__name__}"
    status = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
    return {"status": status, "checks": checks}


@lru_cache
def _code_migration_head() -> str | None:
    """Newest migration shipped IN THIS IMAGE (alembic script directory)."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        ini = Path(__file__).resolve().parents[2] / "alembic.ini"
        script = ScriptDirectory.from_config(Config(str(ini)))
        return script.get_current_head()
    except Exception:  # pragma: no cover — alembic layout changed
        return None


@lru_cache
def _local_git_sha() -> str | None:
    """Dev fallback only — images carry GIT_SHA baked at build (S16-06)."""
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short=12", "HEAD"],
                capture_output=True, text=True, timeout=3, check=True,
            ).stdout.strip()
            or None
        )
    except Exception:  # noqa: BLE001 — no git in the container, by design
        return None


@meta_router.get("/features")
async def features(user=Depends(require_admin_security_if_admin)) -> dict:
    """Per-user feature availability (S18 dark launch).

    `studio` — user may open /studio (flag on, or admin previewing dark);
    `studio_dark` — admin is seeing it with the flag still off (banner state).
    Business personas never get the studio [D17].
    """
    from app.services.platform_settings import get_controls

    controls = await get_controls()
    flag_on = bool(controls.feature_flags.get("studio_enabled", False))
    is_admin = user.role == "admin"
    # Admins are always eligible (dark-launch preview), whatever their persona.
    eligible = user.persona != "business" or is_admin
    return {
        "studio_enabled": flag_on,
        "studio": eligible and (flag_on or is_admin),
        "studio_dark": eligible and is_admin and not flag_on,
        # C1 connector consumption (external-import-connectors spec): ships
        # dark; flipping it activates the SHALL-use-connector spec signal.
        "connectors_enabled": bool(
            controls.feature_flags.get("connectors_enabled", False)
        ),
    }


@meta_router.get("/build-info", dependencies=[Depends(get_current_user)])
async def build_info() -> dict:
    """What exactly is running (S16-06): git SHA + build time baked into the
    image, migration head the code ships vs the head the DATABASE is at —
    a mismatch is the first thing to check in any incident."""
    settings = get_settings()
    db_head: str | None = None
    try:
        async with engine.connect() as conn:
            row = await conn.execute(text("SELECT version_num FROM alembic_version"))
            db_head = row.scalar_one_or_none()
    except Exception:  # noqa: BLE001 — table absent (fresh env/tests)
        db_head = None
    code_head = _code_migration_head()
    return {
        "git_sha": settings.git_sha or _local_git_sha(),
        "build_time": settings.build_time or None,
        "environment": settings.environment,
        "migration_head_code": code_head,
        "migration_head_db": db_head,
        "migrations_in_sync": bool(code_head and db_head and code_head == db_head),
    }
