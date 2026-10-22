"""Tira meta.normalize_tasks

La normalizacion de fechas no es una etapa del pipeline: `transformers/
enrich.py` deriva `fecha_alta` sobre el DataFrame antes del `COPY`, dentro
del ingest. No existe job que escriba en esta tabla ni `job_type=NORMALIZE`
en `meta.job_runs`, asi que la tabla solo podia estar vacia.

No toca ninguna otra tabla: el historial de descargas vive en
`meta.download_tasks` y es lo que hace que una corrida reanude en vez de
rebajar los ZIP.

Revision ID: b0001drop_normalize_tasks
Revises: b0000baseline
Create Date: 2026-10-22

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0001drop_normalize_tasks"
down_revision: str | Sequence[str] | None = "b0000baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JOB_TYPES = "DOWNLOAD, TRANSFORM, CLEE_RESOLUTION"
"""Los unicos `job_type` que algun job escribe."""


def upgrade() -> None:
    """Elimina la tabla y pone al dia el comentario de `job_type`.

    `IF EXISTS` porque una base creada despues de esta revision nunca tuvo
    la tabla: el baseline ya no la incluye en META_TABLES. Eso hace la
    revision idempotente y valida en los dos caminos (base existente que la
    tenia, base nueva que no).

    El import de `src` va aqui adentro y no arriba: al correr el CLI de
    alembic el `sys.path` lo arma `env.py`, asi que a nivel de modulo
    `src` todavia no es importable.
    """
    from src.shared.enums import Layer

    schema = Layer.META.value
    op.execute(f"DROP TABLE IF EXISTS {schema}.normalize_tasks")
    op.execute(f"COMMENT ON COLUMN {schema}.job_runs.job_type IS '{JOB_TYPES}'")


def downgrade() -> None:
    """No se recrea.

    El modelo ORM ya no existe, asi que no hay de donde derivar el DDL. Para
    volver atras hay que reconstruir la base desde el baseline.
    """
    raise NotImplementedError("irreversible: el modelo NormalizeTask ya no existe")
