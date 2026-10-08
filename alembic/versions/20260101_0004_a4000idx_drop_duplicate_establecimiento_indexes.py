"""elimina indices duplicados de establecimientos

Tres de los 16 indices de la tabla de hechos no aportaban nada y cada uno
era un ART persistido que hay que mantener en cada escritura masiva (el
costo que `src_etl/db/indexes.py` documenta como "ordenes de magnitud mas
lentas"):

  - ix_estab_clee   (clee)        duplicaba ix_establecimientos_clee
  - ix_estab_postal (cod_postal)  duplicaba ix_establecimientos_cod_postal

    Ambos nacieron de declarar el indice DOS veces: `index=True` en el
    `mapped_column` (que genera ix_establecimientos_<col>) y ademas un
    `Index(...)` explicito en `__table_args__`. El planner usa uno; el otro
    era espacio y mantenimiento puros.

  - ix_estab_geo_simple ("cve_ent", "cve_mun")

    Es PREFIJO de ix_estab_geo ("cve_ent", "cve_mun", "cve_loc"), que se
    conserva: un indice compuesto sirve los filtros sobre su prefijo, asi
    que el corto era redundante.

Se conservan los dos indices que si se ganan su costo, medido sobre las
82,896,801 filas de Silver: `clee` y `clee_propagado_radio` tienen ~8-9
filas por valor y resuelven busquedas puntuales en ~0.6 ms. En contraste,
un filtro por `period` (~4.1M filas por valor) tarda ~210 ms CON indice y
un escaneo de `nom_estab` SIN indice tarda ~222 ms: cuando el filtro no es
selectivo el ART no aporta, porque DuckDB es columnar y ya descarta bloques
por min/max. Los indices de baja selectividad que quedan (period, cve_ent,
codigo_act, coords, ...) no se tocan aqui: hace falta perfilar consultas
reales antes de quitarlos, y esta migracion se limita a lo indiscutible.

No se pierde informacion: los indices son derivados y declarativos. Si
alguno hiciera falta, se vuelve a declarar en el modelo ORM y
`suspended_indexes()` / `missing_indexes()` lo restituyen.

Revision ID: a4000idx
Revises: a3000entidades
Create Date: 2026-10-06 22:30:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a4000idx"
down_revision: str | Sequence[str] | None = "a3000entidades"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "establecimientos"
"""Tabla de hechos: la unica afectada por esta migracion."""

DROPPED = {
    "ix_estab_clee": ("clee",),
    "ix_estab_postal": ("cod_postal",),
    "ix_estab_geo_simple": ("cve_ent", "cve_mun"),
}
"""Indices eliminados -> columnas que cubrian.

El mapeo existe para que `downgrade()` los pueda reconstruir sin depender
del modelo ORM, del que ya fueron borrados.
"""


def upgrade() -> None:
    """Elimina los tres indices redundantes si existen."""
    conn = op.get_bind()
    existentes = {
        r[0]
        for r in conn.exec_driver_sql(
            "SELECT index_name FROM duckdb_indexes() WHERE table_name = ?",
            (TABLE,),
        ).fetchall()
    }
    for nombre in DROPPED:
        if nombre in existentes:
            op.drop_index(nombre, table_name=TABLE)
            print(f"   {nombre} eliminado")
        else:
            print(f"   {nombre} ya no existia (nada que hacer)")


def downgrade() -> None:
    """Recrea los tres indices redundantes.

    Solo para revertir la cadena de migraciones; recrearlos devuelve el
    costo de mantenimiento que esta migracion elimino.
    """
    for nombre, columnas in DROPPED.items():
        op.create_index(nombre, TABLE, list(columnas))
        print(f"   {nombre} recreado")
