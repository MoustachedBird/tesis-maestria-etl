"""baseline PostgreSQL: tres schemas, tablas y roles

Baseline unico que reemplaza la cadena de cuatro migraciones de DuckDB
(a1000denue -> a2000sesnsp -> a3000entidades -> a4000idx). Se consolida en
una sola porque el warehouse se reconstruye desde cero en PostgreSQL y no
hay ninguna base que migrar incrementalmente (arquitectura.md D8): una
cadena de pasos intermedios solo seria historia de un motor que ya no se
usa.

Crea el esquema objetivo de §2.3 completo:

  - Los tres schemas: `silver`, `gold` y `meta`.
  - `silver.*`: catalogos de geografia, SCIAN con su jerarquia, los cuatro
    catalogos de dominio con surrogate id, la tabla de hechos
    `establecimientos` con sus FKs reales e indices, las dos de SESNSP y la
    dimension `entidades_economicas` (que se movera a `gold` en la Fase 5).
  - `meta.*`: las seis tablas de tracking, ahora con FKs reales hacia
    `job_runs` y `ON DELETE CASCADE` -- lo que el DDL de DuckDB documentaba
    como imposible.
  - `gold` queda vacio a proposito: la capa se disena en la Fase 5.
  - Rol `api_ro` con SELECT solo en `gold` y `meta`.
  - Siembra los catalogos desde los parquet de `data/seeds`.

El esquema se crea desde `src.shared.db.models` (metadata del ORM), que es
la definicion canonica: asi la migracion no puede desincronizarse del
modelo.

Revision ID: b0000baseline
Revises:
Create Date: 2026-10-08 00:01:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0000baseline"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


SILVER_TABLES = [
    "denue_entidades",
    "denue_municipios",
    "denue_localidades",
    "denue_actividades_economicas",
    "denue_tipos_vialidad",
    "denue_tipos_asentamiento",
    "denue_tipos_unidad_economica",
    "denue_estratos_personal",
    "establecimientos",
    "sesnsp_delitos",
    "sesnsp_incidencia_municipal",
    "entidades_economicas",
]
"""Tablas de `silver`, en orden de dependencia.

Los catalogos van ANTES de la tabla de hechos porque sus FKs les apuntan, y
`sesnsp_incidencia_municipal` despues de `sesnsp_delitos` y de la geografia
por la misma razon.
"""

META_TABLES = [
    "job_runs",
    "download_tasks",
    "process_tasks",
    "clee_resolution_tasks",
    "data_lineage",
]
"""Tablas de `meta`. `job_runs` primero: las otras cuatro la referencian."""


def _is_postgres(conn) -> bool:
    """True si la conexion es PostgreSQL.

    Los roles y los GRANT solo existen ahi; en DuckDB (que la suite de
    tests sigue usando) esos pasos se saltan.
    """
    return conn.dialect.name == "postgresql"


def _safe_identifier(value: str, origin: str) -> str:
    """Valida que un nombre de rol venido del `.env` sea un identificador SQL.

    Los nombres de rol no se pueden parametrizar en `CREATE ROLE` ni en
    `GRANT`, asi que terminan interpolados en el DDL. Esto es lo que impide
    que un `POSTGRES_RO_USER` mal puesto (o malicioso) se convierta en SQL
    arbitrario.

    Args:
        value: El identificador a validar.
        origin: Nombre de la variable de entorno, para el mensaje de error.

    Returns:
        El mismo valor si es valido.

    Raises:
        ValueError: Si no es un identificador simple de PostgreSQL.
    """
    import re

    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value or ""):
        raise ValueError(
            f"{origin}='{value}' no es un identificador valido de PostgreSQL "
            "(minusculas, digitos y _, empezando por letra o _, max 63)"
        )
    return value


def _tables(metadata, schema: str, names: list[str]) -> list:
    """Objetos Table del metadata para los nombres dados, en ese orden.

    Califica con el schema porque las claves de `metadata.tables` lo
    llevan desde que los modelos lo declaran (base.py:DEFAULT_SCHEMA).
    """
    return [metadata.tables[f"{schema}.{name}"] for name in names]


def upgrade() -> None:
    """Crea los schemas, las tablas de silver y meta, el rol de lectura y siembra."""
    from src.etl.loaders.dominios import load_seeds as load_dominios
    from src.etl.loaders.estratos import load_seeds as load_estratos
    from src.etl.loaders.geography import load_seeds as load_geografia
    from src.etl.loaders.scian import load_seeds as load_scian
    from src.shared.config import settings
    from src.shared.db.base import SCHEMAS, Base
    from src.shared.db.models import meta, silver  # noqa: F401  (registra el metadata)
    from src.shared.enums import Layer

    conn = op.get_bind()
    seeds = settings.SEEDS_DIR

    # 1. Schemas. Van primero y por separado: SQLAlchemy emite el
    #    CREATE TABLE calificado pero nunca crea el schema que lo contiene.
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    print(f"   Schemas creados: {', '.join(SCHEMAS)}")

    # 2. Tablas de datos y de control.
    Base.metadata.create_all(
        bind=conn, tables=_tables(Base.metadata, Layer.SILVER.value, SILVER_TABLES)
    )
    print(f"   silver: {len(SILVER_TABLES)} tablas")

    Base.metadata.create_all(
        bind=conn, tables=_tables(Base.metadata, Layer.META.value, META_TABLES)
    )
    print(f"   meta: {len(META_TABLES)} tablas")

    # 3. Rol de solo lectura del API (arquitectura.md §2.3.1).
    if _is_postgres(conn):
        _grant_read_only_role(conn, settings)

    # 4. Siembra de catalogos desde los parquet versionados.
    print(f"   Geografia: {load_geografia(conn, seeds)}")
    print(f"   SCIAN: {load_scian(conn, seeds)}")
    print(f"   Dominios: {load_dominios(conn, seeds)}")
    print(f"   Estratos: {load_estratos(conn, seeds)}")


def _grant_read_only_role(conn, settings) -> None:
    """Crea `api_ro` y le da SELECT solo en `gold` y `meta`.

    Es la salvaguarda estructural de §2.3.1: sin GRANT en `silver`, el API
    no puede leer la tabla de observaciones ni, por tanto, confundir sus
    82.9M filas con los 10.45M negocios de `gold.establecimientos`. Que la
    proteccion sea un permiso y no una convencion es justamente el punto.

    `DEFAULT PRIVILEGES` cubre las tablas que se creen despues -- de otro
    modo cada tabla nueva de `gold` naceria invisible para el API y habria
    que acordarse de otorgarla a mano.
    """
    from src.shared.enums import Layer

    role = _safe_identifier(settings.POSTGRES_RO_USER, "POSTGRES_RO_USER")
    owner = _safe_identifier(settings.POSTGRES_USER, "POSTGRES_USER")
    password = settings.POSTGRES_RO_PASSWORD
    readable = (Layer.GOLD.value, Layer.META.value)

    # CREATE ROLE no admite IF NOT EXISTS ni parametros, asi que se consulta
    # primero (parametrizado) y el password se escapa con quote_literal del
    # propio PostgreSQL en vez de interpolarlo a mano.
    exists = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    ).scalar()

    if exists:
        print(f"   Rol {role} ya existe, no se recrea")
    elif password:
        quoted = conn.execute(sa.text("SELECT quote_literal(:pwd)"), {"pwd": password}).scalar()
        op.execute(f"CREATE ROLE {role} LOGIN PASSWORD {quoted}")
    else:
        print(f"   AVISO: POSTGRES_RO_PASSWORD vacio, '{role}' se crea sin login")
        op.execute(f"CREATE ROLE {role} NOLOGIN")

    for schema in readable:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
        op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}")
        op.execute(
            f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
            f"GRANT SELECT ON TABLES TO {role}"
        )

    print(f"   Rol {role}: SELECT en {', '.join(readable)} (sin acceso a silver)")


def downgrade() -> None:
    """Borra los tres schemas con todo su contenido.

    `CASCADE` en vez de ir tabla por tabla: es un baseline, asi que bajar de
    aqui significa dejar la base vacia. El rol `api_ro` se conserva --
    los roles son objetos del cluster, no de la base, y borrarlo podria
    afectar a otra base del mismo servidor.
    """
    from src.shared.db.base import SCHEMAS

    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
