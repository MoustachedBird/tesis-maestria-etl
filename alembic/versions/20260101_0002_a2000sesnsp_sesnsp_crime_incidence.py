"""sesnsp crime incidence (catalogo de delitos + hecho mensual)

Segunda migracion de la cadena consolidada: incidencia delictiva municipal
del SESNSP, la fuente con la que se cruza la supervivencia de negocios.

  - `sesnsp_delitos`: jerarquia bien_juridico -> tipo -> subtipo ->
    modalidad, con surrogate id append-only (el hecho la referencia por FK).
  - `sesnsp_incidencia_municipal`: carpetas de investigacion por municipio x
    delito x mes, en formato largo. Comparte las llaves geograficas de DENUE
    (FKs a entidades y municipios), por eso depende de la migracion core.

El catalogo de delitos NO se siembra aqui: sus filas salen del propio CSV
del SESNSP durante el transform, que las materializa en
`data/seeds/seed_sesnsp_delitos.parquet`.

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
    """Crea el catalogo de delitos y la tabla de incidencia mensual."""
    from src_etl.db.base import Base
    from src_etl.db.models import SesnspIncidenciaMunicipal  # noqa: F401

    conn = op.get_bind()
    Base.metadata.create_all(bind=conn, tables=_tables(Base.metadata, SESNSP_TABLES))
    print(f"   Esquema SESNSP creado: {', '.join(SESNSP_TABLES)}")


def downgrade() -> None:
    """Borra el hecho y luego el catalogo."""
    from src_etl.db.base import Base
    from src_etl.db.models import SesnspIncidenciaMunicipal  # noqa: F401

    conn = op.get_bind()
    Base.metadata.drop_all(bind=conn, tables=_tables(Base.metadata, SESNSP_TABLES))
