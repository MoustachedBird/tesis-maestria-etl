"""denue core schema (catalogos + tabla de hechos, forma final)

Primera migracion de la cadena consolidada. Crea el esquema DENUE tal como
debe quedar, SIN pasos intermedios: nada de tipos ENUM de DuckDB que luego
se convierten a tablas, ni columnas que despues se renombran.

  - Catalogos de geografia (entidades / municipios / localidades), SCIAN
    con su jerarquia (sector/subsector/rama) y los 4 catalogos de dominio
    con surrogate id (tipos de vialidad, asentamiento, unidad economica y
    estratos de personal).
  - `establecimientos` con su forma final: PK, FKs REALES por id hacia los
    catalogos de dominio y geografia, columnas `clee_propagado_radio` /
    `clee_propagado_h3` (ya con su nombre definitivo), celdas H3 y los
    indices del modelo.
  - Siembra los catalogos desde los seeds parquet de `data/seeds`.

El esquema se crea desde `src_etl.db.models` (metadata del ORM), que es la
definicion canonica: asi la migracion no puede desincronizarse del modelo.

Revision ID: a1000denue
Revises:
Create Date: 2026-01-01 00:01:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1000denue"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CORE_TABLES = [
    "denue_entidades",
    "denue_municipios",
    "denue_localidades",
    "denue_actividades_economicas",
    "denue_tipos_vialidad",
    "denue_tipos_asentamiento",
    "denue_tipos_unidad_economica",
    "denue_estratos_personal",
    "establecimientos",
]
"""Tablas que crea esta migracion, en el orden en que las declara el modelo
(los catalogos ANTES de la tabla de hechos: sus FKs les apuntan)."""


def _tables(metadata, names):
    """Objetos Table del metadata para los nombres dados, en ese orden."""
    return [metadata.tables[n] for n in names]


def upgrade() -> None:
    """Crea catalogos + tabla de hechos y siembra los catalogos."""
    from src.shared.config import settings
    from src_etl.db.base import Base
    from src_etl.db.models import Establecimiento  # noqa: F401  (registra el metadata)
    from src_etl.loaders.dominios import load_seeds_sqlalchemy as load_dominios
    from src_etl.loaders.estratos import load_seeds_sqlalchemy as load_estratos
    from src_etl.loaders.geography import load_seeds_sqlalchemy as load_geografia
    from src_etl.loaders.scian import load_seeds_sqlalchemy as load_scian

    conn = op.get_bind()
    seeds = settings.SEEDS_DIR

    Base.metadata.create_all(bind=conn, tables=_tables(Base.metadata, CORE_TABLES))
    print(f"   Esquema DENUE creado: {len(CORE_TABLES)} tablas")

    print(f"   Geografia: {load_geografia(conn, seeds)}")
    print(f"   SCIAN: {load_scian(conn, seeds)}")
    print(f"   Dominios: {load_dominios(conn, seeds)}")
    print(f"   Estratos: {load_estratos(conn, seeds)}")


def downgrade() -> None:
    """Borra la tabla de hechos y los catalogos (orden inverso a los FKs)."""
    from src_etl.db.base import Base
    from src_etl.db.models import Establecimiento  # noqa: F401

    conn = op.get_bind()
    Base.metadata.drop_all(bind=conn, tables=_tables(Base.metadata, CORE_TABLES))
