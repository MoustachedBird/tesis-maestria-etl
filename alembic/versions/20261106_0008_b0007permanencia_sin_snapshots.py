"""Saca de permanencia el valor "del corte mas reciente"

Las tablas guardaban, por establecimiento, el valor con que cada campo
oficial quedo en el ultimo corte en que se lo vio: `nom_estab`,
`codigo_act`, `cve_ent`/`cve_mun`, `per_ocu`/`categoria_tamano`/
`empleados_min`/`empleados_max`, `tipounieco`, `tipo_asent`, `cod_postal`,
`cve_loc`, `ageb`, `latitud`/`longitud`, `h3_cell_9`/`h3_cell_5`, mas la
primera posicion observada.

Era el peor de los dos mundos. Congelaba un instante de algo que cambia
entre cortes --y los catalogos de INEGI cambian de etiqueta: `tipo_asent`
"OTRO" paso a "OTRA (ESPECIFICAR)" en 2026-05-- y de todos modos no
contestaba "que valores tuvo", que es la pregunta real. Para eso el join a
los hechos devuelve TODOS los cortes, no solo el ultimo:

    permanencia_<m>  <-many:1-  clee_<m>  -1:1->  establecimientos

En su lugar quedan banderas de cobertura (`tiene_*`): True si el campo vino
lleno en al menos un corte. No son un sustituto del valor, son otra
pregunta --"de este establecimiento se puede afirmar algo sobre este
campo?"-- y en datos de encuesta, donde cualquier campo puede venir vacio o
faltar un periodo completo, esa pregunta es informacion de primera clase.

Lo que se conserva son los agregados sobre la vida completa, que son los
que NO se pueden recomputar sin barrer los 82.9M hechos: la ventana y su
censura, los conteos de variantes, `delta_empleados_min`, el rectangulo
envolvente, el desplazamiento neto y la dispersion.

Tambien se van `ix_*_geo` e `ix_*_actividad`: sus columnas ya no viven aqui.
Estratificar por municipio o por sector se hace en el join, contra
`ix_estab_geo` e `ix_estab_activity_period`, que si existen.

Revision ID: b0007permanencia_sin_snapshots
Revises: b0006sin_vistas_de_permanencia
Create Date: 2026-11-06

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0007permanencia_sin_snapshots"
down_revision: str | Sequence[str] | None = "b0006sin_vistas_de_permanencia"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS = ("permanencia_radio", "permanencia_h3")
"""Tablas de permanencia a reconstruir, una por metodo de resolucion."""


def upgrade() -> None:
    """Recrea las dos tablas con el esquema nuevo.

    DROP y CREATE en vez de una lista de `ALTER TABLE`: son 16 columnas que
    se van y 11 que llegan, y los valores de las nuevas no se pueden derivar
    de las viejas --salen de agregar los 82.9M hechos--. Un `ALTER` dejaria
    las banderas en NULL y habria que rellenarlas con la misma consulta que
    reconstruye la tabla entera.

    Es seguro porque **estas tablas son 100% derivadas**: se rehacen con
    `transform --only permanencia`. Ninguna otra tabla las referencia, y la
    reconstruccion no toca `establecimientos` ni `clee_<metodo>`.

    El DDL sale del metadata del ORM, no de `op.create_table` a mano: el
    esquema se declara en un solo lugar.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import permanencia  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.SILVER.value

    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")

    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]
    Base.metadata.create_all(bind=op.get_bind(), tables=tablas)
    columnas = len(tablas[0].columns)
    print(
        f"   silver: {len(tablas)} tablas de permanencia recreadas con "
        f"{columnas} columnas, sin valores de un corte concreto "
        f"({', '.join(TABLAS)})"
    )
    print("   hay que reconstruirlas: transform --only permanencia --force")


def downgrade() -> None:
    """Tira las tablas sin recrear el esquema anterior.

    Recrearlo exigiria copiar aqui las 53 columnas viejas, que es
    precisamente el duplicado de esquema que el proyecto evita. Su contenido
    es derivable: volver atras es `alembic downgrade` mas un
    `transform --only permanencia` con el modelo de esa revision.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")
