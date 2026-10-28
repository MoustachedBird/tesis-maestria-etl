"""Cambia entidades_economicas por permanencia_radio y permanencia_h3

`silver.entidades_economicas` era el primer intento de esta tabla: colapsaba
las observaciones a una fila por establecimiento con su primer y ultimo
corte. Le faltaban los alias, las variantes de domicilio y los ingredientes
de censura, y su nombre no distinguia el grano del de la tabla de hechos.

Se reemplaza por una tabla por metodo de resolucion, con el mismo criterio
que `clee_radio`/`clee_h3`: el metodo va en el `FROM`, asi que una consulta
no puede mezclarlos por descuido.

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


def upgrade() -> None:
    """Crea las dos tablas y elimina la que reemplazan.

    El DDL sale del metadata del ORM y no de `op.create_table` escrito a
    mano, por la misma razon que el baseline: el esquema se declara en un
    solo lugar. `checkfirst` hace la revision idempotente.

    El import de `src` va aqui adentro: al correr el CLI de alembic el
    `sys.path` lo arma `env.py`.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import permanencia  # noqa: F401  (registra el metadata)
    from src.shared.enums import Layer

    conn = op.get_bind()
    schema = Layer.SILVER.value

    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]
    Base.metadata.create_all(bind=conn, tables=tablas, checkfirst=True)
    print(f"   silver: {len(tablas)} tablas de permanencia ({', '.join(TABLAS)})")

    op.execute(f"DROP TABLE IF EXISTS {schema}.entidades_economicas")
    print("   silver.entidades_economicas eliminada (estaba vacia)")


def downgrade() -> None:
    """Tira las dos tablas; no recrea la anterior.

    Su contenido era 100% derivable, igual que el de estas: se reconstruye
    con `transform --only permanencia`.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value
    for nombre in TABLAS:
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre}")
