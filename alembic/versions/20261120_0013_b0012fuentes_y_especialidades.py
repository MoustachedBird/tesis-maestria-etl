"""Más fuentes para el agente (v5): recolector fino, especialista por grupo, capturas

El agente pasa de cuatro fuentes a una veintena (`plan_fuentes_agente.md`), y
leerlas con un especialista cada una serían ~20 llamadas al modelo por
negocio. Se separan dos ejes:

    consultas.recolector     la fuente CONCRETA (osm, bing, wayback, sat...):
                             el eje del analisis
    informes.especialista    el GRUPO que la lee (maps, web, redes,
                             plataformas, sitio, registros, fachada)

Los dos `CHECK` se recrean desde los enums (`Recolector`, `Especialidad`) y
las dos columnas pasan de VARCHAR(10) a VARCHAR(20) (`common_crawl`,
`street_view`, `plataformas` no caben en 10). Los valores de v4 (`maps`,
`web`, `redes`, `sitio`) estan en los dos enums, asi que las filas que ya
hay siguen pasando el CHECK nuevo.

Y una columna: `consultas.imagen_ruta`, la captura que leyo el modelo de
vision. Una pieza de fachada es texto que escribio un modelo mirando una
imagen; sin la imagen no se puede auditar.

Solo aditiva e idempotente (`IF EXISTS` / `IF NOT EXISTS`); no tira datos.

Revision ID: b0012fuentes_y_especialidades
Revises: b0011expediente_y_juez
Create Date: 2026-11-20

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0012fuentes_y_especialidades"
down_revision: str | Sequence[str] | None = "b0011expediente_y_juez"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_V4 = ("maps", "web", "redes", "sitio")
"""Los valores de ambos CHECK antes de esta revision, para el downgrade."""


def _lista(valores) -> str:
    """`'a', 'b'` para un `IN (...)`."""
    return ", ".join(f"'{v}'" for v in valores)


def _check(schema: str, tabla: str, columna: str, valores) -> None:
    """Recrea `ck_<tabla>_<columna>` (nombre de la convencion de `base.py`) con estos valores."""
    nombre = f"ck_{tabla}_{columna}"
    op.execute(f"ALTER TABLE {schema}.{tabla} DROP CONSTRAINT IF EXISTS {nombre}")
    op.execute(
        f"ALTER TABLE {schema}.{tabla} ADD CONSTRAINT {nombre} "
        f"CHECK ({columna} IN ({_lista(valores)}))"
    )


def upgrade() -> None:
    """Ensancha y recrea los CHECK de recolector y especialista; agrega `imagen_ruta`."""
    from src.shared.enums import Especialidad, Layer, Recolector

    schema = Layer.VERIFICACION.value
    op.execute(f"ALTER TABLE {schema}.consultas ALTER COLUMN recolector TYPE VARCHAR(20)")
    op.execute(f"ALTER TABLE {schema}.informes ALTER COLUMN especialista TYPE VARCHAR(20)")
    _check(schema, "consultas", "recolector", [r.value for r in Recolector])
    _check(schema, "informes", "especialista", [e.value for e in Especialidad])
    op.execute(f"ALTER TABLE {schema}.consultas ADD COLUMN IF NOT EXISTS imagen_ruta TEXT")
    op.execute(
        f"COMMENT ON COLUMN {schema}.consultas.imagen_ruta IS "
        "'La captura que leyo el modelo de vision, bajo data/agente/capturas/; NULL si la pieza "
        "es texto'"
    )
    print(
        f"   {schema}: {len(Recolector)} recolectores, {len(Especialidad)} especialidades; "
        "consultas.imagen_ruta agregada"
    )


def downgrade() -> None:
    """Vuelve a los cuatro valores de v4.

    Falla si ya hay filas con un recolector o especialista nuevo: bajar de
    aqui exige borrar antes esos veredictos, y eso se hace a sabiendas.
    """
    from src.shared.enums import Layer

    schema = Layer.VERIFICACION.value
    op.execute(f"ALTER TABLE {schema}.consultas DROP COLUMN IF EXISTS imagen_ruta")
    _check(schema, "consultas", "recolector", _V4)
    _check(schema, "informes", "especialista", _V4)
    op.execute(f"ALTER TABLE {schema}.consultas ALTER COLUMN recolector TYPE VARCHAR(10)")
    op.execute(f"ALTER TABLE {schema}.informes ALTER COLUMN especialista TYPE VARCHAR(10)")
