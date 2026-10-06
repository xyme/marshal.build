"""Model ↔ migration parity guard.

The suite builds its schema from the MODELS (`Base.metadata.create_all`), while
Postgres gets it from the MIGRATIONS. A column present in a model but missing
from the migrations therefore passes every test and fails only in production —
which is exactly what happened in S15 (`team_members.tenant_id`, found by the
live team drill).

This is a text-level heuristic over the migration files, not a real schema
diff (a true diff needs Postgres, which the suite does not have). It is aimed at
the specific, repeatable mistake: adding a mixin column to a new table and
forgetting it in the create_table.
"""

import re
from pathlib import Path

import pytest

from app.models import Base
from app.models.entities import TenantMixin, TimestampMixin

MIGRATIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"

# Columns that arrive via mixins — the ones most easily forgotten, because the
# model never mentions them explicitly.
MIXIN_COLUMNS = {
    TenantMixin: ["tenant_id"],
    TimestampMixin: ["created_at", "updated_at"],
}


def _migration_text() -> str:
    return "\n".join(p.read_text() for p in sorted(MIGRATIONS.glob("*.py")))


def _tables_with_mixin(mixin) -> list[str]:
    return [
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if issubclass(mapper.class_, mixin)
    ]


@pytest.mark.parametrize("mixin,columns", list(MIXIN_COLUMNS.items()))
def test_mixin_columns_exist_in_migrations(mixin, columns):
    """Every table using a mixin must have that mixin's columns in migrations."""
    text = _migration_text()
    q = "[\"']"  # migrations mix single and double quotes across sprints
    missing: list[str] = []
    for table in sorted(set(_tables_with_mixin(mixin))):
        if not re.search(rf"{q}{table}{q}", text):
            # Table predates the tracked chain or is test-only — nothing to assert.
            continue
        for column in columns:
            # The column is fine if it is created WITH the table (search the
            # create_table block only, so a later table's column cannot satisfy
            # an earlier one) or added by name afterwards. The block ends at a
            # closing paren indented ≤4 spaces: nested multiline sa.Column(...)
            # closers sit at 8 (learned in S16 — the first version stopped at
            # ANY line-start paren and truncated bodies with multiline columns).
            block = re.search(
                rf"create_table\(\s*{q}{table}{q}(?P<body>.*?)\n\s{{0,4}}\)", text, re.S
            )
            created_with_table = block and re.search(
                rf"{q}{column}{q}", block.group("body")
            )
            added_later = re.search(
                rf"add_column\(\s*{q}{table}{q},\s*sa\.Column\(\s*{q}{column}{q}", text, re.S
            )
            # Third accepted form: a retrofit loop over a table list, which is
            # how S12 added tenant_id to the 19 pre-existing tables. Satisfied
            # when a file both lists this table and adds the column in a loop
            # over a variable (not a literal table name).
            added_in_loop = any(
                re.search(rf"{q}{table}{q}", src)
                and re.search(rf"add_column\(\s*\w+,\s*sa\.Column\(\s*{q}{column}{q}", src, re.S)
                for src in (p.read_text() for p in sorted(MIGRATIONS.glob("*.py")))
            )
            if not (created_with_table or added_later or added_in_loop):
                missing.append(f"{table}.{column}")
    assert missing == [], (
        f"Columns in the models but never created by a migration: {missing}. "
        "The test suite builds schema from models, so this only breaks Postgres."
    )


def test_every_model_table_appears_in_migrations():
    """A new table must ship with a migration that creates it."""
    text = _migration_text()
    q = "[\"']"
    tables = {mapper.class_.__tablename__ for mapper in Base.registry.mappers}
    missing = [
        t
        for t in sorted(tables)
        if not re.search(rf"create_table\(\s*{q}{t}{q}", text)
    ]
    assert missing == [], f"Model tables with no create_table migration: {missing}"
