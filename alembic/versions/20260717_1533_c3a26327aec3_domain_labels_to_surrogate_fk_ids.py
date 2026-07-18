"""domain labels to surrogate fk ids

Relaciones REALES por id (decision del usuario 2026-07-17, supersede la
decision "sin FK fisico" de ae66f32a9bed):

  - Los 4 catalogos de dominio pasan a llevar surrogate key
    `id INTEGER PRIMARY KEY` (posicion 1-based en su enum, ver
    `enums.domain_id`), label UNIQUE y columna `descripcion` con el
    significado de cada valor (seeds generados en `loaders/dominios.py`;
    estratos ya materializa min/max/categoria).
  - `establecimientos` referencia los catalogos con FOREIGN KEYs REALES:
    `tipo_vial_id`, `tipo_asent_id`, `tipounieco_id`, `per_ocu_id`
    (INTEGER) + FK en `codigo_act` hacia el catalogo SCIAN (verificado:
    0 codigos huerfanos en el Silver real). Los 3 FKs de geografia se
    conservan.

Como DuckDB NO soporta ALTER TABLE ... ADD CONSTRAINT, los FKs solo pueden
nacer con la tabla:

  - Tablas de hechos NUEVAS (writer / create_all) ya nacen con el DDL
    final (`loaders.denue.ESTABLECIMIENTOS_DDL` / modelo ORM).
  - `establecimientos` VACIA (cadena fresca de migraciones): esta
    migracion la reconstruye al DDL final (drop + create, costo cero).
  - `establecimientos` CON DATOS (Silver real, 77M filas): la reconstruye
    el script ONE-TIME `scripts/reorganize_domain_ids.py`
    (`make reorganize-domain-ids`), que copia los datos convirtiendo
    labels -> ids via JOIN, verifica paridad y hace swap. Esta migracion
    solo prepara catalogos e imprime la instruccion.

Costo asumido y documentado (AGENTS.md): cada FK implica lookups y
mantenimiento de indice en DuckDB durante escrituras masivas; el usuario
prefirio integridad referencial fisica.

Revision ID: c3a26327aec3
Revises: ae66f32a9bed
Create Date: 2026-07-17 15:33:59.914621

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c3a26327aec3"
down_revision: str | Sequence[str] | None = "ae66f32a9bed"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

FACT_TABLE = "establecimientos"

# Catalogos que deben tener el esquema nuevo (id surrogate + descripcion/
# atributos). Columna que delata el esquema viejo: falta `id`.
CATALOG_TABLES = [
    "denue_estratos_personal",
    "denue_tipos_vialidad",
    "denue_tipos_asentamiento",
    "denue_tipos_unidad_economica",
]


def _table_exists(conn, table: str) -> bool:
    """True si la tabla existe en el esquema actual."""
    return (
        conn.execute(
            sa.text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :t"),
            {"t": table},
        ).scalar()
        > 0
    )


def _columns(conn, table: str) -> set[str]:
    """Nombres de columna de una tabla."""
    rows = conn.execute(
        sa.text("SELECT column_name FROM information_schema.columns WHERE table_name = :t"),
        {"t": table},
    ).fetchall()
    return {r[0] for r in rows}


def upgrade() -> None:
    """Catalogos con id surrogate + rebuild de la tabla de hechos (si vacia)."""
    from src_etl.loaders.dominios import load_seeds_sqlalchemy as load_dominios
    from src_etl.loaders.estratos import load_seeds_sqlalchemy as load_estratos

    conn = op.get_bind()

    # --- 1. Catalogos al esquema nuevo (id + descripcion) ---
    # Nada los referencia por FK todavia en bases con esquema viejo, asi
    # que el drop es seguro; en cadenas frescas ya nacen con id y solo se
    # verifica el contenido (skip-if-match dentro de los loaders).
    for table in CATALOG_TABLES:
        if _table_exists(conn, table) and "id" not in _columns(conn, table):
            conn.execute(sa.text(f"DROP TABLE {table}"))
            print(f"   {table}: esquema viejo (sin id) -> recreada")
    load_estratos(conn, truncate=True)
    load_dominios(conn, truncate=True)
    print("   Catalogos de dominio con surrogate ids listos.")

    # --- 2. Tabla de hechos ---
    if not _table_exists(conn, FACT_TABLE):
        print(f"   {FACT_TABLE} no existe; el writer/create_all la creara con FKs.")
        return

    cols = _columns(conn, FACT_TABLE)
    if "per_ocu_id" in cols and "per_ocu" not in cols:
        print(f"   {FACT_TABLE} ya esta en el esquema relacional por ids. Nada que hacer.")
        return

    n_rows = conn.execute(sa.text(f"SELECT COUNT(*) FROM {FACT_TABLE}")).scalar()
    if n_rows == 0:
        # Rebuild instantaneo: sin datos no hay nada que convertir.
        from src_etl.db.models import Establecimiento

        conn.execute(sa.text(f"DROP TABLE {FACT_TABLE}"))
        Establecimiento.__table__.create(conn)
        print(f"   {FACT_TABLE} (vacia) reconstruida con FKs reales + indices del modelo.")
    else:
        print(
            f"   {FACT_TABLE} tiene {n_rows:,} filas: la conversion labels->ids es el\n"
            "   paso ONE-TIME aparte (reconstruye la tabla con FKs):\n"
            "       make reorganize-domain-ids\n"
            "   (o: uv run python scripts/reorganize_domain_ids.py)"
        )


def downgrade() -> None:
    """Best-effort: solo revierte esquemas sin datos convertidos.

    Una vez corrido el script de reorganizacion sobre datos reales, el
    downgrade automatico NO reconstruye los labels (la informacion sigue
    integra via JOIN, pero revertir 77M filas es una reorganizacion
    inversa consciente, no un downgrade).
    """
    conn = op.get_bind()

    if _table_exists(conn, FACT_TABLE):
        cols = _columns(conn, FACT_TABLE)
        n_rows = conn.execute(sa.text(f"SELECT COUNT(*) FROM {FACT_TABLE}")).scalar()
        if "per_ocu_id" in cols and n_rows > 0:
            raise RuntimeError(
                "Downgrade no soportado: establecimientos ya esta convertida a ids "
                "con datos. Revertir requiere una reorganizacion inversa manual."
            )
        if "per_ocu_id" in cols and n_rows == 0:
            # Rebuild vacio al esquema de labels de ae66f32a9bed
            conn.execute(sa.text(f"DROP TABLE {FACT_TABLE}"))
            conn.execute(
                sa.text("""
                CREATE TABLE establecimientos (
                    id VARCHAR PRIMARY KEY,
                    clee VARCHAR, clee_propagado_radio VARCHAR, clee_propagado_h3 VARCHAR,
                    nom_estab VARCHAR, raz_social VARCHAR, codigo_act VARCHAR,
                    tipo_vial VARCHAR, tipo_asent VARCHAR, tipounieco VARCHAR, per_ocu VARCHAR,
                    nom_vial VARCHAR, tipo_v_e_1 VARCHAR, nom_v_e_1 VARCHAR,
                    tipo_v_e_2 VARCHAR, nom_v_e_2 VARCHAR, tipo_v_e_3 VARCHAR,
                    nom_v_e_3 VARCHAR, numero_ext VARCHAR, letra_ext VARCHAR,
                    edificio VARCHAR, edificio_e VARCHAR, numero_int VARCHAR,
                    letra_int VARCHAR, nomb_asent VARCHAR, nom_cencom VARCHAR,
                    num_local VARCHAR, cod_postal VARCHAR,
                    cve_ent VARCHAR, cve_mun VARCHAR, cve_loc VARCHAR,
                    ageb VARCHAR, manzana VARCHAR, telefono VARCHAR,
                    correoelec VARCHAR, www VARCHAR,
                    latitud DOUBLE, longitud DOUBLE,
                    fecha_alta_str VARCHAR, fecha_alta_date DATE, period DATE NOT NULL,
                    h3_cell_9 BIGINT, h3_cell_5 BIGINT,
                    FOREIGN KEY (cve_ent) REFERENCES denue_entidades(cve_ent),
                    FOREIGN KEY (cve_ent, cve_mun)
                        REFERENCES denue_municipios(cve_ent, cve_mun),
                    FOREIGN KEY (cve_ent, cve_mun, cve_loc)
                        REFERENCES denue_localidades(cve_ent, cve_mun, cve_loc)
                )
                """)
            )
            print("   establecimientos (vacia) revertida al esquema de labels.")

    # Catalogos: volver al esquema con label como PK (sin id) reseedeando
    # desde los mismos enums.
    from src_etl.loaders.dominios import TABLES, build_rows
    from src_etl.loaders.estratos import build_rows as build_estratos

    for table, label_col, _enum, _disp in TABLES:
        conn.execute(sa.text(f"DROP TABLE IF EXISTS {table}"))
        if table == "denue_tipos_unidad_economica":
            conn.execute(
                sa.text(
                    f"CREATE TABLE {table} ({label_col} VARCHAR PRIMARY KEY, "
                    "descripcion VARCHAR NOT NULL)"
                )
            )
            for row in build_rows()[table]:
                conn.execute(
                    sa.text(f"INSERT INTO {table} VALUES (:label, :descripcion)"),
                    {"label": row[label_col], "descripcion": row["descripcion"]},
                )
        else:
            conn.execute(sa.text(f"CREATE TABLE {table} ({label_col} VARCHAR PRIMARY KEY)"))
            for row in build_rows()[table]:
                conn.execute(
                    sa.text(f"INSERT INTO {table} VALUES (:label)"), {"label": row[label_col]}
                )

    conn.execute(sa.text("DROP TABLE IF EXISTS denue_estratos_personal"))
    conn.execute(
        sa.text(
            "CREATE TABLE denue_estratos_personal (per_ocu VARCHAR PRIMARY KEY, "
            "empleados_min INTEGER NOT NULL, empleados_max INTEGER NOT NULL, "
            "categoria VARCHAR NOT NULL)"
        )
    )
    for row in build_estratos():
        conn.execute(
            sa.text(
                "INSERT INTO denue_estratos_personal VALUES "
                "(:per_ocu, :empleados_min, :empleados_max, :categoria)"
            ),
            {k: v for k, v in row.items() if k != "id"},
        )
