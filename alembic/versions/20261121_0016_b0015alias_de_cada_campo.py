"""Los alias de cada campo que cambia entre cortes, y el domicilio y teléfono elegidos

Son datos de encuesta: además del nombre, el domicilio, el teléfono, la
razón social, el sitio y el correo cambian de un corte a otro. `b0014` guardó
los alias del nombre; esta revisión hace lo mismo con el resto, y guarda el
domicilio y el teléfono con que el planificador (v6) buscó al negocio:

    alias_domicilios        alias_telefonos        alias_razones_sociales
    alias_sitios            alias_correos          (el más reciente primero)
    domicilio_elegido       telefono_elegido       (lo último del planificador)

Los alias se rellenan para las muestras que ya existen desde
`silver.establecimientos` por `clee` (indexado); los elegidos, desde el
veredicto más reciente con `nombres_buscados`. Solo escribe donde la columna
está en NULL: idempotente, no cambia nada que ya hubiera.

Revision ID: b0015alias_de_cada_campo
Revises: b0014alias_en_la_muestra
Create Date: 2026-11-21

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0015alias_de_cada_campo"
down_revision: str | Sequence[str] | None = "b0014alias_en_la_muestra"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_DOMICILIO = (
    "concat_ws(' ', NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''), "
    "NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, ''))"
)
"""Copia congelada de `insumo._DOMICILIO`: una migración no cambia si cambia el código."""

_ALIAS = {
    "alias_domicilios": _DOMICILIO,
    "alias_telefonos": "e.telefono",
    "alias_razones_sociales": "e.raz_social",
    "alias_sitios": "e.www",
    "alias_correos": "e.correoelec",
}
"""Columna -> expresión que la llena."""


def upgrade() -> None:
    """Agrega las columnas y rellena lo derivable."""
    s = "verificacion"
    for columna in _ALIAS:
        op.execute(f"ALTER TABLE {s}.muestra_negocios ADD COLUMN IF NOT EXISTS {columna} TEXT[]")
    for columna in ("domicilio_elegido", "telefono_elegido"):
        op.execute(f"ALTER TABLE {s}.muestra_negocios ADD COLUMN IF NOT EXISTS {columna} TEXT")
    for columna, expr in _ALIAS.items():
        op.execute(f"""
            UPDATE {s}.muestra_negocios mn
            SET {columna} = (
                SELECT array_agg(x.v ORDER BY x.ultimo DESC, x.v)
                FROM (SELECT {expr} AS v, max(e.period) AS ultimo
                      FROM silver.establecimientos e
                      WHERE e.clee = mn.clee AND {expr} <> ''
                      GROUP BY 1) x)
            WHERE mn.{columna} IS NULL
        """)
    op.execute(f"""
        UPDATE {s}.muestra_negocios mn
        SET domicilio_elegido = v.nombres_buscados ->> 'domicilio',
            telefono_elegido = v.nombres_buscados ->> 'telefono'
        FROM (
            SELECT DISTINCT ON (muestra_id, clee) muestra_id, clee, nombres_buscados
            FROM {s}.veredictos
            WHERE nombres_buscados IS NOT NULL
            ORDER BY muestra_id, clee, verificado_en DESC
        ) v
        WHERE mn.muestra_id = v.muestra_id AND mn.clee = v.clee
          AND mn.domicilio_elegido IS NULL AND mn.telefono_elegido IS NULL
    """)
    op.execute(
        f"COMMENT ON COLUMN {s}.veredictos.nombres_buscados IS "
        "'{nombres, domicilio, telefono, consistentes, razonamiento, automatica}: con que datos se "
        "busco al negocio y por que, segun el planificador (v6+). NULL en veredictos anteriores'"
    )
    print(f"   {s}.muestra_negocios: alias de domicilio, telefono, razon social, sitio y correo")


def downgrade() -> None:
    """Quita las columnas."""
    for columna in (*_ALIAS, "domicilio_elegido", "telefono_elegido"):
        op.execute(f"ALTER TABLE verificacion.muestra_negocios DROP COLUMN IF EXISTS {columna}")
