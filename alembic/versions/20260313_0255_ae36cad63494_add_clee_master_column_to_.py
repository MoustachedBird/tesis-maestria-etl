"""add clee_master column to establecimientos

Adds the `clee_master` column used by the CLEE Resolution stage to store
resolved entity identifiers. This column enables tracking establishments
across temporal DENUE snapshots.

Values:
  - Original CLEE (copied from `clee` when available)
  - Temporal CLEE: NEW_{REGION_PREFIX}-{uuid_hex_12} for new entities

The migration is idempotent: it checks if the column already exists before
adding it (supports environments where it was added via ALTER TABLE).

Revision ID: ae36cad63494
Revises: dff9d0804542
Create Date: 2026-03-13 02:55:23.210530

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ae36cad63494"
down_revision: str | Sequence[str] | None = "dff9d0804542"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add clee_master column to establecimientos (idempotent)."""
    conn = op.get_bind()

    # Check if column already exists (idempotent for environments where
    # it was added via manual ALTER TABLE before this migration existed)
    result = conn.execute(
        sa.text("""
            SELECT COUNT(*) FROM information_schema.columns
            WHERE table_name = 'establecimientos' AND column_name = 'clee_master'
        """)
    )
    column_exists = result.scalar() > 0

    if not column_exists:
        op.add_column(
            "establecimientos",
            sa.Column(
                "clee_master",
                sa.String(),
                nullable=True,
                comment="Resolved entity identifier from CLEE resolution stage",
            ),
        )


def downgrade() -> None:
    """Remove clee_master column from establecimientos."""
    op.drop_column("establecimientos", "clee_master")
