"""Crea silver.clee_celda_h3_7: el primer metodo con otra regla espacial

`clee_radio` y `clee_h3` resultaron ser el MISMO metodo con dos indices:
medido el 2026-11-07, producen la misma particion --15,643,923 identidades
en las dos, 0 de 82.9M observaciones en disputa-- porque los dos terminan
en el circulo de SPATIAL_RADIUS_M y solo difieren en como buscan a los
vecinos.

`clee_celda_h3_7` es el primero que cambia la REGLA: dos observaciones son
candidatas si caen en la misma celda de CELL_MATCH_RES (res 7), y el radio
no se aplica. Su error depende de donde cae la reticula de H3 y no de la
distancia, que es lo que lo hace util como voto distinto en un consenso.

Dos cambios, los dos aditivos:

1. `silver.establecimientos.h3_cell_7` -- la celda res 7, derivada como
   padre de `h3_cell_9`. Se PRECALCULA en el ingest (`enrich.py`) por lo
   mismo que `h3_cell_5`: derivarla al vuelo costaria una llamada a h3 por
   celda distinta en cada periodo y en cada corrida, y la columna se paga
   una vez. Nace NULL en las 82.9M filas ya cargadas; el backfill es
   aparte, porque es una operacion y no un cambio de esquema.
2. `silver.clee_celda_h3_7` -- la tabla del metodo, vacia y sin particiones.
   Las crea la resolucion corte por corte, igual que las otras dos (b0002).

No toca `clee_radio`, `clee_h3` ni las de permanencia, y no reescribe
ninguna fila: `ADD COLUMN` con default NULL es solo catalogo en PostgreSQL.

Ver `alternativas_clee_propagation.md` §§0 y 5.

Revision ID: b0008clee_celda_h3_7
Revises: b0007permanencia_sin_snapshots
Create Date: 2026-11-07

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0008clee_celda_h3_7"
down_revision: str | Sequence[str] | None = "b0007permanencia_sin_snapshots"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLA = "clee_celda_h3_7"
"""Tabla de resolucion del metodo `celda_h3_7`, una por metodo (b0002)."""


def upgrade() -> None:
    """Agrega `h3_cell_7` a los hechos y crea la tabla del metodo.

    El DDL de la tabla sale del metadata del ORM y no de `op.create_table`
    escrito a mano, por la misma razon que el resto de la cadena: el esquema
    se declara en un solo lugar.

    `ADD COLUMN ... IF NOT EXISTS` y `checkfirst=True` vuelven la revision
    segura de re-aplicar: las dos operaciones son aditivas y no hay nada que
    preservar si ya existen.

    **La columna va sobre la tabla PADRE.** En una tabla particionada eso la
    propaga sola a las 20 particiones, y al ser NULL por default PostgreSQL
    no reescribe ninguna de las 82.9M filas: es un cambio de catalogo.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import clee  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.SILVER.value

    op.execute(f"ALTER TABLE {schema}.establecimientos ADD COLUMN IF NOT EXISTS h3_cell_7 bigint")
    op.execute(
        f"COMMENT ON COLUMN {schema}.establecimientos.h3_cell_7 IS "
        "'Celda H3 res 7 (~1.5km), padre de h3_cell_9. CRITERIO espacial del metodo celda'"
    )
    print(f"   silver: {schema}.establecimientos.h3_cell_7 agregada (NULL en lo ya cargado)")

    tabla = Base.metadata.tables[f"{schema}.{TABLA}"]
    Base.metadata.create_all(bind=op.get_bind(), tables=[tabla], checkfirst=True)
    print(f"   silver: {TABLA} creada (metodo `celda_h3_7`, sin particiones)")
    print("   falta: poblar h3_cell_7 en los cortes ya cargados, y luego")
    print("          transform --only clee-celda-h3-7")


def downgrade() -> None:
    """Tira la tabla y la columna.

    Las dos son 100% derivables: `h3_cell_7` del padre de `h3_cell_9`, y la
    tabla con `transform --only clee-celda-h3-7`.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    op.execute(f"DROP TABLE IF EXISTS {schema}.{TABLA}")
    op.execute(f"ALTER TABLE {schema}.establecimientos DROP COLUMN IF EXISTS h3_cell_7")
