"""Permanencia de cada establecimiento en el directorio — una tabla por método.

El grano cambia respecto a `silver.establecimientos`, y esa es toda la razón
de ser de estas tablas:

    silver.establecimientos    82.9M   una fila cada vez que INEGI lo vio
    silver.permanencia_<m>     ~10M    una fila por establecimiento

Cada fila responde "¿desde cuándo y hasta cuándo estuvo este establecimiento
en el directorio?", que es lo que el modelo de supervivencia necesita y lo
que la tabla de hechos no puede dar sin un `GROUP BY` de minutos sobre 82.9M
filas unidas a la tabla de identidades.

**No guardan el veredicto de cierre, solo el tiempo observado.** Eso es
deliberado: el protocolo de la tesis define la regla de cierre como
*preliminar* --"se considerará cerrado el establecimiento que deja de
aparecer en dos cortes consecutivos"-- y la validación externa puede
cambiarla. Guardar `cerrado: bool` obligaría a reconstruir 10M de filas con
cada ajuste de la regla; guardar los ingredientes (`idx_primer`,
`idx_ultimo`, `cortes_observados`, `huecos`, `cortes_tras_ultimo`) permite
aplicar cualquier regla aguas abajo sin tocar Silver.

Una tabla por método y no una con columna `metodo`, por el mismo motivo que
`silver.clee_radio` y `silver.clee_h3`: el método va en el `FROM`, así que
una consulta no puede mezclarlos por descuido. Con una sola tabla, un
`WHERE metodo = 'radio'` olvidado devuelve dos filas por establecimiento y
los agregados salen al doble, en silencio y sin error.

**Ningún valor de un corte concreto.** La tabla guarda solo tres clases de
columna, y las tres son propiedades de la vida completa del
establecimiento:

1. **La ventana y su censura** (`idx_primer`, `cortes_tras_ultimo`, ...).
2. **Agregados de trayectoria**: cuánto varió algo, no en qué valor quedó
   (`n_nombres`, `cambio_municipio`, `delta_empleados_min`,
   `desplazamiento_neto_m`, el rectángulo envolvente).
3. **Cobertura del dato** (`tiene_*`): si el campo vino lleno en al menos
   un corte. Los datos del DENUE son de encuesta y cualquier campo puede
   venir vacío, o faltar en un periodo completo; estas banderas dicen de
   qué establecimientos se puede afirmar algo y de cuáles no.

Lo que **no** hay es el valor "del corte más reciente" de ningún campo
oficial --nombre, actividad, municipio, estrato de personal, tipo de
unidad, coordenadas, asentamiento--. Un valor así congela un instante de
algo que cambia entre cortes, y encima es el peor de los dos mundos: ocupa
espacio en 10M filas y de todos modos no contesta "¿qué valores tuvo?".
Los valores, TODOS y corte por corte, salen de unir `clee_resuelto` con la
tabla de identidad del método y de ahí a los hechos:

    permanencia_<m>  ←many:1─  clee_<m>  ─1:1→  establecimientos

No hace falta tabla puente: `clee_<m>` lo es, y tiene índice por
`clee_resuelto`. La consulta está en `AGENTS.md` §N3 y ejecutable en
`tests/unit/test_permanencia.py:variantes()`.
"""

from datetime import date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    Double,
    Index,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, declared_attr, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.SILVER.value
"""Derivadas de Silver y consumidas por el análisis: siguen siendo Silver.

La capa de consumo congelada y fechada es Gold (Fase 5), que aplicará la
regla de cierre sobre el método elegido.
"""


class PermanenciaMixin:
    """Núcleo común a toda tabla de permanencia.

    Se declara una vez para que agregar un método (p. ej. `permanencia_llm`)
    sea una tabla y una migración, sin repetir treinta columnas.
    """

    # ===== IDENTIDAD =====
    clee_resuelto: Mapped[str] = mapped_column(
        String(50), primary_key=True, comment="Identidad resuelta por este metodo"
    )
    es_sintetica: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si la identidad es NEW_* (sin pareja encontrada)"
    )

    # ===== VENTANA OBSERVADA =====
    # Las fechas para reportar; los indices para calcular. Los cortes del
    # DENUE distan entre 4 y 12 meses, asi que el protocolo mide el tiempo
    # en PERIODOS entre cortes y no en dias: `idx_*` es la posicion en la
    # secuencia ordenada de cortes (0 = el mas antiguo).
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Primer corte en que se observo"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Ultimo corte en que se observo"
    )
    idx_primer: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Posicion del primer corte en la secuencia (0-based)"
    )
    idx_ultimo: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Posicion del ultimo corte en la secuencia"
    )
    cortes_observados: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="En cuantos cortes distintos aparecio"
    )
    huecos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes dentro de su ventana en que NO aparecio: "
            "(idx_ultimo - idx_primer + 1) - cortes_observados. "
            "Mayor que 0 = desaparecio y volvio"
        ),
    )
    cortes_tras_ultimo: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes publicados despues del ultimo en que aparecio. "
            "0 = presente en el corte final (censura por la derecha); "
            "es el campo sobre el que se aplica la regla de cierre"
        ),
    )
    duracion_periodos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes transcurridos entre el primero y el ultimo en que se observo "
            "(idx_ultimo - idx_primer). Es la VARIABLE DE TIEMPO del modelo de "
            "supervivencia, y difiere de cortes_observados cuando hubo huecos"
        ),
    )
    presente_desde_el_inicio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si ya estaba en la primera edicion disponible: su antiguedad real "
            "es DESCONOCIDA (censura por la izquierda). El protocolo exige este "
            "indicador explicito, porque la antiguedad se mide desde el primer corte "
            "observado y para estas unidades ese corte no es su inicio"
        ),
    )
    fecha_alta_date: Mapped[date | None] = mapped_column(
        Date, comment="Fecha de alta en el RENEM mas antigua observada (no inicio de operaciones)"
    )
    dias_alta_a_primera: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Dias entre fecha_alta_date y primer_periodo. Diagnostico de la calidad "
            "de la fecha de alta: un valor NEGATIVO es inconsistente (alta posterior "
            "a la primera aparicion), y uno muy grande indica que el negocio existia "
            "mucho antes de que el directorio lo viera"
        ),
    )

    # ===== TRAYECTORIA DE TAMANO =====
    # El protocolo nombra el tamano como variable del establecimiento, pero
    # lo que se materializa es como CAMBIO, no en que estrato quedo: el
    # estrato de un corte concreto se recupera por el join.
    cambio_tamano: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de estrato de personal entre cortes"
    )
    delta_empleados_min: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Cota inferior de empleados del ULTIMO corte menos la del PRIMERO. "
            "Positivo = crecio, negativo = se encogio. Es una medida de "
            "trayectoria, no de nivel, y por eso respeta el orden temporal"
        ),
    )

    # ===== PATRON DE PRESENCIA Y CONFIANZA EN LA IDENTIDAD =====
    cortes_idx: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer),
        comment=(
            "Indices EXACTOS de los cortes en que aparecio, ordenados. De aqui se "
            "derivan duracion, huecos y cualquier patron de ausencia que el analisis "
            "quiera medir, sin volver a la tabla de hechos"
        ),
    )
    cortes_directo: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes en que la identidad vino de INEGI (origen=DIRECTO). Mide cuanto "
            "de la identidad es OBSERVADO frente a inferido: 10 cortes directos son "
            "mas confiables que 1 directo y 9 propagados"
        ),
    )
    cortes_propagado: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cortes en que la identidad se infirio por similitud"
    )

    # ===== RESUMEN DE VARIANTES =====
    # Conteos y banderas, no los valores: los valores mismos se recuperan
    # uniendo `clee_resuelto` con la tabla de identidad y de ahi a los
    # hechos. Materializar los arrays duplicaria ~3-4 GB de datos que ese
    # join ya entrega, y cargaria doce arrays mas al GROUP BY de 10M grupos.
    #
    # Lo que se queda son los ESCALARES, por dos razones: responden las
    # preguntas agregadas sobre los 10M de un golpe ("cuantos cambiaron de
    # nombre"), y son lo que un modelo puede consumir -- un `text[]` no entra
    # a un arbol de decision sin codificar.
    #
    # Y no son adorno: un establecimiento que cambio de municipio Y de
    # actividad es casi seguro una fusion indebida de la resolucion.
    n_nombres: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos nombres distintos tuvo"
    )
    cambio_actividad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de actividad economica entre cortes"
    )
    cambio_municipio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si cambio de municipio: la senal mas fuerte de fusion indebida",
    )
    n_direcciones: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos domicilios distintos tuvo"
    )

    # ===== COBERTURA DEL DATO: SI LO HUBO, no cual =====
    # Una bandera por campo oficial: True si vino lleno (no nulo y no cadena
    # vacia) en AL MENOS UN corte. Son UNION sobre toda la vida, no del corte
    # mas reciente, y eso es lo que las hace utiles: para verificar si un
    # negocio cerro sirve cualquier telefono que haya publicado, aunque fuera
    # en 2016.
    #
    # No son un sustituto del valor, son otra pregunta: "de este
    # establecimiento, se puede afirmar algo sobre este campo?". Los datos
    # del DENUE son de encuesta y cualquier campo puede venir vacio o faltar
    # en un periodo completo, asi que la cobertura es informacion de primera
    # clase -- y la que decide si una unidad entra o no a un analisis que
    # dependa de ese campo.
    tiene_nombre: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si publico nombre comercial en algun corte"
    )
    tiene_razon_social: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si publico razon social en algun corte. Tipicamente solo la tienen "
            "las unidades formalmente constituidas, asi que correlaciona con formalidad"
        ),
    )
    tiene_actividad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si tuvo codigo SCIAN en algun corte"
    )
    tiene_municipio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si tuvo entidad y municipio en algun corte"
    )
    tiene_localidad: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si tuvo clave de localidad en algun corte (distingue urbano de rural)",
    )
    tiene_ageb: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si tuvo AGEB en algun corte. Es la llave con que se une a los "
            "indices de marginacion y a los datos censales, asi que en False "
            "significa que esa unidad no puede entrar a ese analisis"
        ),
    )
    tiene_cod_postal: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si publico codigo postal en algun corte"
    )
    tiene_tipo_asent: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si tuvo tipo de asentamiento en algun corte"
    )
    tiene_coordenadas: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si tuvo latitud Y longitud en algun corte. En False, las medidas "
            "geograficas de esta fila son todas nulas por construccion"
        ),
    )
    tiene_estrato_personal: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si tuvo estrato de personal ocupado en algun corte. En False, "
            "`cambio_tamano` y `delta_empleados_min` no significan nada"
        ),
    )
    tiene_tipounieco: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si tuvo tipo de unidad economica en algun corte"
    )
    tiene_telefono: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si publico telefono en algun corte"
    )
    tiene_correo: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si publico correo en algun corte"
    )
    tiene_sitio: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si publico sitio web en algun corte"
    )
    en_centro_comercial: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si en algun corte estuvo dentro de un centro comercial o mercado",
    )

    # ===== GEOGRAFIA: DOS MEDIDAS, QUE RESPONDEN COSAS DISTINTAS =====
    # Las coordenadas de un corte concreto NO se guardan (ni la primera ni la
    # ultima): salen del join. Lo que se materializa son las medidas sobre
    # toda la vida, que es lo que no se puede recomputar sin barrer los
    # 82.9M. El rectangulo envolvente ademas sirve de ubicacion aproximada
    # --su centro-- cuando basta con saber donde esta.
    n_posiciones: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Pares (lat, lon) distintos que tuvo"
    )
    desplazamiento_neto_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Distancia entre la PRIMERA y la ULTIMA posicion observada, en metros. "
            "Son dos puntos REALES y respeta el orden temporal: responde "
            "'termino en otro lado?'"
        ),
    )
    lat_min: Mapped[float | None] = mapped_column(Double, comment="Latitud minima observada")
    lat_max: Mapped[float | None] = mapped_column(Double, comment="Latitud maxima observada")
    lon_min: Mapped[float | None] = mapped_column(Double, comment="Longitud minima observada")
    lon_max: Mapped[float | None] = mapped_column(Double, comment="Longitud maxima observada")
    dispersion_max_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Diagonal del rectangulo que contiene todas sus coordenadas. Es una "
            "COTA SUPERIOR de cuanto se movio, no una distancia recorrida: sus "
            "esquinas pueden ser puntos donde el negocio nunca estuvo, y es ciega "
            "al orden temporal. Se lee JUNTO al neto -- neto ~0 con dispersion "
            "grande es jitter de GPS o una coordenada mala; neto grande y "
            "dispersion similar es una mudanza real"
        ),
    )

    construida_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        comment="Cuando se construyo esta fila: la tabla es derivada y se reconstruye entera",
    )

    @declared_attr.directive
    def __table_args__(cls) -> tuple:
        """Índices del acceso real, iguales para todo método.

        No se particiona: el grano es el establecimiento, que **cruza** los
        periodos, así que no hay clave de partición natural. Son ~10M filas
        angostas y el acceso es por la ventana y la censura.

        **No hay índice geográfico ni por actividad**, y no es un olvido:
        esas columnas ya no viven aquí. Estratificar por sector o por
        municipio se hace uniendo a `clee_<metodo>` y a los hechos, donde
        `ix_estab_geo` e `ix_estab_activity_period` sí existen.
        """
        tabla = cls.__tablename__
        return (
            # El analisis de supervivencia filtra y estratifica por aqui.
            Index(f"ix_{tabla}_ventana", "idx_primer", "idx_ultimo"),
            Index(f"ix_{tabla}_censura", "cortes_tras_ultimo"),
            # Separa lo que INEGI identifico de lo que el pipeline infirio.
            Index(f"ix_{tabla}_sintetica", "es_sintetica"),
            {"schema": SCHEMA},
        )


class PermanenciaRadio(PermanenciaMixin, Base):
    """Permanencia según la identidad resuelta por radio/BallTree.

    El método de referencia de la tesis: es de esta tabla de la que sale la
    unidad de análisis.
    """

    __tablename__ = "permanencia_radio"

    def __repr__(self) -> str:
        return (
            f"<PermanenciaRadio({self.clee_resuelto}, "
            f"{self.primer_periodo}..{self.ultimo_periodo}, n={self.cortes_observados})>"
        )


class PermanenciaH3(PermanenciaMixin, Base):
    """Permanencia según la identidad resuelta por blocking H3.

    Pista de comparación: se mide contra `permanencia_radio` para saber si el
    blocking reproduce la misma ventana de vida.
    """

    __tablename__ = "permanencia_h3"

    def __repr__(self) -> str:
        return (
            f"<PermanenciaH3({self.clee_resuelto}, "
            f"{self.primer_periodo}..{self.ultimo_periodo}, n={self.cortes_observados})>"
        )
