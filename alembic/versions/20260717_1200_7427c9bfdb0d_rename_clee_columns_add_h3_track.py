"""rename clee_master and add H3 resolution column (self-descriptive names)

Establishes the three-column identity design with self-descriptive names
(user feedback: "clee_master" says nothing about the method):

  - clee:                  original INEGI CLEE (untouched source)
  - clee_propagado_radio:  propagated/generated via tolerance-radius logic
                           (BallTree, SPATIAL_RADIUS_M) - RENAMED from the
                           old `clee_master` (data preserved: RENAME COLUMN
                           is metadata-only, no row rewrite)
  - clee_propagado_h3:     propagated/generated via H3 cell blocking
                           (Fase 4) - NEW column

Keeping both resolved columns enables side-by-side parity analysis
(thesis methodology) and a safe rollout of the H3 method.

Deliberately NO index on clee_propagado_h3: ART maintenance makes massive
resolution writes ~1000x slower (measured 2026-07-17: ~2K rows/s with
index vs 5M rows/s without). Index AFTER resolution if analysis needs it.

Idempotent: safe to re-run at any state.

KNOWN DuckDB CAVEAT: on rare occasions the transactional-DDL wrapper of
alembic/duckdb_engine can crash natively (observed only on a
downgrade->re-upgrade cycle; the plain forward upgrade tested clean).
The index definitions are printed BEFORE dropping as a recovery aid.
Manual fallback (same sequence, outside a transaction, verified working):

    import duckdb
    conn = duckdb.connect("data/silver/denue.duckdb")
    rows = conn.execute("SELECT index_name, sql FROM duckdb_indexes()"
                        " WHERE table_name='establecimientos'").fetchall()
    for n, _ in rows: conn.execute(f'DROP INDEX "{n}"')
    conn.execute("ALTER TABLE establecimientos RENAME COLUMN clee_master"
                 " TO clee_propagado_radio")
    conn.execute("ALTER TABLE establecimientos ADD COLUMN clee_propagado_h3 VARCHAR")
    for _, s in rows: conn.execute(s.replace('clee_master', 'clee_propagado_radio'))
    conn.execute("UPDATE alembic_version SET version_num='7427c9bfdb0d'")

Revision ID: 7427c9bfdb0d
Revises: bbe078d563ac
Create Date: 2026-07-17 12:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "7427c9bfdb0d"
down_revision: str | Sequence[str] | None = "bbe078d563ac"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "establecimientos"


def _existing_columns(conn) -> set[str]:
    result = conn.execute(
        sa.text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = :t"
        ),
        {"t": TABLE},
    )
    return {row[0] for row in result}


def _run_without_indexes(conn, alter_fn, rename_map: dict[str, str]) -> None:
    """
    Ejecuta operaciones ALTER tolerando la limitacion de DuckDB.

    DuckDB bloquea ALTER TABLE RENAME/DROP COLUMN sobre tablas con
    CUALQUIER indice (DependencyException), aunque el indice no toque la
    columna afectada. Aqui: capturar definiciones desde duckdb_indexes(),
    soltar indices, ejecutar alter_fn() y recrearlos (renombrando en su
    SQL las columnas de rename_map si algun indice las referia).

    NOTA de costo: en el Silver real (~77M filas) recrear los ~13 indices
    ART toma decenas de minutos. Costo unico de esta migracion.
    En PostgreSQL alter_fn() corre directo, sin tocar indices.
    """
    if conn.dialect.name != "duckdb":
        alter_fn()
        return

    idx_rows = conn.execute(
        sa.text(
            "SELECT index_name, sql FROM duckdb_indexes() WHERE table_name = :t"
        ),
        {"t": TABLE},
    ).fetchall()

    # Imprimir definiciones ANTES de soltar: si algo fallara a mitad,
    # estas lineas permiten recrear los indices a mano.
    for index_name, index_sql in idx_rows:
        print(f"   [respaldo indice] {index_sql}")
    for index_name, _ in idx_rows:
        conn.execute(sa.text(f'DROP INDEX "{index_name}"'))
    if idx_rows:
        print(f"   {len(idx_rows)} indices soltados temporalmente.")

    alter_fn()

    for index_name, index_sql in idx_rows:
        recreated = index_sql
        for old, new in rename_map.items():
            recreated = recreated.replace(old, new)
        conn.execute(sa.text(recreated))
    if idx_rows:
        print(f"   {len(idx_rows)} indices recreados.")


def upgrade() -> None:
    """Rename clee_master -> clee_propagado_radio; add clee_propagado_h3."""
    conn = op.get_bind()
    cols = _existing_columns(conn)

    if "clee_propagado_radio" not in cols:
        if "clee_master" not in cols:
            raise RuntimeError(
                f"{TABLE} has neither clee_propagado_radio nor clee_master: "
                "unexpected schema state, aborting."
            )
        _run_without_indexes(
            conn,
            lambda: op.alter_column(
                TABLE, "clee_master", new_column_name="clee_propagado_radio"
            ),
            {"clee_master": "clee_propagado_radio"},
        )
        print("   clee_master renamed to clee_propagado_radio.")
    else:
        print("   clee_propagado_radio already present, rename skipped.")

    if "clee_propagado_h3" not in cols:
        op.add_column(
            TABLE,
            sa.Column(
                "clee_propagado_h3",
                sa.String(),
                nullable=True,
                comment=(
                    "Resolved CLEE via H3 cell blocking "
                    "(comparison track vs clee_propagado_radio)"
                ),
            ),
        )
        print("   clee_propagado_h3 added.")
    else:
        print("   clee_propagado_h3 already present, skipped.")


def downgrade() -> None:
    """Drop clee_propagado_h3 and restore the clee_master name."""
    conn = op.get_bind()
    cols = _existing_columns(conn)

    def _alter():
        if "clee_propagado_h3" in cols:
            op.drop_column(TABLE, "clee_propagado_h3")
        if "clee_propagado_radio" in cols and "clee_master" not in cols:
            op.alter_column(
                TABLE, "clee_propagado_radio", new_column_name="clee_master"
            )

    _run_without_indexes(conn, _alter, {"clee_propagado_radio": "clee_master"})
