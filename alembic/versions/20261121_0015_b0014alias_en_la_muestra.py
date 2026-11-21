"""Los alias del negocio y los nombres que eligió el planificador, en la muestra

`verificacion.muestra_negocios` gana tres columnas, para ver de un vistazo
con qué nombres apareció cada negocio y con cuáles se le buscó:

    alias                  los nom_estab distintos del DENUE, el mas reciente primero
    nombres_elegidos       los que eligio el planificador la ultima vez (v6+)
    nombres_elegidos_por   el agente (modelo/version) que los eligio

`alias` se rellena aquí para las muestras que ya existen, desde
`silver.establecimientos` por `clee` (indexado): son cientos de negocios,
no los 82.9M. `nombres_elegidos` se rellena desde el veredicto más reciente
que tenga `nombres_buscados`. Solo escribe donde la columna está en NULL,
así que es idempotente; no borra ni cambia nada que ya hubiera.

Revision ID: b0014alias_en_la_muestra
Revises: b0013nombres_buscados
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0014alias_en_la_muestra"
down_revision: str | Sequence[str] | None = "b0013nombres_buscados"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Agrega las tres columnas y rellena lo que se puede derivar."""
    schema = "verificacion"
    op.execute(f"ALTER TABLE {schema}.muestra_negocios ADD COLUMN IF NOT EXISTS alias TEXT[]")
    op.execute(
        f"ALTER TABLE {schema}.muestra_negocios ADD COLUMN IF NOT EXISTS nombres_elegidos TEXT[]"
    )
    op.execute(
        f"ALTER TABLE {schema}.muestra_negocios "
        "ADD COLUMN IF NOT EXISTS nombres_elegidos_por VARCHAR(80)"
    )
    op.execute(f"""
        UPDATE {schema}.muestra_negocios mn
        SET alias = x.alias
        FROM (
            SELECT n.clee, array_agg(n.nom_estab ORDER BY n.ultimo DESC, n.nom_estab) AS alias
            FROM (
                SELECT e.clee, e.nom_estab, max(e.period) AS ultimo
                FROM silver.establecimientos e
                WHERE e.clee IN (SELECT clee FROM {schema}.muestra_negocios WHERE alias IS NULL)
                  AND e.nom_estab <> ''
                GROUP BY 1, 2
            ) n
            GROUP BY 1
        ) x
        WHERE mn.clee = x.clee AND mn.alias IS NULL
    """)
    op.execute(f"""
        UPDATE {schema}.muestra_negocios mn
        SET nombres_elegidos = ARRAY(SELECT jsonb_array_elements_text(v.nombres_buscados -> 'nombres')),
            nombres_elegidos_por = v.agente
        FROM (
            SELECT DISTINCT ON (muestra_id, clee) muestra_id, clee, nombres_buscados, agente
            FROM {schema}.veredictos
            WHERE nombres_buscados IS NOT NULL
            ORDER BY muestra_id, clee, verificado_en DESC
        ) v
        WHERE mn.muestra_id = v.muestra_id AND mn.clee = v.clee AND mn.nombres_elegidos IS NULL
    """)
    print(f"   {schema}.muestra_negocios: alias, nombres_elegidos y nombres_elegidos_por")


def downgrade() -> None:
    """Quita las tres columnas."""
    for columna in ("nombres_elegidos_por", "nombres_elegidos", "alias"):
        op.execute(f"ALTER TABLE verificacion.muestra_negocios DROP COLUMN IF EXISTS {columna}")
