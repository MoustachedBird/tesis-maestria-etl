"""add personnel strata catalog (denue_estratos_personal)

Normalization N2: materializes the INEGI employee-count ranges (per_ocu)
as a catalog table with numeric bounds and business-size category, so SME
classification (Micro/Small/Medium/Large) is queryable in plain SQL instead
of living only in the `PersonalOcupado` Python enum.

Rows are generated from the enum at migration time (single source of truth:
`src_etl.loaders.estratos.build_rows()` — same pattern as the seed migration
dff9d0804542, which also imports app code).

Idempotent: creates the table only if missing, reseeds (delete + insert)
so re-runs after enum changes refresh the 8 rows.

Revision ID: bbe078d563ac
Revises: 173b3ded90cd
Create Date: 2026-07-16 23:44:30.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "bbe078d563ac"
down_revision: str | Sequence[str] | None = "173b3ded90cd"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create and seed denue_estratos_personal from the PersonalOcupado enum."""
    from src_etl.loaders.estratos import TABLE_DDL, TABLE_NAME, load_seeds_sqlalchemy

    conn = op.get_bind()

    table_exists = (
        conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"
            ),
            {"table": TABLE_NAME},
        ).scalar()
        > 0
    )
    if not table_exists:
        conn.execute(sa.text(TABLE_DDL.format(table=TABLE_NAME)))

    stats = load_seeds_sqlalchemy(conn, truncate=True)
    print(f"   {TABLE_NAME} seeded: {stats['personnel_strata']} strata.")


def downgrade() -> None:
    """Drop the personnel strata catalog."""
    from src_etl.loaders.estratos import TABLE_NAME

    conn = op.get_bind()
    conn.execute(sa.text(f"DROP TABLE IF EXISTS {TABLE_NAME}"))
