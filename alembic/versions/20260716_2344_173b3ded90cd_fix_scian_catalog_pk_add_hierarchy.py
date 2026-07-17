"""fix scian catalog pk and add hierarchy columns

Normalization N1: `denue_actividades_economicas` had a composite PK
(codigo_act, nombre_act), a 2NF violation since nombre_act depends
functionally on codigo_act (it also allowed the same code with two names,
the ambiguity that forces fuzzy matching in the CLEE activity gate).

This migration recreates the table with:
  - PK on codigo_act alone
  - nombre_act as a NOT NULL attribute
  - sector / subsector / rama columns materializing the SCIAN hierarchy
    (2/3/4-digit prefixes of the 6-digit code), queryable in plain SQL
  - index on sector

DuckDB compatibility: DuckDB does not support ALTER TABLE DROP CONSTRAINT,
so the PK change is done by create-new -> copy -> drop-old -> rename,
which works on both DuckDB and PostgreSQL. The migration aborts loudly if
the existing data actually contains one code with multiple names.

Idempotent: skipped if the table already has the `sector` column.

Revision ID: 173b3ded90cd
Revises: 74b0a3dad572
Create Date: 2026-07-16 23:44:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "173b3ded90cd"
down_revision: str | Sequence[str] | None = "74b0a3dad572"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "denue_actividades_economicas"
TMP_TABLE = f"{TABLE}_new"


def _column_exists(conn, table: str, column: str) -> bool:
    result = conn.execute(
        sa.text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = :table AND column_name = :column
        """),
        {"table": table, "column": column},
    )
    return result.scalar() > 0


def upgrade() -> None:
    """Recreate the SCIAN catalog with codigo_act PK + hierarchy columns."""
    conn = op.get_bind()

    if _column_exists(conn, TABLE, "sector"):
        print(f"   {TABLE} already normalized (sector column present), skipping.")
        return

    # Abort if the composite PK let in a code with more than one name:
    # deduplicating silently here would hide a data problem.
    duplicates = conn.execute(
        sa.text(f"""
            SELECT codigo_act FROM {TABLE}
            GROUP BY codigo_act HAVING COUNT(DISTINCT nombre_act) > 1
        """)
    ).fetchall()
    if duplicates:
        codes = ", ".join(row[0] for row in duplicates[:10])
        raise RuntimeError(
            f"{TABLE} has {len(duplicates)} codigo_act values with multiple "
            f"nombre_act (e.g. {codes}). Fix the data before normalizing the PK."
        )

    conn.execute(sa.text(f"DROP TABLE IF EXISTS {TMP_TABLE}"))
    conn.execute(
        sa.text(f"""
            CREATE TABLE {TMP_TABLE} (
                codigo_act VARCHAR PRIMARY KEY,
                nombre_act VARCHAR NOT NULL,
                sector VARCHAR NOT NULL,
                subsector VARCHAR NOT NULL,
                rama VARCHAR NOT NULL
            )
        """)
    )
    conn.execute(
        sa.text(f"""
            INSERT INTO {TMP_TABLE}
            SELECT codigo_act, nombre_act,
                   substr(codigo_act, 1, 2),
                   substr(codigo_act, 1, 3),
                   substr(codigo_act, 1, 4)
            FROM {TABLE}
        """)
    )
    conn.execute(sa.text(f"DROP TABLE {TABLE}"))
    conn.execute(sa.text(f"ALTER TABLE {TMP_TABLE} RENAME TO {TABLE}"))
    op.create_index("ix_denue_actividades_economicas_sector", TABLE, ["sector"])

    count = conn.execute(sa.text(f"SELECT COUNT(*) FROM {TABLE}")).scalar()
    print(f"   {TABLE} normalized: {count:,} rows, PK=codigo_act, hierarchy materialized.")


def downgrade() -> None:
    """Restore the original composite-PK catalog (drops hierarchy columns)."""
    conn = op.get_bind()

    conn.execute(sa.text(f"DROP TABLE IF EXISTS {TMP_TABLE}"))
    conn.execute(
        sa.text(f"""
            CREATE TABLE {TMP_TABLE} (
                codigo_act VARCHAR,
                nombre_act VARCHAR,
                PRIMARY KEY (codigo_act, nombre_act)
            )
        """)
    )
    conn.execute(sa.text(f"INSERT INTO {TMP_TABLE} SELECT codigo_act, nombre_act FROM {TABLE}"))
    conn.execute(sa.text(f"DROP TABLE {TABLE}"))
    conn.execute(sa.text(f"ALTER TABLE {TMP_TABLE} RENAME TO {TABLE}"))
