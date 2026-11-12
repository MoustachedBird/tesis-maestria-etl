"""Crea gold.negocios_clee_oficial: la edad del negocio y si cerro

Primera y unica tabla de la capa de consumo. Hasta aqui `gold` era un schema
vacio con el rol de lectura ya otorgado (`b0000`), esperando que se disenara.

PARA QUE EXISTE, Y ES UNA SOLA COSA
-----------------------------------
Calcular la **edad** de un negocio y decidir si **cerro**, con los extremos
de su trayectoria al lado para poder interpretarlo. De ahi se saca una
muestra aleatoria y se verifica con agentes si la premisa "dejo de aparecer
en el DENUE => quebro" es defendible.

El alcance de la cohorte lo fija `gold_cohorte_2021.md`: SOLO los cortes en
que INEGI publica el CLEE. Medido el 2026-11-08 son 9 cortes (2021-05 ->
2026-05) con cobertura del 100% frente a 11 al 0%, y suman 51.5M de las 82.9M
observaciones. Ahi la identidad es OBSERVADA y no inferida, asi que no hay
`NEW_*` que consolidar ni error de propagacion que arrastrar.

LO QUE ESTA TABLA NO ES: UNA COPIA DE `permanencia`
---------------------------------------------------
Ese fue el error del primer diseno de Gold, y era literal: ventana, censura,
`cortes_idx`, `fecha_alta_date`, `cambio_tamano`, `delta_empleados_min`,
`n_nombres`, `n_direcciones`, `cambio_municipio`, el rectangulo envolvente y
quince banderas `tiene_*` existian ya todas en `silver.permanencia_<metodo>`.
Gold agregaba la renumeracion de la ventana oficial y poco mas.

Lo que si la justifica son tres cosas que `permanencia` no tiene:

    los EXTREMOS      `permanencia` consume el primer y el ultimo estado para
                      calcular el delta de empleados y el desplazamiento neto,
                      y despues los tira. Aqui se quedan: empleados y tamano
                      inicial y final, posicion inicial y final.
    la EDAD           nadie la calculaba. De `fecha_alta` al ultimo corte en
                      que se observo, lo que para un negocio activo es una
                      edad CENSURADA por la derecha -- exactamente lo que el
                      modelo de supervivencia consume.
    el VEREDICTO      `cerrado`, grabado y versionado. El proyecto venia
                      diciendo que la regla era preliminar y se derivaba en
                      cada consulta; eso deja de servir cuando el objetivo es
                      VALIDAR la regla, porque no se puede muestrear un
                      veredicto que no se escribio.

Los extremos no contradicen `b0007`: no son "el valor de un corte
cualquiera", son los dos extremos de la vida, que es una propiedad de la
trayectoria completa.

NO HAY TABLA DE VARIANTES
-------------------------
Un diseno intermedio la tuvo: trece campos y ~100M filas. Se elimina porque
los valores distintos de cualquier campo salen de `silver.establecimientos`
filtrando por `clee` --que los tiene de los nueve cortes y mejor tipados-- y
eso es lo que el LLM que unifica el nombre y los agentes que buscan en linea
necesitan:

    SELECT DISTINCT nom_estab, telefono, www
    FROM silver.establecimientos
    WHERE clee = '14...' AND nom_estab <> '';

Tampoco hay banderas `tiene_*`: con los valores concretos materializados, un
NULL dice lo mismo que decian las quince columnas.

Por lo mismo esta revision NO instala `pg_trgm`: la necesitaba el indice GIN
de busqueda difusa sobre la tabla de variantes, y sin esa tabla no hay nada
que indexar asi.

La tabla nace VACIA: poblarla es una operacion y no un cambio de esquema. Se
construye con `transform --only gold`.

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

TABLA = "negocios_clee_oficial"
"""La unica tabla de la capa."""

VARIANTES_RETIRADA = "negocios_clee_oficial_variantes"
"""Tabla de un diseno intermedio. El DROP la quita si la base viene de ahi."""


def upgrade() -> None:
    """Crea la tabla desde el metadata del ORM.

    El DDL sale del modelo y no de `op.create_table` escrito a mano, por la
    misma razon que el resto de la cadena: el esquema se declara en un solo
    lugar y la migracion no puede desincronizarse de el.

    **Tira antes de crear**, y no es paranoia: el esquema de Gold se rediseno
    varias veces sobre la marcha, asi que esta revision tiene que dejar la
    forma correcta aunque la base venga de una version anterior de ella
    misma. Es seguro porque el contenido es 100% derivable --se reconstruye
    con `transform --only gold`-- y porque nada consume Gold todavia.
    """
    from src.shared.db.base import Base
    from src.shared.db.models import gold  # noqa: F401  (puebla el metadata)
    from src.shared.enums import Layer

    schema = Layer.GOLD.value

    # La hija primero: su FK apuntaba a la madre. Si la base nunca la tuvo, el
    # IF EXISTS no hace nada.
    op.execute(f"DROP TABLE IF EXISTS {schema}.{VARIANTES_RETIRADA} CASCADE")
    op.execute(f"DROP TABLE IF EXISTS {schema}.{TABLA} CASCADE")

    tabla = Base.metadata.tables[f"{schema}.{TABLA}"]
    Base.metadata.create_all(bind=op.get_bind(), tables=[tabla], checkfirst=False)
    print(f"   gold: {TABLA} creada ({len(tabla.columns)} columnas, vacia)")
    print(f"   gold: {VARIANTES_RETIRADA} retirada (las variantes salen de silver)")

    # El grano en el COMMENT, que es donde ocurriria la confusion: en un `\d+`
    # o en una consulta ad-hoc. Es la tercera salvaguarda de
    # arquitectura.md §2.3.1.
    op.execute(
        f"COMMENT ON TABLE {schema}.{TABLA} IS "
        "'Un NEGOCIO de la cohorte con CLEE oficial de INEGI (9 cortes, "
        "2021-05..2026-05), con su EDAD y el veredicto de si CERRO. NO es "
        "silver.establecimientos, que son 82.9M OBSERVACIONES. `cerrado` es una "
        "HIPOTESIS a validar, no un hecho: el DENUE no reporta cierres, solo deja de "
        "listar. Los valores distintos de cualquier campo salen de "
        "silver.establecimientos filtrando por clee'"
    )

    print("   para poblarla: transform --only gold")


def downgrade() -> None:
    """Tira la tabla. El contenido es 100% derivable."""
    from src.shared.enums import Layer

    op.execute(f"DROP TABLE IF EXISTS {Layer.GOLD.value}.{TABLA} CASCADE")
