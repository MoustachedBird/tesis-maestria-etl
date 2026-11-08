"""Crea gold.negocios_clee_oficial y su tabla de variantes

Primera tabla de la capa de consumo. Hasta ahora `gold` era un schema vacio
con el rol de lectura ya otorgado (`b0000`), esperando que se disenara.

El alcance lo fija `gold_cohorte_2021.md`: SOLO los cortes en que INEGI
publica el CLEE. Medido el 2026-11-08 son 9 cortes (2021-05 -> 2026-05) con
cobertura del 100% frente a 11 al 0%, y suman 51.5M de las 82.9M
observaciones. Ahi la identidad es OBSERVADA y no inferida, asi que no hay
`NEW_*` que consolidar ni error de propagacion que arrastrar, y los tres
metodos de resolucion coinciden trivialmente.

Dos tablas:

1. `negocios_clee_oficial` -- una fila por negocio de la cohorte, con su
   ventana de vida calculada sobre los cortes oficiales, los agregados de
   trayectoria, las banderas de cobertura, los valores canonicos y la
   procedencia que la vuelve citable (`ventana_cortes`, `metodo_base`,
   `version_reglas`, `construida_en`).
2. `negocios_clee_oficial_variantes` -- una fila por (negocio, campo, valor):
   todos los valores distintos que tuvo en seis campos, con la ventana en que
   uso cada uno y `es_canonico` marcando el que publico la tabla madre.

**Trae la primera extension del esquema: `pg_trgm`.** La necesita el indice
GIN de busqueda difusa sobre `valor`, que es lo que vuelve respondible "que
negocio es 'tacos paco'?" -- la pregunta que se hace al verificar un caso a
mano o al cruzar con una fuente externa. Viene en el contrib de la imagen
oficial `postgres:17`, asi que no es un cambio de infraestructura.

Las dos nacen VACIAS: el loader es aparte, porque poblarlas es una operacion
y no un cambio de esquema.

Revision ID: b0009gold_cohorte_oficial
Revises: b0008clee_celda_h3_7
Create Date: 2026-11-08

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0009gold_cohorte_oficial"
down_revision: str | Sequence[str] | None = "b0008clee_celda_h3_7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS = ("negocios_clee_oficial", "negocios_clee_oficial_variantes")
"""En orden de dependencia: la hija tiene FK hacia la madre."""


def upgrade() -> None:
    """Instala pg_trgm y crea las dos tablas desde el metadata del ORM.

    El DDL sale del modelo y no de `op.create_table` escrito a mano, por la
    misma razon que el resto de la cadena: el esquema se declara en un solo
    lugar y la migracion no puede desincronizarse de el.

    `CREATE EXTENSION` va antes del `create_all` porque el indice GIN de
    `valor` usa `gin_trgm_ops`, que no existe hasta que la extension esta.

    `checkfirst=True` vuelve la revision segura de re-aplicar: las dos tablas
    son aditivas y derivadas, asi que no hay nada que preservar.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import gold  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.GOLD.value

    # La extension es de la BASE, no del schema: se instala una vez y la usan
    # todos. Va en `public` por default, que es donde el search_path la
    # encuentra desde cualquier schema.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    print("   extension pg_trgm instalada (busqueda difusa de nombres)")

    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]
    Base.metadata.create_all(bind=op.get_bind(), tables=tablas, checkfirst=True)
    for tabla in tablas:
        print(f"   gold: {tabla.name} creada ({len(tabla.columns)} columnas, vacia)")

    # El grano en el COMMENT, que es donde ocurriria la confusion: en un `\d+`
    # o en una consulta ad-hoc de analisis. Es la tercera salvaguarda de
    # arquitectura.md §2.3.1, y la unica que sigue siendo necesaria ahora que
    # el nombre no colisiona con silver.establecimientos.
    op.execute(
        f"COMMENT ON TABLE {schema}.negocios_clee_oficial IS "
        "'Un NEGOCIO de la cohorte con CLEE oficial de INEGI (9 cortes, 2021-05..2026-05). "
        "NO es silver.establecimientos, que son 82.9M OBSERVACIONES. Toda identidad de "
        "esta tabla la asigno INEGI: no hay propagadas ni sinteticas NEW_*'"
    )
    op.execute(
        f"COMMENT ON TABLE {schema}.negocios_clee_oficial_variantes IS "
        "'Valores distintos que tuvo cada negocio por campo, con su ventana y es_canonico. "
        "Solo la cohorte oficial; las de la propagada irian en una tabla aparte'"
    )

    print("   para poblarlas: transform --only gold (loader pendiente)")


def downgrade() -> None:
    """Tira las dos tablas; conserva la extension.

    `pg_trgm` se queda porque quitarla afectaria a cualquier otro indice que
    la use despues, y no cuesta nada tenerla instalada.

    El contenido es 100% derivable: se reconstruye con el loader.
    """
    from src.shared.enums import Layer

    schema = Layer.GOLD.value
    for nombre in reversed(TABLAS):
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")
