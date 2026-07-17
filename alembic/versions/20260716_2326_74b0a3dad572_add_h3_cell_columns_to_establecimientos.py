"""add h3 cell columns to establecimientos

Adds the H3 spatial index columns used by the CLEE Resolution stage to
replace the per-pass BallTree pre-filter with cell-based blocking:

  - h3_cell_9: H3 cell index at resolution 9 (~174m avg edge). Blocking key
    for candidate generation via grid_disk join + exact haversine post-filter.
  - h3_cell_5: H3 cell index at resolution 5 (~8.5km avg edge). Coarse key
    for spatial partitioning of the reference KB.

Both are nullable BIGINT (valid H3 indexes fit in signed 64-bit; NULL when
the row lacks coordinates). Values are backfilled in a separate step.

The migration is idempotent: it checks whether each column/index already
exists before adding it (supports environments where they were added via
manual ALTER TABLE).

Revision ID: 74b0a3dad572
Revises: ae36cad63494
Create Date: 2026-07-16 23:26:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "74b0a3dad572"
down_revision: str | Sequence[str] | None = "ae36cad63494"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

H3_COLUMNS = {
    "h3_cell_9": "H3 cell index at resolution 9 (~174m edge) for spatial blocking",
    "h3_cell_5": "H3 cell index at resolution 5 (~8.5km edge) for spatial partitioning",
}


def _existing_columns(conn) -> set[str]:
    result = conn.execute(
        sa.text("""
            SELECT column_name FROM information_schema.columns
            WHERE table_name = 'establecimientos'
        """)
    )
    return {row[0] for row in result}


def upgrade() -> None:
    """Add h3_cell_9 / h3_cell_5 columns and blocking index (idempotent)."""
    conn = op.get_bind()
    existing = _existing_columns(conn)

    for column_name, comment in H3_COLUMNS.items():
        if column_name not in existing:
            op.add_column(
                "establecimientos",
                sa.Column(
                    column_name,
                    sa.BigInteger(),
                    nullable=True,
                    comment=comment,
                ),
            )

    existing_indexes = {ix["name"] for ix in sa.inspect(conn).get_indexes("establecimientos")}
    if "ix_estab_h3_9" not in existing_indexes:
        op.create_index("ix_estab_h3_9", "establecimientos", ["h3_cell_9"])


def downgrade() -> None:
    """Remove H3 columns and index from establecimientos."""
    op.drop_index("ix_estab_h3_9", table_name="establecimientos")
    for column_name in H3_COLUMNS:
        op.drop_column("establecimientos", column_name)
