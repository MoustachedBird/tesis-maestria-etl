"""Con qué nombres se buscó a cada negocio: `veredictos.nombres_buscados`

Desde v6 un agente planificador decide, antes de recolectar, con qué nombres
se busca al negocio en línea: el nombre del DENUE cambia entre cortes
(artículos, orden, errores de captura, un cambio de nombre real) y buscar
solo el de gold lo pierde cuando en línea aparece de otra forma. La decisión
cambia todo lo que se consulta, así que se guarda con el veredicto:

    veredictos.nombres_buscados   {nombres, razonamiento, automatica}

NULL en los veredictos de v5 y anteriores, que buscaban solo el de gold.
Solo aditiva e idempotente; no tira nada.

Revision ID: b0013nombres_buscados
Revises: b0012fuentes_y_especialidades
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0013nombres_buscados"
down_revision: str | Sequence[str] | None = "b0012fuentes_y_especialidades"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Agrega `veredictos.nombres_buscados` (JSONB)."""
    from src.shared.enums import Layer

    schema = Layer.VERIFICACION.value
    op.execute(f"ALTER TABLE {schema}.veredictos ADD COLUMN IF NOT EXISTS nombres_buscados JSONB")
    op.execute(
        f"COMMENT ON COLUMN {schema}.veredictos.nombres_buscados IS "
        "'{nombres, razonamiento, automatica}: con que nombres se busco al negocio y por que, "
        "segun el planificador (v6+). NULL en veredictos anteriores'"
    )
    print(f"   {schema}: veredictos.nombres_buscados agregada")


def downgrade() -> None:
    """Quita la columna (lo que tenga no es derivable)."""
    from src.shared.enums import Layer

    op.execute(
        f"ALTER TABLE {Layer.VERIFICACION.value}.veredictos DROP COLUMN IF EXISTS nombres_buscados"
    )
