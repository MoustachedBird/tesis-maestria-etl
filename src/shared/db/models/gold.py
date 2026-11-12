"""Capa de consumo: el negocio, su edad y si cerró.

El grano cambia dos veces a lo largo del pipeline, y por eso los nombres de
las tres capas no se parecen:

    silver.establecimientos          82,896,807   una observación (negocio, corte)
    silver.permanencia_<metodo>          ~15.6M   un negocio, ventana de 20 cortes
    gold.negocios_clee_oficial            9.58M   un negocio de la cohorte oficial

**Para qué existe esta tabla, y es una sola cosa:** calcular la **edad** de un
negocio y decidir si **cerró**, con los extremos de su trayectoria al lado
para poder interpretarlo. De ahí se saca una muestra aleatoria y se verifica
con agentes si la premisa "dejó de aparecer en el DENUE ⇒ quebró" es
defendible. Lo que no sirva a eso no va aquí.

**Lo que esta tabla NO es: una copia de `permanencia`.** Ese fue el error del
primer diseño, y era literal: ventana, censura, `cortes_idx`,
`fecha_alta_date`, `cambio_tamano`, `delta_empleados_min`, `n_nombres`,
`n_direcciones`, `cambio_municipio`, el rectángulo envolvente y quince
banderas `tiene_*` existían ya todas en `silver.permanencia_<metodo>`. Gold
agregaba la renumeración de la ventana oficial y poco más, así que no
justificaba su existencia.

Lo que sí la justifica son tres cosas que `permanencia` no tiene:

1. **Los EXTREMOS de la trayectoria, materializados.** `permanencia` consume
   el primer y el último estado para calcular `delta_empleados_min` y el
   desplazamiento neto, y después los tira. Aquí se quedan: empleados y
   tamaño inicial y final, posición inicial y final. No contradice `b0007`
   --no son "el valor de un corte cualquiera", son los dos extremos de la
   vida, que es una propiedad de la trayectoria completa.
2. **La EDAD.** Nadie la calculaba. Va de `fecha_alta` al último corte en que
   se observó, y para un negocio activo eso es una edad **censurada por la
   derecha**, que es exactamente lo que el modelo de supervivencia consume.
3. **El VEREDICTO de cierre, grabado y versionado.** El proyecto venía
   diciendo que la regla era preliminar y se derivaba en cada consulta. Eso
   deja de servir en el momento en que el objetivo es **validar la regla**:
   no se puede muestrear y verificar un veredicto que no se escribió.

**No hay tabla de variantes.** La tuvo --trece campos, ~100M filas-- y se
eliminó: los valores distintos de cualquier campo salen de
`silver.establecimientos` filtrando por `clee`, que los tiene de los nueve
cortes y mejor tipados. Es el insumo del LLM que unifica el nombre y de los
agentes que buscan el negocio en línea:

    SELECT DISTINCT nom_estab, telefono, www
    FROM silver.establecimientos
    WHERE clee = '14...' AND nom_estab <> '';

**Tampoco hay banderas `tiene_*`.** Con los valores concretos materializados,
un `NULL` dice lo mismo que decían las quince columnas.

**La cohorte: solo los cortes en que INEGI publica el CLEE.** Medido el
2026-11-08 son **9 cortes** (2021-05 → 2026-05) con cobertura del 100%,
frente a 11 cortes al 0%; suman 51.5M de las 82.9M observaciones. Ahí la
identidad es **observada y no inferida**, así que no hay `NEW_*` que
consolidar ni error de propagación que arrastrar. El alcance está en
`gold_cohorte_2021.md`.

**No se llama `gold.establecimientos`** porque el grano es distinto y el
nombre tiene que decirlo: con nombres distintos, confundir la N de 82.9M
observaciones con la de los negocios deja de ser posible
(`arquitectura.md` §2.3.1).
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    Index,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.GOLD.value
"""La capa que lee el API con el rol `api_ro`, que no tiene GRANT en silver."""

ORIGEN_NOMBRE_MODA = "moda"
"""El nombre lo eligió la regla determinista: el más frecuente entre cortes."""

ORIGEN_NOMBRE_LLM = "llm"
"""El nombre lo generó un LLM unificando las variantes.

La etapa no existe todavía. La columna se escribe desde el principio para que
cuando exista no haga falta una migración, y para que una fila siempre diga
quién eligió su nombre -- que es lo que vuelve auditable el resultado del LLM.
"""


class NegocioCleeOficial(Base):
    """Un negocio de la cohorte oficial: su vida, su edad y si cerró.

    **No se copia de `silver.permanencia_<metodo>`.** Esa tabla agrega sobre
    los 20 cortes y una fila suya mezcla los dos regímenes: un negocio visto
    en 2019 (identidad propagada) y en 2021 (oficial) comparte
    `clee_resuelto`, así que tiene `cortes_directo > 0` **y**
    `cortes_propagado > 0`. Filtrar por `cortes_directo > 0` daría la cohorte
    correcta con la ventana contaminada. Los `idx` además se renumeran dentro
    del subconjunto: heredar los de la secuencia de 20 rompería la edad y la
    censura sin un solo error visible (`gold_cohorte_2021.md` §4).

    **Cuatro columnas de `permanencia` no están, y su ausencia ES el
    invariante**: `es_sintetica` (siempre falsa), `origen` (siempre DIRECTO),
    `cortes_propagado` (siempre 0) y `cortes_directo` (igual a
    `cortes_observados`). Toda identidad de esta tabla la asignó INEGI.
    """

    __tablename__ = "negocios_clee_oficial"
    __table_args__ = (
        # El analisis de supervivencia filtra y estratifica por aqui.
        Index("ix_negocios_clee_oficial_cerrado", "cerrado"),
        Index("ix_negocios_clee_oficial_edad", "edad_anios"),
        # Para muestrear: donde `huecos > 0` la regla de cierre es mas fragil,
        # asi que el muestreo con agentes se estratifica por aqui.
        Index("ix_negocios_clee_oficial_huecos", "huecos"),
        # El tiempo de ausencia es el eje comparable, no el conteo de cortes.
        Index("ix_negocios_clee_oficial_ausencia", "meses_sin_aparecer"),
        # El cruce con SESNSP y con marginacion entra por la llave final.
        Index("ix_negocios_clee_oficial_geo_final", "cve_geo_final"),
        Index("ix_negocios_clee_oficial_actividad", "codigo_act"),
        # Separa lo que hay que mirar a mano de lo que se puede analizar.
        Index("ix_negocios_clee_oficial_sospechoso", "cluster_sospechoso"),
        {"schema": SCHEMA},
    )

    # ===== IDENTIDAD =====
    # Se llama `clee` y no `clee_resuelto` a proposito: en esta cohorte no se
    # resolvio nada, es el identificador que publico INEGI. La tabla de la
    # cohorte propagada (si se construye) si llevaria `clee_resuelto`, y esa
    # diferencia de nombre es documentacion: si ves `clee_resuelto`, estas en
    # terreno inferido.
    clee: Mapped[str] = mapped_column(
        String(50), primary_key=True, comment="CLEE oficial de INEGI; la identidad del negocio"
    )
    nombre: Mapped[str | None] = mapped_column(
        String,
        comment=(
            "Nombre unificado del negocio. NO ES COVARIABLE DEL MODELO: es campo "
            "de presentacion, para reportar y para que un agente pueda buscarlo en "
            "linea. Las variantes de las que sale se consultan en "
            "silver.establecimientos filtrando por clee"
        ),
    )
    nombre_origen: Mapped[str | None] = mapped_column(
        String(10),
        comment=(
            "Quien eligio el nombre: 'moda' (la regla deterministica) o 'llm'. Sin "
            "esto, un nombre generado y uno observado son indistinguibles"
        ),
    )
    n_nombres: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos nombres distintos tuvo entre los cortes"
    )

    # ===== VENTANA: en que cortes aparecio =====
    periodos: Mapped[list[date]] = mapped_column(
        ARRAY(Date),
        nullable=False,
        comment=(
            "Los cortes EXACTOS en que se observo, ordenados. De aqui sale cualquier "
            "patron de presencia --y los anios, con EXTRACT-- sin volver a los 82.9M "
            "hechos. Son a lo mas nueve fechas"
        ),
    )
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Primer corte oficial en que se observo"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Ultimo corte oficial en que se observo"
    )
    cortes_observados: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="En cuantos de los cortes oficiales aparecio"
    )
    huecos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes dentro de su ventana en que NO aparecio. Mayor que 0 significa "
            "que ya desaparecio y VOLVIO, o sea un caso probado de que ausencia no "
            "implica cierre. Es la medida del error de la regla con los propios "
            "datos, y por donde conviene estratificar el muestreo con agentes"
        ),
    )
    cortes_tras_ultimo: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes oficiales publicados despues del ultimo en que aparecio. 0 = "
            "presente en el corte final, o sea censurado por la derecha. La regla de "
            "cierre NO se aplica sobre esto sino sobre `meses_sin_aparecer`, porque "
            "contar cortes no es contar tiempo; se conserva porque es el patron de "
            "presencia que el analisis quiere ver junto a los meses"
        ),
    )

    # ===== EDAD =====
    fecha_alta: Mapped[date | None] = mapped_column(
        Date,
        comment=(
            "Fecha de alta en el RENEM mas antigua observada (MIN sobre los cortes). "
            "Es de REGISTRO y no de inicio de operaciones, pero es el unico proxy de "
            "antiguedad INDEPENDIENTE de la ventana de observacion -- y por eso es la "
            "base de la edad y no el primer corte observado"
        ),
    )
    edad_anios: Mapped[float | None] = mapped_column(
        Numeric(6, 2),
        comment=(
            "Anios de `fecha_alta` al ULTIMO CORTE EN QUE APARECIO, no a hoy: con "
            "'hoy' la tabla dejaria de ser reproducible. Para un negocio activo es "
            "una edad CENSURADA por la derecha, que es exactamente lo que el modelo "
            "de supervivencia consume. NULL si no hay fecha de alta"
        ),
    )
    dias_alta_a_primera: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Dias entre `fecha_alta` y `primer_periodo`. Diagnostico de la calidad de "
            "la fecha de alta: un valor NEGATIVO es inconsistente --el negocio "
            "aparecio antes de darse de alta-- y esas filas no deberian entrar a un "
            "analisis de antiguedad"
        ),
    )

    # ===== VEREDICTO =====
    meses_sin_aparecer: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Meses del ultimo corte en que aparecio al ULTIMO de la ventana. 0 = "
            "presente en la entrega final. Existe porque CONTAR CORTES NO ES CONTAR "
            "TIEMPO: los intervalos de la ventana son 6, 6, 6, 12, 6, 6, 6, 12 meses "
            "(faltan 2023-05 y 2025-11), asi que `cortes_tras_ultimo = 1` significa "
            "12 meses de evidencia para quien desaparecio en 2022-11 y solo 6 para "
            "quien desaparecio en 2024-05. Es la columna comparable, el INGREDIENTE "
            "sobre el que se aplica la regla de cierre (`>= CIERRE_MESES_AUSENTE`), "
            "y la que un modelo de supervivencia quiere como tiempo"
        ),
    )
    local_reocupado: Mapped[bool | None] = mapped_column(
        Boolean,
        comment=(
            "True si OTRO negocio aparecio en la misma celda H3 res 9 (~174 m) en un "
            "corte POSTERIOR al ultimo de este. No prueba el cierre --en una celda "
            "urbana caben decenas de negocios-- pero es evidencia corroborante de que "
            "el local siguio vivo con otro ocupante, que es justo lo que un verificador "
            "humano buscaria. Sirve para estratificar la muestra: donde esto es True, "
            "la hipotesis de cierre es mas creible. "
            "NULL = no se evaluo (el negocio no esta cerrado, o no tiene coordenadas)"
        ),
    )
    cerrado: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si lleva al menos `CIERRE_MESES_AUSENTE` meses sin aparecer (12 por "
            "default: dos ciclos semestrales del DENUE). ES UNA HIPOTESIS, no un hecho "
            "observado: el DENUE no reporta cierres, solo deja de listar. Validar esta "
            "columna con una muestra verificada en linea (schema `verificacion`) es el "
            "proposito de la tabla, asi que el umbral queda en `version_reglas` y los "
            "ingredientes (`meses_sin_aparecer`, `huecos`) se conservan. Un negocio "
            "ausente pero por menos meses que el umbral queda en False: no se afirma "
            "nada de el todavia (`cortes_tras_ultimo > 0 AND NOT cerrado`)"
        ),
    )

    # ===== EMPLEO: los dos extremos =====
    empleados_inicial: Mapped[int | None] = mapped_column(
        Integer, comment="Cota INFERIOR del estrato de personal en el primer corte observado"
    )
    empleados_final: Mapped[int | None] = mapped_column(
        Integer, comment="Cota inferior del estrato en el ultimo corte observado"
    )
    tamano_inicial: Mapped[str | None] = mapped_column(
        String(10), comment="Micro / Small / Medium / Large al aparecer"
    )
    tamano_final: Mapped[str | None] = mapped_column(
        String(10),
        comment="Micro / Small / Medium / Large al desaparecer (o en el ultimo corte)",
    )
    cambio_tamano: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si cambio de estrato en ALGUN momento, no solo entre los extremos",
    )

    # ===== GEOGRAFIA: los dos extremos, y si se movio =====
    # La llave va concatenada y jerarquica, cortada en el primer nivel
    # ausente: `cve_mun` no es unico a nivel nacional sin `cve_ent`, ni
    # `cve_loc` sin los dos, asi que guardarlos sueltos permitiria
    # reconstruir municipios que nunca existieron. El LARGO dice el nivel:
    # 2 entidad, 5 municipio, 9 localidad, mas AGEB.
    cve_geo_inicial: Mapped[str | None] = mapped_column(
        String(20), comment="Llave geografica al aparecer (ent|mun|loc|ageb concatenados)"
    )
    cve_geo_final: Mapped[str | None] = mapped_column(
        String(20),
        comment=(
            "Llave geografica al desaparecer. `LEFT(cve_geo_final, 5)` es la llave del "
            "cruce con la incidencia delictiva del SESNSP"
        ),
    )
    lat_inicial: Mapped[float | None] = mapped_column(Double)
    lon_inicial: Mapped[float | None] = mapped_column(Double)
    lat_final: Mapped[float | None] = mapped_column(Double)
    lon_final: Mapped[float | None] = mapped_column(Double)
    h3_cell_9_final: Mapped[int | None] = mapped_column(
        BigInteger,
        comment=(
            "Celda H3 res 9 (~174 m) de la ultima posicion. Se guarda porque NO se "
            "puede recalcular en SQL: no hay extension H3 en esta base, el calculo "
            "vive en Python (`transformers/enrich.py`)"
        ),
    )
    se_mudo: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si la llave geografica inicial y la final difieren a nivel de "
            "MUNICIPIO. Es mas estricto que comparar coordenadas: el ruido de GPS "
            "mueve el punto sin cambiar el municipio"
        ),
    )
    distancia_mudanza_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Distancia entre la PRIMERA y la ULTIMA posicion, en metros. Respeta el "
            "orden temporal, asi que responde 'termino en otro lado?' y no 'cuanto "
            "deambulo?'. Un valor grande con `se_mudo` en False es ruido de GPS"
        ),
    )
    n_posiciones: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Pares (lat, lon) distintos que tuvo. Varios con se_mudo=False es jitter",
    )

    # ===== ACTIVIDAD =====
    codigo_act: Mapped[str | None] = mapped_column(
        String(10),
        comment=(
            "SCIAN canonico: el ANCESTRO COMUN mas especifico de todas las actividades "
            "que tuvo, no la del ultimo corte. Por eso puede ser un prefijo (una rama, "
            "4 digitos) y no un codigo de clase. NULL si la discrepancia cruza rama: "
            "ahi hay que juzgar"
        ),
    )
    cambio_actividad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de actividad economica entre cortes"
    )

    # ===== CALIDAD DE LA IDENTIDAD =====
    cluster_sospechoso: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si `n_nombres` excede el numero de cortes de la ventana, lo que es "
            "IMPOSIBLE para un cluster correcto (un establecimiento aparece una vez "
            "por corte): significa que la identidad agrupo mas de un establecimiento. "
            "Esas filas no se nombran y no deberian entrar al analisis ni a la muestra"
        ),
    )

    # ===== PROCEDENCIA: lo que hace citable a la fila =====
    ventana_cortes: Mapped[list[date]] = mapped_column(
        ARRAY(Date),
        nullable=False,
        comment=(
            "Los cortes oficiales con que se construyo ESTA version. Si manana entra "
            "un corte nuevo, la N, las edades y todas las tasas cambian, y esto es lo "
            "que lo explica. Sin esto la fila no es citable"
        ),
    )
    metodo_base: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment=(
            "De donde salio la identidad. En esta cohorte es `inegi`: el `clee` viene "
            "en los propios hechos, asi que no se leyo ninguna silver.clee_<metodo> y "
            "no hubo nada que resolver"
        ),
    )
    version_reglas: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment=(
            "Version de las reglas: el umbral de cierre, como se elige el nombre y "
            "como se calcula la edad. Cambiar cualquiera cambia los numeros, asi que "
            "subir esto es parte del cambio"
        ),
    )
    construida_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        estado = "cerrado" if self.cerrado else "activo"
        return f"<NegocioCleeOficial({self.clee}, {estado}, {self.edad_anios} anios)>"
