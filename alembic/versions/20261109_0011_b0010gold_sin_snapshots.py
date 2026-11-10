"""Saca de gold los valores de UN corte: solo agregados y banderas

Es `b0007` aplicado a Gold. En Silver ya se habia decidido que una tabla
derivada no guarda el valor de un corte --guarda agregados sobre la vida
completa y banderas `tiene_*`, y el valor se recupera por join-- y Gold habia
quedado con nueve columnas que violaban eso.

Las nueve que se van, y por que cada una era un snapshot:

    razon_social        "la mas reciente no vacia"
    per_ocu             del corte de referencia
    categoria_tamano    del corte de referencia
    empleados_min       del corte de referencia
    empleados_max       del corte de referencia
    tipounieco          del corte de referencia
    telefono            "el mas reciente no vacio"
    correoelec          "el mas reciente no vacio"
    www                 "el mas reciente no vacio"

Las tres de contacto eran el peor caso: un valor **sin fecha**, imposible de
situar en el tiempo y por tanto de defender. Las de tamano al menos venian
fechadas por `corte_referencia`, pero el tamano de un negocio es justo lo que
cambia, y congelar uno invita a tratarlo como atributo fijo cuando para el
modelo de supervivencia es una covariable VARIABLE EN EL TIEMPO.

Donde quedan ahora: en `negocios_clee_oficial_variantes`, **con su ventana**
(`primer_periodo`, `ultimo_periodo`, `n_cortes`), que es mas informacion que
la que se quita. `CAMPOS_CON_VARIANTES` pasa de seis campos a ocho: entran
`per_ocu` y `tipounieco`.

`categoria_tamano`, `empleados_min` y `empleados_max` NO entran a variantes:
se derivan de `per_ocu` uniendo `silver.denue_estratos_personal`, asi que
tenerlas seria repetir el catalogo.

Lo que SE QUEDA en la madre, y por que no contradice la regla:

    nombre          la MODA sobre los nueve cortes -> agregado
    codigo_act      el ANCESTRO COMUN de las actividades -> agregado
    bloque de ubicacion   un snapshot, si, pero el UNICO que ademas es LLAVE
                    DE JOIN: `cve_mun` cruza con SESNSP y `ageb` con
                    marginacion. Y no finge ser atemporal: `corte_referencia`
                    dice de que corte salio. Si se fuera, cada analisis
                    tendria que elegir un corte por su cuenta y esa regla
                    quedaria fuera de `version_reglas`.

Las dos tablas se recrean VACIAS desde el modelo, no se alteran columna por
columna: su contenido es 100% derivable y recrearlas garantiza que el
resultado sea identico al ORM, sin posibilidad de deriva. Hay que reconstruir
con `transform --only gold`.

Revision ID: b0010gold_sin_snapshots
Revises: b0009gold_cohorte_oficial
Create Date: 2026-11-09

"""

from collections.abc import Sequence

from alembic import op

revision: str = "b0010gold_sin_snapshots"
down_revision: str | Sequence[str] | None = "b0009gold_cohorte_oficial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLAS = ("negocios_clee_oficial", "negocios_clee_oficial_variantes")
"""En orden de dependencia: la hija tiene FK hacia la madre."""

SNAPSHOTS_RETIRADOS = (
    "razon_social",
    "per_ocu",
    "categoria_tamano",
    "empleados_min",
    "empleados_max",
    "tipounieco",
    "telefono",
    "correoelec",
    "www",
)
"""Las nueve columnas que dejan de existir, para poder reportarlo en el log."""


def _recrear(schema: str) -> None:
    """Tira las dos tablas de gold y las vuelve a crear desde el ORM.

    El `DROP` va en orden inverso al de dependencia: la hija primero, porque
    su FK apunta a la madre.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import gold  # noqa: F401  (puebla el metadata)

    for nombre in reversed(TABLAS):
        op.execute(f"DROP TABLE IF EXISTS {schema}.{nombre} CASCADE")

    tablas = [Base.metadata.tables[f"{schema}.{nombre}"] for nombre in TABLAS]
    Base.metadata.create_all(bind=op.get_bind(), tables=tablas, checkfirst=False)
    return tablas


def upgrade() -> None:
    """Recrea las dos tablas sin las columnas de snapshot.

    No hay `ALTER TABLE ... DROP COLUMN` porque no hace falta: el contenido se
    reconstruye entero, y recrear desde el metadata del ORM es lo que garantiza
    que el esquema resultante sea exactamente el declarado --el mismo criterio
    que el resto de la cadena.
    """
    from src.shared.db.models.gold import CAMPOS_CON_VARIANTES
    from src.shared.enums import Layer

    schema = Layer.GOLD.value
    tablas = _recrear(schema)

    print(f"   gold: retiradas {len(SNAPSHOTS_RETIRADOS)} columnas de un solo corte")
    print(f"     {', '.join(SNAPSHOTS_RETIRADOS)}")
    print(f"   ahora viven en {TABLAS[1]} con su ventana")
    print(f"   CAMPOS_CON_VARIANTES: {len(CAMPOS_CON_VARIANTES)} campos")
    for tabla in tablas:
        print(f"   gold: {tabla.name} recreada ({len(tabla.columns)} columnas, vacia)")

    # Los COMMENT se vuelven a poner: el DROP se los llevo.
    op.execute(
        f"COMMENT ON TABLE {schema}.negocios_clee_oficial IS "
        "'Un NEGOCIO de la cohorte con CLEE oficial de INEGI (9 cortes, 2021-05..2026-05). "
        "Solo AGREGADOS sobre la vida completa y banderas tiene_*: el valor de un corte "
        "concreto NO se materializa, vive en negocios_clee_oficial_variantes con su "
        "ventana. La excepcion es el bloque de ubicacion, fechado por corte_referencia, "
        "porque es la llave de los cruces con SESNSP y marginacion'"
    )
    op.execute(
        f"COMMENT ON TABLE {schema}.negocios_clee_oficial_variantes IS "
        "'Valores distintos que tuvo cada negocio por campo, con su ventana. es_canonico "
        "marca el que publico la tabla madre, y solo nom_estab y codigo_act tienen uno: "
        "los otros seis campos cambian entre cortes y la madre no elige ninguno'"
    )

    print("   para reconstruir: transform --only gold")


def downgrade() -> None:
    """Vuelve al esquema de `b0009`, con las nueve columnas de snapshot.

    Recrea las tablas desde el modelo de ESTA revision, asi que para bajar de
    verdad hay que tener el codigo de `b0009` --igual que cualquier migracion
    de la cadena que genera su DDL del ORM. Las tablas quedan vacias: el
    contenido es derivable.
    """
    from src.shared.enums import Layer

    _recrear(Layer.GOLD.value)
    print("   gold: tablas recreadas; reconstruir con transform --only gold")
