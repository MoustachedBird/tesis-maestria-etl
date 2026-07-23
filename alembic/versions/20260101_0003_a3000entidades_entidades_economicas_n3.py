"""entidades economicas (dimension N3)

Tercera y ultima migracion de la cadena consolidada: la dimension contra la
que se calculan supervivencia y mortalidad en la tesis.

`entidades_economicas` colapsa `establecimientos` (una fila por negocio POR
PERIODO) a una fila por identidad resuelta, con el rango de vida observado.
Nace vacia: es un artefacto DERIVADO que reconstruye por completo
`make build-entidades` tras cada corrida de resolucion CLEE. Deliberadamente
sin FKs a los catalogos (la tabla de hechos ya garantiza esos dominios y la
dimension se reconstruye en bloque).

Revision ID: a3000entidades
Revises: a2000sesnsp
Create Date: 2026-01-01 00:03:00.000000

"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3000entidades"
down_revision: str | Sequence[str] | None = "a2000sesnsp"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "entidades_economicas"
"""Unica tabla de esta migracion (dimension N3)."""


def upgrade() -> None:
    """Crea la dimension vacia."""
    from src_etl.db.base import Base
    from src_etl.db.models import EntidadEconomica  # noqa: F401

    conn = op.get_bind()
    Base.metadata.create_all(bind=conn, tables=[Base.metadata.tables[TABLE]])
    print(f"   {TABLE} creada (vacia; poblar con `make build-entidades`)")


def downgrade() -> None:
    """Borra la dimension."""
    from src_etl.db.base import Base
    from src_etl.db.models import EntidadEconomica  # noqa: F401

    conn = op.get_bind()
    Base.metadata.drop_all(bind=conn, tables=[Base.metadata.tables[TABLE]])
