"""Cambia entidades_economicas por permanencia_radio y permanencia_h3

`silver.entidades_economicas` era el primer intento de esta tabla: colapsaba
las observaciones a una fila por establecimiento con su primer y ultimo
corte. Le faltaban los alias, las variantes de domicilio, el contacto y los
ingredientes de censura, y su nombre no distinguia el grano del de la tabla
de hechos.

Se reemplaza por una tabla por metodo de resolucion, con el mismo criterio
que `clee_radio`/`clee_h3`: el metodo va en el `FROM`, asi que una consulta
no puede mezclarlos por descuido.

El estatus de cierre va en una VISTA (`silver.estatus_<metodo>`) y no en una
columna: el protocolo define la regla como preliminar, y redefinir una vista
cuesta un segundo contra reconstruir 10M de filas.

La tabla que se elimina esta VACIA: solo la poblaba el comando
`build-entidades`, que nunca se corrio contra la base actual.

Revision ID: b0005permanencia_por_metodo
Revises: b0004texto_libre_sin_tope
Create Date: 2026-10-28

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0005permanencia_por_metodo"
down_revision: str | Sequence[str] | None = "b0004texto_libre_sin_tope"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS = ("permanencia_radio", "permanencia_h3")
"""Tablas de permanencia a crear, una por metodo de resolucion."""

METODOS = ("radio", "h3")
"""Metodos, para nombrar sus vistas de estatus."""


def upgrade() -> None:
    """Crea las tablas y sus vistas, y elimina la que reemplazan.

    El DDL de las tablas sale del metadata del ORM y no de `op.create_table`
    escrito a mano, por la misma razon que el baseline: el esquema se declara
    en un solo lugar.

    DROP antes del CREATE y sin `checkfirst`: estas tablas son derivadas y se
    reconstruyen con `transform --only permanencia`, asi que tirarlas no
    cuesta datos. Eso hace la revision segura de re-aplicar a mano si el
    modelo gano columnas despues de un primer intento.

    Los imports de `src` van aqui adentro: al correr el CLI de alembic el
    `sys.path` lo arma `env.py`.
    """
    from src.etl.loaders.permanencia import _vista_sql, _vista_variantes_sql
    from src.shared.db.base import Base
    from src.shared.db.models import permanencia  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.SILVER.value

    for metodo in METODOS:
        op.execute(f"DROP VIEW IF EXISTS {schema}.estatus_{metodo}")
        op.execute(f"DROP VIEW IF EXISTS {schema}.variantes_{metodo}")
    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")

    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]
    Base.metadata.create_all(bind=op.get_bind(), tables=tablas)
    print(f"   silver: {len(tablas)} tablas de permanencia ({', '.join(TABLAS)})")

    # Via `op.execute` y no `conn.execute`: es lo unico que emite SQL en modo
    # offline (`alembic upgrade --sql`), que es como se revisa la migracion
    # antes de aplicarla.
    for metodo in METODOS:
        op.execute(_vista_sql(metodo, schema))
        op.execute(_vista_variantes_sql(metodo, schema))
    print(f"   silver: {2 * len(METODOS)} vistas (estatus de cierre y variantes)")

    op.execute(f"DROP TABLE IF EXISTS {schema}.entidades_economicas")
    print("   silver.entidades_economicas eliminada (estaba vacia)")


def downgrade() -> None:
    """Tira las vistas y las tablas; no recrea la anterior.

    Su contenido era 100% derivable, igual que el de estas: se reconstruye
    con `transform --only permanencia`.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    for metodo in METODOS:
        op.execute(f"DROP VIEW IF EXISTS {schema}.estatus_{metodo}")
        op.execute(f"DROP VIEW IF EXISTS {schema}.variantes_{metodo}")
    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")
