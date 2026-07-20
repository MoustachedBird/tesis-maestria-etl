"""add SESNSP municipal crime incidence tables

New data source: SESNSP incidencia delictiva del fuero comun a nivel
municipal (carpetas de investigacion por municipio x delito x mes, 2015+),
downloaded from datos.gob.mx and normalized to long format.

Creates:
- `sesnsp_delitos`: crime hierarchy catalog (bien juridico -> tipo ->
  subtipo -> modalidad) with append-only surrogate ids materialized in
  data/seeds/seed_sesnsp_delitos.parquet.
- `sesnsp_incidencia_municipal`: monthly fact table sharing the DENUE
  geographic keys (FK to denue_entidades/denue_municipios) so business
  survival can be joined with crime incidence per municipality over time.

Also syncs `denue_municipios` with the geography seed, which now includes
the 8 synthetic SESNSP municipalities (cve_mun 998 "NO ESPECIFICADO" /
999 "OTROS MUNICIPIOS") required to satisfy the composite geography FK.

Both tables are created WITH their constraints (DuckDB cannot ALTER TABLE
ADD CONSTRAINT). The delitos catalog is seeded by the transform stage
(transformers/sesnsp.py) from the seed parquet, not here: the parquet is
first generated from the initial CSV cut.

Revision ID: f1a2b3c4d5e6
Revises: c3a26327aec3
Create Date: 2026-07-19 21:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a2b3c4d5e6"
down_revision: str | Sequence[str] | None = "c3a26327aec3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _table_exists(conn, table: str) -> bool:
    """True si la tabla ya existe en el esquema actual."""
    return (
        conn.execute(
            sa.text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
            {"table": table},
        ).scalar()
        > 0
    )


def upgrade() -> None:
    """Create SESNSP tables and sync the municipios seed (idempotent)."""
    from src_etl.core.config import settings
    from src_etl.loaders.sesnsp import SESNSP_DELITOS_DDL, SESNSP_INCIDENCIA_DDL

    conn = op.get_bind()

    if not _table_exists(conn, "sesnsp_delitos"):
        conn.execute(sa.text(SESNSP_DELITOS_DDL))
        print("   sesnsp_delitos created.")
    if not _table_exists(conn, "sesnsp_incidencia_municipal"):
        conn.execute(sa.text(SESNSP_INCIDENCIA_DDL))
        print("   sesnsp_incidencia_municipal created.")

    # Insert seed municipios rows missing from the DB (the 8 synthetic
    # SESNSP keys on existing databases). Append-only, never deletes.
    import polars as pl

    seed_path = settings.SEEDS_DIR / "seed_municipios.parquet"
    seed = pl.read_parquet(seed_path)
    existing = {
        (r[0], r[1])
        for r in conn.execute(sa.text("SELECT cve_ent, cve_mun FROM denue_municipios")).fetchall()
    }
    missing = [row for row in seed.to_dicts() if (row["cve_ent"], row["cve_mun"]) not in existing]
    if missing:
        conn.execute(
            sa.text(
                "INSERT INTO denue_municipios (cve_ent, cve_mun, municipio) "
                "VALUES (:cve_ent, :cve_mun, :municipio)"
            ),
            missing,
        )
        print(f"   denue_municipios synced: +{len(missing)} rows from seed.")


def downgrade() -> None:
    """Drop the SESNSP tables (fact first: it references the catalog).

    The synthetic municipios rows are NOT removed: they are plain catalog
    rows and other data may reference them.
    """
    conn = op.get_bind()
    conn.execute(sa.text("DROP TABLE IF EXISTS sesnsp_incidencia_municipal"))
    conn.execute(sa.text("DROP TABLE IF EXISTS sesnsp_delitos"))
