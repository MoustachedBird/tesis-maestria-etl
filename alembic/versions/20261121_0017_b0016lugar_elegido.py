"""Cómo nombró el planificador el lugar: `muestra_negocios.lugar_elegido`

Desde v7 el planificador también decide cómo nombrar el lugar en las
búsquedas (qué de la localidad, el municipio y el estado), y escribe los
domicilios completos. No hay regla: lo razona el modelo, así que se guarda
lo que eligió junto a los demás campos elegidos. El historial por veredicto
ya está en `veredictos.nombres_buscados -> 'lugar'`.

Solo aditiva e idempotente; rellena desde el veredicto más reciente.

Revision ID: b0016lugar_elegido
Revises: b0015alias_de_cada_campo
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0016lugar_elegido"
down_revision: str | Sequence[str] | None = "b0015alias_de_cada_campo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Agrega `lugar_elegido` y la rellena donde se pueda."""
    s = "verificacion"
    op.execute(f"ALTER TABLE {s}.muestra_negocios ADD COLUMN IF NOT EXISTS lugar_elegido TEXT")
    op.execute(f"""
        UPDATE {s}.muestra_negocios mn
        SET lugar_elegido = v.nombres_buscados ->> 'lugar'
        FROM (
            SELECT DISTINCT ON (muestra_id, clee) muestra_id, clee, nombres_buscados
            FROM {s}.veredictos
            WHERE nombres_buscados ? 'lugar'
            ORDER BY muestra_id, clee, verificado_en DESC
        ) v
        WHERE mn.muestra_id = v.muestra_id AND mn.clee = v.clee AND mn.lugar_elegido IS NULL
    """)
    print(f"   {s}.muestra_negocios.lugar_elegido agregada")


def downgrade() -> None:
    """Quita la columna."""
    op.execute("ALTER TABLE verificacion.muestra_negocios DROP COLUMN IF EXISTS lugar_elegido")
