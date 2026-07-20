"""add entidades_economicas dimension (N3)

One row per resolved business identity (`clee_propagado_radio`): most
recent period attributes + observed lifespan (primer/ultimo periodo,
n_periodos). Survival/mortality unit of analysis for the thesis; joins
with sesnsp_incidencia_municipal via (cve_ent, cve_mun) + period.

The migration only creates the (empty) table for schema parity; content
is built/rebuilt by `loaders/entidades.py:build_entidades_economicas()`
(`make build-entidades`) AFTER each CLEE resolution run — NEW_* synthetic
ids are not stable across --force re-resolutions, so the dimension is
always derived, never migrated.

NOT applied to the real Silver automatically (regla de oro): run
`make db-migrate` in a controlled step, ideally after the radio
re-resolution finishes (the current clee_propagado_radio values are
about to be regenerated).

Revision ID: b2c3d4e5f6a7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-20 10:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b2c3d4e5f6a7"
down_revision: str | Sequence[str] | None = "f1a2b3c4d5e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the empty dimension table if missing (idempotent)."""
    from src_etl.loaders.entidades import ENTIDADES_DDL, TABLE_NAME

    conn = op.get_bind()
    exists = (
        conn.execute(
            sa.text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
            {"table": TABLE_NAME},
        ).scalar()
        > 0
    )
    if not exists:
        conn.execute(sa.text(ENTIDADES_DDL))
        print(f"   {TABLE_NAME} created (empty; build with `make build-entidades`).")


def downgrade() -> None:
    """Drop the dimension (fully derived: rebuildable at any time)."""
    from src_etl.loaders.entidades import TABLE_NAME

    conn = op.get_bind()
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
