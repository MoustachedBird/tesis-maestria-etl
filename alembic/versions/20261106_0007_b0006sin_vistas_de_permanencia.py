"""Elimina las vistas estatus_<metodo> y variantes_<metodo>

Las cuatro vistas que acompanaban a `silver.permanencia_<metodo>` eran
consultas guardadas sobre datos que ya estan en el warehouse, y ninguna
tenia consumidor: el analisis escribe la consulta que necesita.

`estatus_<metodo>` era `permanencia.*` mas dos expresiones derivadas de
columnas de la propia tabla (`CASE` sobre `cortes_tras_ultimo`, y
`huecos > 0`). Cero informacion nueva.

`variantes_<metodo>` era el join de los hechos con la tabla de identidad y
los tres catalogos. Se reconstruye tomando `clee_resuelto` de la tabla de
permanencia, filtrando `clee_<metodo>` por esa identidad y volviendo a
`establecimientos` por su llave primaria `(id, period)` -- que es
exactamente lo que hacia la vista, y que ademas es el camino rapido porque
`clee_resuelto` esta indexado. La consulta de referencia quedo en
`AGENTS.md` §N3.

`b0005` ya no las crea, asi que una base construida desde cero no las tiene
y este DROP es un no-op. Las tira de las bases que ya estaban en `b0005`.

Revision ID: b0006sin_vistas_de_permanencia
Revises: b0005permanencia_por_metodo
Create Date: 2026-11-06

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0006sin_vistas_de_permanencia"
down_revision: str | Sequence[str] | None = "b0005permanencia_por_metodo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VISTAS = (
    "estatus_radio",
    "estatus_h3",
    "variantes_radio",
    "variantes_h3",
)
"""Vistas a eliminar, dos por metodo de resolucion."""


def upgrade() -> None:
    """Tira las cuatro vistas.

    `IF EXISTS` porque una base nueva nunca las tuvo. Sin `CASCADE`: si algo
    llegara a depender de una de ellas, el error es la respuesta correcta --
    nada deberia, y descubrirlo en silencio seria peor.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    for vista in VISTAS:
        op.execute(f"DROP VIEW IF EXISTS {schema}.{vista}")
    print(f"   silver: {len(VISTAS)} vistas de permanencia eliminadas")


def downgrade() -> None:
    """No las recrea: su SQL ya no vive en el codigo.

    Esta revision es de ida. El SQL de cada vista esta en el historial de
    git (`src/etl/loaders/permanencia.py` antes de este cambio) y la
    consulta equivalente en `AGENTS.md` §N3, asi que no hay nada que
    recuperar que no se pueda escribir.
    """
