"""Crea silver.clee_radio y silver.clee_h3

El resultado de la resolución de identidad sale de `silver.establecimientos`
y pasa a una tabla por método. Ver `docs/separacion_ingest_clee.md`.

Esta revisión es **puramente aditiva**: crea dos tablas vacías y no toca
`silver.establecimientos`, `meta.download_tasks` ni ninguna otra. Los ZIP ya
descargados y su historial siguen intactos, y las columnas
`clee_propagado_radio`/`_h3` siguen en su lugar hasta que ningún lector las
use (eso es el paso 4 del documento).

Revision ID: b0002clee_por_metodo
Revises: b0001drop_normalize_tasks
Create Date: 2026-10-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0002clee_por_metodo"
down_revision: str | Sequence[str] | None = "b0001drop_normalize_tasks"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS = ("clee_radio", "clee_h3")
"""Tablas de resolución a crear, en el schema `silver`."""


def upgrade() -> None:
    """Crea las tablas padre particionadas, sin particiones.

    Las particiones por corte las crea el pipeline al escribir
    (`shared/db/partitions.py`), igual que en `silver.establecimientos`: una
    migración no puede saber qué cortes existen.

    El DDL sale del metadata del ORM y no de `op.create_table` escrito a
    mano, por la misma razón que el baseline: el esquema se declara en un
    solo lugar. `checkfirst` hace la revisión idempotente.

    El import de `src` va aquí adentro: al correr el CLI de alembic el
    `sys.path` lo arma `env.py`, así que a nivel de módulo todavía no es
    importable.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import clee  # noqa: F401  (registra el metadata)
    from src.shared.enums import Layer

    conn = op.get_bind()
    schema = Layer.SILVER.value
    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]

    Base.metadata.create_all(bind=conn, tables=tablas, checkfirst=True)
    print(f"   silver: {len(tablas)} tablas de resolucion CLEE ({', '.join(TABLAS)})")


def downgrade() -> None:
    """Tira las dos tablas con sus particiones.

    Reversible sin pérdida real: su contenido es derivable re-corriendo la
    resolución desde `silver.establecimientos`.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre} CASCADE")
