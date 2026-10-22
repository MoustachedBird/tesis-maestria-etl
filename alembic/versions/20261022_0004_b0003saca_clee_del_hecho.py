"""Saca la identidad resuelta de silver.establecimientos

Cierra la separación de `docs/separacion_ingest_clee.md`: la tabla de hechos
guarda lo que INEGI publicó, y la identidad resuelta vive en la tabla de su
método (`silver.clee_radio`, `silver.clee_h3`).

Dos cambios:

- `establecimientos` pierde `clee_propagado_radio` y `clee_propagado_h3`.
  Sus índices se van con ellas (PostgreSQL los elimina al eliminar la
  columna). La columna `clee` NO se toca: es el dato de INEGI, no una
  derivación.
- `entidades_economicas.clee_propagado_radio` pasa a llamarse
  `clee_resuelto`. La dimensión guarda la identidad del negocio, no una
  columna de otra tabla, y el nombre viejo quedaba huérfano.

**Irreversible en la práctica.** El contenido de las columnas es derivable
re-corriendo la resolución, que es lo que hace esta migración segura: lo que
se tira no es un dato de origen. Tras aplicarla hay que correr la resolución
para poblar `silver.clee_radio` — hasta entonces la dimensión
`entidades_economicas` se reconstruye vacía.

Revision ID: b0003saca_clee_del_hecho
Revises: b0002clee_por_metodo
Create Date: 2026-10-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0003saca_clee_del_hecho"
down_revision: str | Sequence[str] | None = "b0002clee_por_metodo"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNAS_DEL_HECHO = ("clee_propagado_radio", "clee_propagado_h3")
"""Derivaciones que salen de `establecimientos`."""


def upgrade() -> None:
    """Elimina las columnas derivadas y renombra la de la dimensión.

    `IF EXISTS` en los DROP y una guarda en el RENAME para que la revisión
    sea idempotente: una base creada después de esta revisión nunca tuvo las
    columnas, porque el modelo ORM ya no las declara.

    El import de `src` va aquí adentro: al correr el CLI de alembic el
    `sys.path` lo arma `env.py`.
    """
    from src.shared.enums import Layer

    schema = Layer.SILVER.value

    for columna in COLUMNAS_DEL_HECHO:
        op.execute(f"ALTER TABLE {schema}.establecimientos DROP COLUMN IF EXISTS {columna}")

    # El RENAME no admite IF EXISTS, asi que se consulta el catalogo.
    renombrar = f"""
    DO $$
    BEGIN
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = '{schema}'
              AND table_name = 'entidades_economicas'
              AND column_name = 'clee_propagado_radio'
        ) THEN
            ALTER TABLE {schema}.entidades_economicas
                RENAME COLUMN clee_propagado_radio TO clee_resuelto;
        END IF;
    END $$;
    """
    op.execute(renombrar)


def downgrade() -> None:
    """No se recrea.

    Las columnas no guardaban dato de origen: su contenido se reconstruye
    corriendo la resolución CLEE, que ahora escribe en las tablas por
    método.
    """
    raise NotImplementedError(
        "irreversible: la identidad resuelta se reconstruye corriendo la resolucion"
    )
