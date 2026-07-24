"""sesnsp crime incidence (catalogo de delitos + hecho mensual)

Segunda migracion de la cadena consolidada: incidencia delictiva municipal
del SESNSP, la fuente con la que se cruza la supervivencia de negocios.

  - `sesnsp_delitos`: jerarquia bien_juridico -> tipo -> subtipo ->
    modalidad, con surrogate id append-only (el hecho la referencia por FK).
  - `sesnsp_incidencia_municipal`: carpetas de investigacion por municipio x
    delito x mes, en formato largo. Comparte las llaves geograficas de DENUE
    (FKs a entidades y municipios), por eso depende de la migracion core.

El catalogo de delitos SE SIEMBRA aqui desde
`data/seeds/seed_sesnsp_delitos.parquet`, que es su fuente de verdad (mismo
patron que los catalogos DENUE). El transform de cada corte anual NO
descubre delitos: valida que los del CSV existan ya en el catalogo y los
enlaza por FK, igual que un establecimiento se valida contra SCIAN o contra
el tipo de asentamiento. Un delito nuevo se da de alta en el parquet.

Revision ID: a2000sesnsp
Revises: a1000denue
Create Date: 2026-01-01 00:02:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a2000sesnsp"
down_revision: str | Sequence[str] | None = "a1000denue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SESNSP_TABLES = ["sesnsp_delitos", "sesnsp_incidencia_municipal"]
"""Catalogo antes que el hecho: el FK delito_id le apunta."""


def _tables(metadata, names):
    """Objetos Table del metadata para los nombres dados, en ese orden."""
    return [metadata.tables[n] for n in names]


def upgrade() -> None:
    """Crea las tablas SESNSP y siembra el catalogo de delitos."""
    from src_etl.core.config import settings
    from src_etl.db.base import Base
    from src_etl.db.models import SesnspIncidenciaMunicipal  # noqa: F401
    from src_etl.loaders.sesnsp import load_seeds_sqlalchemy as load_delitos

    conn = op.get_bind()
    Base.metadata.create_all(bind=conn, tables=_tables(Base.metadata, SESNSP_TABLES))
    print(f"   Esquema SESNSP creado: {', '.join(SESNSP_TABLES)}")

    print(f"   Delitos: {load_delitos(conn, settings.SEEDS_DIR)}")


def downgrade() -> None:
    """Borra el hecho y luego el catalogo."""
    from src_etl.db.base import Base
    from src_etl.db.models import SesnspIncidenciaMunicipal  # noqa: F401

    conn = op.get_bind()
    Base.metadata.drop_all(bind=conn, tables=_tables(Base.metadata, SESNSP_TABLES))
