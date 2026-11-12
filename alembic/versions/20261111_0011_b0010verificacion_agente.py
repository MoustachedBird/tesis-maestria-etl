"""Crea el schema `verificacion`: lo que el agente observa en linea

`gold.negocios_clee_oficial.cerrado` es una hipotesis --"dejo de aparecer
en el DENUE, luego quebro"-- y hasta aqui no habia donde guardar la prueba.
Esta revision crea el cuarto schema de la base y sus cuatro tablas:

    verificacion.muestras           una tirada: tamano, semilla, reglas de gold
    verificacion.muestra_negocios   los negocios que salieron, con `cerrado`
                                    tal como estaba en gold al muestrear
    verificacion.veredictos         activo | cerrado | indeterminado, con
                                    confianza y razonamiento
    verificacion.evidencias         cada pieza en que se apoyo el agente

POR QUE UN SCHEMA Y NO TABLAS EN `gold`
--------------------------------------
Todo lo de `gold` es derivable y se trunca con `--force`. Un veredicto no:
costo busquedas y tiempo de agente, y rehacerlo no daria lo mismo porque la
web cambia. Tiene que sobrevivir a cualquier reconstruccion de `gold`, y la
forma estructural de decirlo es otro schema sin FK hacia `gold` (una FK
impediria truncar la cohorte mientras exista un veredicto). El join es
logico, por `clee`. Ademas lo escribe OTRO actor (el agente, no el ETL),
igual que cada `silver.clee_<metodo>` la escribe solo su metodo.

`Layer` ya trae `VERIFICACION`, asi que en una base nueva `b0000` crea el
schema desde el baseline (itera `SCHEMAS`); aqui el `IF NOT EXISTS` cubre
los dos caminos.

`api_ro` recibe SELECT: los veredictos son datos de consumo, igual que
`gold`, y el API o un tablero los leen con el mismo rol.

Las tablas nacen VACIAS. Quien las llena es el muestreo y el agente
(`verificacion_cierre.md`), no una migracion.

Revision ID: b0010verificacion_agente
Revises: b0009gold_cohorte_oficial
Create Date: 2026-11-11

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b0010verificacion_agente"
down_revision: str | Sequence[str] | None = "b0009gold_cohorte_oficial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COMENTARIOS = {
    "muestras": (
        "Una tirada de la muestra que se verifica en linea: cuantos negocios, con que "
        "semilla y bajo que version de las reglas de gold. Reproducible con la semilla"
    ),
    "muestra_negocios": (
        "Los negocios que salieron en una muestra, con la HIPOTESIS congelada "
        "(cerrado_gold) tal como estaba en gold.negocios_clee_oficial al muestrear. "
        "Join logico por clee: sin FK, para que gold se pueda truncar"
    ),
    "veredictos": (
        "Lo que el agente concluyo de un negocio buscandolo en linea: activo, cerrado o "
        "indeterminado, con confianza y razonamiento. Puede haber mas de uno por negocio"
    ),
    "evidencias": (
        "Cada pieza en que se apoyo un veredicto: fuente, URL, fecha, extracto citado y "
        "hacia donde apunta por si sola"
    ),
}
"""El grano de cada tabla, en el COMMENT: donde ocurriria la confusion (un `\\d+`)."""


def _grant_lectura(conn, schema: str) -> None:
    """SELECT para `api_ro` en el schema nuevo, si el rol existe.

    Mismo patron que `b0000`: USAGE + SELECT sobre lo que hay + DEFAULT
    PRIVILEGES para lo que se cree despues. Si el rol no existe (base de
    pruebas levantada a mano) no hay nada que otorgar y no es un error.
    """
    from src.shared.config import settings

    role = settings.POSTGRES_RO_USER
    owner = settings.POSTGRES_USER
    existe = conn.execute(
        sa.text("SELECT 1 FROM pg_roles WHERE rolname = :role"), {"role": role}
    ).scalar()
    if not existe:
        print(f"   {schema}: el rol {role} no existe, sin GRANT")
        return
    op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    op.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO {role}")
    op.execute(
        f"ALTER DEFAULT PRIVILEGES FOR ROLE {owner} IN SCHEMA {schema} "
        f"GRANT SELECT ON TABLES TO {role}"
    )
    print(f"   {schema}: SELECT para {role}")


def upgrade() -> None:
    """Crea el schema y sus cuatro tablas desde el metadata del ORM."""
    from src.shared.db.base import Base, tables_for
    from src.shared.db.models import verificacion  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.VERIFICACION.value
    conn = op.get_bind()

    op.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
    tablas = tables_for(schema)
    # `checkfirst=True`, al reves que b0009: esto NO se tira antes de crear.
    # Lo que haya dentro no es derivable, asi que si la tabla ya existe se
    # queda como esta. Es la unica revision de la cadena con esa regla.
    Base.metadata.create_all(bind=conn, tables=tablas, checkfirst=True)
    print(
        f"   {schema}: {len(tablas)} tablas creadas, vacias ({', '.join(t.name for t in tablas)})"
    )

    for tabla, comentario in COMENTARIOS.items():
        op.execute(f"COMMENT ON TABLE {schema}.{tabla} IS '{comentario}'")

    _grant_lectura(conn, schema)
    print("   para llenarlas: el muestreo y el agente (verificacion_cierre.md)")


def downgrade() -> None:
    """Tira el schema completo.

    CASCADE porque bajar de aqui significa que la verificacion no existe; lo
    que habia dentro NO es derivable, asi que esto se corre a sabiendas.
    """
    from src.shared.enums import Layer

    op.execute(f"DROP SCHEMA IF EXISTS {Layer.VERIFICACION.value} CASCADE")
