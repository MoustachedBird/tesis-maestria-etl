"""normalize domain enums to catalog tables

Normalizacion de dominios (cierra la serie N1/N2): los 4 dominios de tipo de
`establecimientos` que vivian como tipos ENUM de DuckDB (tipo_vial_type,
tipo_asent_type, tipounieco_type, per_ocu_type) pasan a ser tablas de
catalogo relacionadas + columnas VARCHAR:

  - Crea y siembra `denue_tipos_vialidad` (39), `denue_tipos_asentamiento`
    (45) y `denue_tipos_unidad_economica` (3, con `descripcion` semantica),
    generadas desde los enums de src_etl/db/enums.py (fuente unica --
    mismo patron que denue_estratos_personal en bbe078d563ac).
  - Convierte las 4 columnas ENUM de establecimientos a VARCHAR (los datos
    se preservan: el cast enum->varchar es el valor textual).
  - Elimina (best-effort) los 4 tipos ENUM huerfanos.

SIN FK fisico de establecimientos hacia estos catalogos A PROPOSITO: cada
FOREIGN KEY en DuckDB crea un indice ART implicito sobre las columnas
referenciantes; sobre 77M filas eso hace las escrituras masivas ~1000x mas
lentas (medido en este repo con ix_estab_h3_9). Ademas DuckDB no soporta
ALTER TABLE ... ADD CONSTRAINT, asi que anadir FKs exigiria recrear la
tabla completa (~22GB). La integridad la garantizan (a) la validacion
fail-fast del ingest (EstablecimientoWriter.validate_integrity contra
DOMAIN_CATALOGS, derivado de los mismos enums) y (b) los relationships del
ORM (models.py). Los 3 FKs de geografia existentes se conservan.

LIMITACIONES DE DUCKDB MANEJADAS AQUI (verificadas empiricamente):
  1. ALTER COLUMN ... SET DATA TYPE falla si la tabla tiene CUALQUIER
     indice de usuario -> se capturan (SQL impreso como respaldo), se
     sueltan, se convierte y se recrean. En el Silver real solo existe
     ix_estab_h3_9 (los analiticos se perdieron en 7427c9bfdb0d); su
     recreacion sobre 77M filas toma varios minutos.
  2. El DROP INDEX debe estar COMMITEADO antes del ALTER (dentro de la
     misma transaccion DuckDB sigue viendo el indice) -> commit intermedio
     a nivel DBAPI + nueva transaccion (_commit_and_restart).
  3. DROP TYPE tras el ALTER falla en la MISMA sesion (dependencia
     fantasma en el catalogo en memoria) -> se intenta y, si falla, se
     imprime el comando manual; los tipos huerfanos son inofensivos.

Idempotente: detecta si cada columna sigue siendo ENUM antes de convertir
y usa IF EXISTS / try-except en drops; re-ejecutar es seguro.

Revision ID: ae66f32a9bed
Revises: 7427c9bfdb0d
Create Date: 2026-07-17 14:55:14.165573

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "ae66f32a9bed"
down_revision: str | Sequence[str] | None = "7427c9bfdb0d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Columnas de dominio y su antiguo tipo ENUM de DuckDB.
DOMAIN_COLUMNS = {
    "tipo_vial": "tipo_vial_type",
    "tipo_asent": "tipo_asent_type",
    "tipounieco": "tipounieco_type",
    "per_ocu": "per_ocu_type",
}

FACT_TABLE = "establecimientos"


def _fact_table_exists(conn) -> bool:
    """True si la tabla de hechos existe (en replays desde cero si existe:
    la crea la migracion base cbc954c59583 antes que esta)."""
    return (
        conn.execute(
            sa.text(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :t"
            ),
            {"t": FACT_TABLE},
        ).scalar()
        > 0
    )


def _enum_columns_present(conn) -> list[str]:
    """Columnas de DOMAIN_COLUMNS cuyo tipo actual sigue siendo ENUM."""
    rows = conn.execute(
        sa.text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": FACT_TABLE},
    ).fetchall()
    types = {r[0]: (r[1] or "") for r in rows}
    return [c for c in DOMAIN_COLUMNS if types.get(c, "").upper().startswith("ENUM")]


def _commit_and_restart(conn) -> None:
    """Commit intermedio a nivel DBAPI + nueva transaccion.

    DuckDB no aplica el DROP INDEX al catalogo visible del ALTER mientras
    ambos vivan en la misma transaccion abierta ("an index depends on it").
    Alembic envuelve la migracion en una transaccion SQLAlchemy; commitear
    por debajo y abrir una nueva deja el estado consistente (el commit
    final de Alembic cierra la transaccion nueva). Verificado en scratch.
    """
    raw = conn.connection.dbapi_connection
    raw.commit()
    raw.begin()


def _capture_user_indexes(conn) -> list[tuple[str, str]]:
    """(nombre, sql) de todos los indices de usuario de la tabla de hechos."""
    return [
        (r[0], r[1])
        for r in conn.execute(
            sa.text(
                "SELECT index_name, sql FROM duckdb_indexes() "
                "WHERE table_name = :t AND sql IS NOT NULL"
            ),
            {"t": FACT_TABLE},
        ).fetchall()
    ]


def upgrade() -> None:
    """Catalogos de dominio + columnas ENUM -> VARCHAR + drop de tipos."""
    from src_etl.loaders.dominios import load_seeds_sqlalchemy

    conn = op.get_bind()

    # --- 1. Crear y sembrar los catalogos de dominio ---
    stats = load_seeds_sqlalchemy(conn, truncate=True)
    print(f"   Catalogos de dominio sembrados: {stats}")

    if not _fact_table_exists(conn):
        print(f"   {FACT_TABLE} no existe aun; solo catalogos sembrados.")
        return

    # --- 2. Convertir columnas ENUM -> VARCHAR ---
    enum_cols = _enum_columns_present(conn)
    if not enum_cols:
        print("   Columnas de dominio ya son VARCHAR; nada que convertir.")
    else:
        # DuckDB bloquea ALTER COLUMN TYPE si la tabla tiene indices de
        # usuario: capturar (respaldo impreso), soltar, convertir, recrear.
        indexes = _capture_user_indexes(conn)
        if indexes:
            print(f"   Respaldo de {len(indexes)} indice(s) a recrear tras el ALTER:")
            for name, sql in indexes:
                print(f"      {sql};")
            for name, _sql in indexes:
                conn.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
            # DuckDB exige el DROP INDEX commiteado antes del ALTER
            _commit_and_restart(conn)

        for col in enum_cols:
            conn.execute(
                sa.text(f"ALTER TABLE {FACT_TABLE} ALTER COLUMN {col} SET DATA TYPE VARCHAR")
            )
            print(f"   {FACT_TABLE}.{col}: ENUM -> VARCHAR")

        for name, sql in indexes:
            print(f"   Recreando indice {name} (77M filas puede tomar minutos)...")
            conn.execute(sa.text(sql))

    # --- 3. Drop de los tipos ENUM huerfanos (best-effort) ---
    pending_types = []
    for type_name in DOMAIN_COLUMNS.values():
        try:
            conn.execute(sa.text(f"DROP TYPE IF EXISTS {type_name}"))
        except Exception:
            # Dependencia fantasma de la misma sesion (limitacion DuckDB):
            # el tipo ya no lo usa ninguna columna, pero el catalogo en
            # memoria conserva la dependencia hasta reabrir la base.
            pending_types.append(type_name)

    if pending_types:
        print(
            "   AVISO: tipos ENUM huerfanos sin borrar (dependencia fantasma "
            "de esta sesion; son inofensivos). Para limpiarlos, en una "
            "conexion NUEVA ejecutar: "
            + " ".join(f"DROP TYPE IF EXISTS {t};" for t in pending_types)
        )


def downgrade() -> None:
    """Recrea los tipos ENUM, revierte las columnas y borra los catalogos."""
    from src_etl.utils.denue_schema import DOMAIN_CATALOGS

    conn = op.get_bind()

    if _fact_table_exists(conn):
        # Recrear tipos ENUM desde el dominio actual
        for col, type_name in DOMAIN_COLUMNS.items():
            exists = conn.execute(
                sa.text("SELECT COUNT(*) FROM duckdb_types() WHERE type_name = :n"),
                {"n": type_name},
            ).scalar()
            if not exists:
                vals = ", ".join(f"'{v}'" for v in DOMAIN_CATALOGS[col])
                conn.execute(sa.text(f"CREATE TYPE {type_name} AS ENUM ({vals})"))

        # Mismo baile de indices que en upgrade()
        indexes = _capture_user_indexes(conn)
        for name, _sql in indexes:
            conn.execute(sa.text(f'DROP INDEX IF EXISTS "{name}"'))
        if indexes:
            _commit_and_restart(conn)

        for col, type_name in DOMAIN_COLUMNS.items():
            conn.execute(
                sa.text(
                    f"ALTER TABLE {FACT_TABLE} ALTER COLUMN {col} SET DATA TYPE {type_name}"
                )
            )

        for _name, sql in indexes:
            conn.execute(sa.text(sql))

    for table in (
        "denue_tipos_vialidad",
        "denue_tipos_asentamiento",
        "denue_tipos_unidad_economica",
    ):
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
