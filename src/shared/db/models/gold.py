"""Capa de consumo: el negocio como unidad de análisis, ya resuelto.

El grano cambia dos veces a lo largo del pipeline, y por eso los nombres de
las tres capas no se parecen:

    silver.establecimientos          82,896,807   una observación (negocio, corte)
    silver.permanencia_<metodo>          ~15.6M   un negocio, ventana de 20 cortes
    gold.negocios_clee_oficial              (?)   un negocio de la cohorte oficial

**La cohorte: solo los cortes en que INEGI publica el CLEE.** Medido el
2026-11-08 son **9 cortes** (2021-05 → 2026-05) con cobertura del 100%,
frente a 11 cortes al 0%; los oficiales suman 51.5M de las 82.9M
observaciones, o sea el 62%. Ahí la identidad es **observada y no
inferida**, así que no hay `NEW_*` que consolidar, no hay error de
propagación y los tres métodos de resolución coinciden trivialmente. El
alcance completo y lo que cuesta están en `gold_cohorte_2021.md`.

**Esta capa SÍ materializa valores**, al contrario de Silver. `b0007` sacó
de `permanencia` el valor "del corte más reciente" porque esas tablas son
reconstruibles a voluntad y el join entrega mejor. Gold es lo contrario por
definición: es la capa **citable**, el artefacto que la tesis reporta, y
tiene que ser estable aunque la identidad se re-resuelva después. De ahí que
cada fila lleve fecha, ventana y versión de reglas.

    silver   reconstruible a voluntad  →  agregados + cobertura, sin snapshots
    gold     congelado y fechado       →  un valor canónico por campo, con procedencia

**No se llama `gold.establecimientos`.** `arquitectura.md` §2.3.1 eligió ese
nombre simétrico y dejó anotada la alternativa; se adopta la alternativa
(2026-11-07) porque el grano es distinto y el nombre tiene que decirlo. El
beneficio es estructural, no estético: con nombres distintos, confundir la N
de 82.9M observaciones con la de los negocios deja de ser posible y las tres
salvaguardas de §2.3.1 dejan de depender de la disciplina de quien escribe
la consulta.

Las reglas de normalización por campo —de qué corte sale cada valor, cómo se
elige el SCIAN, dónde interviene un LLM— están en `normalizacion_gold.md`
§§4-6 y no se repiten aquí.
"""

from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    String,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from src.shared.db.base import Base
from src.shared.enums import Layer

SCHEMA = Layer.GOLD.value
"""La capa que lee el API con el rol `api_ro`, que no tiene GRANT en silver."""

CAMPOS_CON_VARIANTES = (
    "nom_estab",
    "raz_social",
    "codigo_act",
    "telefono",
    "correoelec",
    "www",
)
"""Campos cuyos valores distintos se listan en la tabla de variantes.

El criterio para entrar, de `normalizacion_gold.md` §6.1: una variante se
materializa si es **la evidencia de una decisión que Gold tomó** (el nombre
canónico, el SCIAN elegido) o **el insumo de un consumidor que trabaja
negocio por negocio** (la cascada de verificación prueba cada teléfono y
cada correo, no solo el último).

Lo que NO entra, y por qué: el domicilio por componentes (ya está
`n_direcciones`), las coordenadas (ya están el rectángulo, el neto y la
dispersión), el estrato de personal (ya están `cambio_tamano` y el delta), y
`tipo_asent`/`cod_postal`/`cve_loc`/`ageb` (variantes sin consumidor). Es el
crecimiento de GB que `b0007` evitó en Silver.
"""


class NegocioCleeOficial(Base):
    """Un negocio de la cohorte con CLEE oficial, con su ventana de vida.

    La unidad de análisis de la tesis. Una fila por identidad que INEGI
    asignó, agregada **solo sobre los cortes oficiales**.

    **No se copia de `silver.permanencia_<metodo>`.** Esa tabla agrega sobre
    los 20 cortes y una fila suya mezcla los dos regímenes: un negocio visto
    en 2019 (identidad propagada) y en 2021 (oficial) comparte
    `clee_resuelto`, así que tiene `cortes_directo > 0` **y**
    `cortes_propagado > 0`. Filtrar por `cortes_directo > 0` daría la cohorte
    correcta con la ventana contaminada, y el error de propagación volvería
    por la puerta de atrás. Los `idx` además se renumeran dentro del
    subconjunto: heredar los de la secuencia de 20 rompería
    `presente_desde_el_inicio` y `duracion_periodos` sin un solo error
    visible (`gold_cohorte_2021.md` §4).

    **Cuatro columnas de `permanencia` no están, y su ausencia ES el
    invariante**: `es_sintetica` (siempre falsa), `origen` (siempre
    DIRECTO), `cortes_propagado` (siempre 0) y `cortes_directo` (igual a
    `cortes_observados`). Tenerlas sería ancho constante; no tenerlas dice
    que toda identidad de esta tabla la asignó INEGI.

    **Tampoco guarda el veredicto de cierre.** El protocolo llama
    *preliminar* a la regla, así que se deriva de `cortes_tras_ultimo` en la
    consulta que lo necesite — mismo criterio que en Silver, y mover el
    umbral no cuesta ni una columna.
    """

    __tablename__ = "negocios_clee_oficial"
    __table_args__ = (
        # El analisis de supervivencia filtra y estratifica por aqui.
        Index("ix_negocios_clee_oficial_censura", "cortes_tras_ultimo"),
        Index("ix_negocios_clee_oficial_ventana", "idx_primer", "idx_ultimo"),
        # A diferencia de `permanencia`, aqui estas columnas SI existen, asi
        # que el indice vale: estratificar por municipio o por sector es lo
        # que el analisis hace, y el cruce con SESNSP necesita la llave
        # geografica.
        Index("ix_negocios_clee_oficial_geo", "cve_ent", "cve_mun"),
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

    # ===== VENTANA OBSERVADA (relativa a los cortes OFICIALES) =====
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Primer corte oficial en que se observo"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Ultimo corte oficial en que se observo"
    )
    idx_primer: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Posicion del primer corte DENTRO DE LA VENTANA OFICIAL (0-based): "
            "0 = el primer corte con CLEE, no el primer corte del DENUE"
        ),
    )
    idx_ultimo: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Posicion del ultimo corte en la ventana oficial"
    )
    cortes_observados: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="En cuantos cortes oficiales aparecio"
    )
    huecos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes dentro de su ventana en que NO aparecio. Mayor que 0 = "
            "desaparecio y volvio, asi que una ausencia no implica cierre"
        ),
    )
    cortes_tras_ultimo: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Cortes oficiales publicados despues del ultimo en que aparecio. "
            "0 = presente en el corte final (censura por la derecha); es el campo "
            "sobre el que se aplica la regla de cierre"
        ),
    )
    duracion_periodos: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "idx_ultimo - idx_primer. Es la VARIABLE DE TIEMPO del modelo de "
            "supervivencia, en periodos y no en dias porque los cortes oficiales "
            "distan entre 6 y 12 meses (falta el corte 2023-05)"
        ),
    )
    presente_desde_el_inicio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si ya estaba en el primer corte oficial. OJO: entre 2020-11 y "
            "2021-05 la cobertura del DENUE paso de 3.22M a 5.52M observaciones "
            "(+71%, cambio de metodologia de INEGI), asi que para una parte de "
            "estos negocios esto significa 'INEGI empezo a verlo' y no 'ya "
            "existia'. La antiguedad real la aproxima fecha_alta_date"
        ),
    )
    cortes_idx: Mapped[list[int] | None] = mapped_column(
        ARRAY(Integer),
        comment=(
            "Indices EXACTOS de los cortes oficiales en que aparecio, ordenados. "
            "De aqui se deriva cualquier patron de ausencia sin volver a los hechos"
        ),
    )

    # ===== ANTIGUEDAD =====
    fecha_alta_date: Mapped[date | None] = mapped_column(
        Date,
        comment=(
            "Fecha de alta en el RENEM mas antigua observada. Es de REGISTRO y no "
            "de inicio de operaciones, pero es el unico proxy de antiguedad "
            "INDEPENDIENTE de la ventana de observacion -- y por eso es la "
            "covariable de antiguedad y no el primer corte observado"
        ),
    )
    dias_alta_a_primera: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Dias entre fecha_alta_date y primer_periodo. Diagnostico de la "
            "calidad de la fecha de alta: un valor NEGATIVO es inconsistente"
        ),
    )

    # ===== TRAYECTORIA: cuanto vario, no en que valor quedo =====
    cambio_tamano: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de estrato de personal entre cortes"
    )
    delta_empleados_min: Mapped[int | None] = mapped_column(
        Integer,
        comment=(
            "Cota inferior de empleados del ULTIMO corte menos la del PRIMERO. "
            "Positivo = crecio. Respeta el orden temporal, asi que distingue "
            "crecer de encogerse -- que un conteo de estratos distintos no puede"
        ),
    )
    n_nombres: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos nombres comerciales distintos tuvo"
    )
    n_direcciones: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Cuantos domicilios distintos tuvo"
    )
    cambio_actividad: Mapped[bool] = mapped_column(
        Boolean, nullable=False, comment="True si cambio de actividad economica entre cortes"
    )
    cambio_municipio: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si cambio de municipio: la senal mas fuerte de fusion indebida",
    )

    # ===== GEOGRAFIA: medidas sobre toda la vida =====
    n_posiciones: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Pares (lat, lon) distintos que tuvo"
    )
    desplazamiento_neto_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Distancia entre la PRIMERA y la ULTIMA posicion observada, en metros. "
            "Dos puntos reales y respeta el orden: responde 'termino en otro lado?'"
        ),
    )
    lat_min: Mapped[float | None] = mapped_column(Double, comment="Latitud minima observada")
    lat_max: Mapped[float | None] = mapped_column(Double, comment="Latitud maxima observada")
    lon_min: Mapped[float | None] = mapped_column(Double, comment="Longitud minima observada")
    lon_max: Mapped[float | None] = mapped_column(Double, comment="Longitud maxima observada")
    dispersion_max_m: Mapped[float | None] = mapped_column(
        Double,
        comment=(
            "Diagonal del rectangulo que contiene todas sus coordenadas: COTA "
            "SUPERIOR del movimiento, ciega al orden. Se lee junto al neto -- "
            "neto ~0 con dispersion grande es jitter de GPS, no una mudanza"
        ),
    )

    # ===== COBERTURA DEL DATO: si lo hubo, no cual =====
    # Union sobre toda la vida. No sustituyen al valor: contestan "de este
    # negocio se puede afirmar algo sobre este campo?", que es lo que decide
    # si entra a un analisis que dependa de el.
    tiene_nombre: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_razon_social: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="Tipicamente solo la tienen las unidades formalmente constituidas",
    )
    tiene_actividad: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_municipio: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_localidad: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_ageb: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "Llave para unir a marginacion y datos censales: en False, esa unidad "
            "no puede entrar a ese analisis"
        ),
    )
    tiene_cod_postal: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_tipo_asent: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_coordenadas: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="En False, las medidas geograficas de esta fila son nulas por construccion",
    )
    tiene_estrato_personal: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="En False, cambio_tamano y delta_empleados_min no significan nada",
    )
    tiene_tipounieco: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_telefono: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_correo: Mapped[bool] = mapped_column(Boolean, nullable=False)
    tiene_sitio: Mapped[bool] = mapped_column(Boolean, nullable=False)
    en_centro_comercial: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment="True si en algun corte estuvo dentro de un centro comercial o mercado",
    )

    # ===== VALORES CANONICOS =====
    # La diferencia con Silver: aqui SI hay un valor por campo, porque esta
    # capa es la citable. Las reglas de cual se elige estan en
    # `normalizacion_gold.md` §4.
    nombre: Mapped[str | None] = mapped_column(
        String,
        comment=(
            "Nombre canonico derivado de todas las variantes. NO ES COVARIABLE "
            "DEL MODELO: es campo de presentacion y puede haberlo generado un "
            "LLM. Las variantes de las que sale estan en la tabla hija, con "
            "es_canonico marcando esta"
        ),
    )
    razon_social: Mapped[str | None] = mapped_column(
        String, comment="La mas reciente no vacia. Dato legal: no se promedia ni se inventa"
    )
    codigo_act: Mapped[str | None] = mapped_column(
        String(10),
        comment=(
            "SCIAN canonico: el ancestro comun mas especifico en la jerarquia del "
            "catalogo cuando hubo varios. Solo si la discrepancia cruza rama "
            "interviene un LLM"
        ),
    )

    # Bloque de UBICACION: los campos de abajo salen TODOS del mismo corte,
    # el de referencia. Armarlo por partes --el CP del corte mas reciente que
    # lo tenga, la colonia de otro, las coordenadas del ultimo-- produciria un
    # domicilio que nunca existio (`normalizacion_gold.md` §5).
    corte_referencia: Mapped[date] = mapped_column(
        Date,
        nullable=False,
        comment=(
            "Corte del que sale TODO el bloque de ubicacion. Normalmente el "
            "ultimo observado; la excepcion es el jitter de GPS, donde se usa la "
            "mediana. Materializarlo es lo que hace verificable la coherencia"
        ),
    )
    cve_ent: Mapped[str | None] = mapped_column(String(2), comment="Entidad; llave del cruce SESNSP")
    cve_mun: Mapped[str | None] = mapped_column(
        String(3), comment="Municipio; llave del cruce con incidencia delictiva"
    )
    cve_loc: Mapped[str | None] = mapped_column(
        String(4), comment="Localidad: distingue urbano de rural"
    )
    ageb: Mapped[str | None] = mapped_column(
        String, comment="AGEB: llave hacia marginacion y datos censales"
    )
    cod_postal: Mapped[str | None] = mapped_column(String)
    tipo_asent: Mapped[str | None] = mapped_column(
        String, comment="Colonia, fraccionamiento, pueblo... El protocolo lo nombra"
    )
    latitud: Mapped[float | None] = mapped_column(Double)
    longitud: Mapped[float | None] = mapped_column(Double)
    h3_cell_9: Mapped[int | None] = mapped_column(
        BigInteger, comment="Celda H3 res 9 del corte de referencia (int64)"
    )

    # Tamano y tipo: variables del protocolo.
    per_ocu: Mapped[str | None] = mapped_column(
        String, comment="Estrato de personal ocupado en el corte de referencia"
    )
    categoria_tamano: Mapped[str | None] = mapped_column(
        String, comment="Micro / Small / Medium / Large: el sujeto mismo de la tesis"
    )
    empleados_min: Mapped[int | None] = mapped_column(Integer)
    empleados_max: Mapped[int | None] = mapped_column(Integer)
    tipounieco: Mapped[str | None] = mapped_column(
        String,
        comment=(
            "Fijo / semifijo / ambulante: dinamicas de supervivencia distintas, y "
            "determina si el negocio es verificable con fuentes externas"
        ),
    )

    # Contacto: el mas reciente no vacio de CADA UNO, independientemente. Que
    # vengan de cortes distintos no es inconsistencia: aqui no se describe un
    # lugar, se acumula una via de verificacion.
    telefono: Mapped[str | None] = mapped_column(String(50))
    correoelec: Mapped[str | None] = mapped_column(String(200))
    www: Mapped[str | None] = mapped_column(String(200))

    # ===== CALIDAD DE LA IDENTIDAD =====
    cluster_sospechoso: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si n_nombres excede el numero de cortes de la ventana, lo que es "
            "IMPOSIBLE para un cluster correcto (un establecimiento aparece una "
            "vez por corte): significa que la identidad agrupo mas de un "
            "establecimiento. Medido sobre los 20 cortes habia ~1,245 casos, uno "
            "con 345 nombres. No son rebranding, son fusiones indebidas -- no se "
            "canonicalizan y se reportan como limite de la resolucion"
        ),
    )

    # ===== PROCEDENCIA: lo que hace citable a la fila =====
    ventana_cortes: Mapped[list[date]] = mapped_column(
        ARRAY(Date),
        nullable=False,
        comment=(
            "Los cortes oficiales con que se construyo ESTA version. Si manana "
            "entra un corte nuevo, la N y todas las tasas cambian, y esto es lo "
            "que lo explica. Sin esto la fila no es citable"
        ),
    )
    metodo_base: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment=(
            "De que silver.clee_<metodo> salio la identidad. En esta cohorte los "
            "tres metodos coinciden (fase 1 deterministica), asi que es "
            "trazabilidad y no una eleccion"
        ),
    )
    version_reglas: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Version de las reglas de normalizacion. Cambiarlas cambia los numeros",
    )
    construida_en: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<NegocioCleeOficial({self.clee}, {self.primer_periodo}..{self.ultimo_periodo})>"


class NegocioCleeOficialVariante(Base):
    """Los valores distintos que tuvo un negocio en cada campo, con su ventana.

    Una fila por `(negocio, campo, valor)`. Es tabla y no una columna
    `text[]` por tres cosas que el array no da: **búsqueda difusa** con el
    índice trigram ("¿qué negocio es *tacos paco*?", que es la pregunta al
    verificar un caso a mano), **la cronología del rebranding** —`TACOS PACO
    (2021–2022) → TAQUERIA DON PACO (2023–)`, que sale del mismo `GROUP BY` y
    por tanto es gratis— y **un solo lugar para todos los campos** con el
    discriminador `campo`, en vez de una columna de array por cada uno.

    Para qué existe, en orden de peso:

    1. **Es la evidencia del valor canónico.** Si la tabla madre afirma que
       el negocio se llama `TAQUERIA DON PACO`, aquí se ve de qué salió y
       cuál ganó (`es_canonico`), sin comparar cadenas entre tablas.
    2. **Es el mejor diagnóstico de un mal cluster que existe.**
       `n_nombres = 2` no dice nada; ver `[TACOS PACO, FARMACIA PACO]` delata
       la fusión indebida al instante.
    3. **El verificador de supervivencia los necesita todos.** Buscar en
       Google Places funciona con un alias y falla con otro
       (`crear_agentes.md` §11.6).

    **Solo la cohorte oficial.** No contiene identidades propagadas ni
    sintéticas `NEW_*`. Si se construye la extensión de robustez, sus
    variantes van en `gold.negocios_clee_propagado_variantes` — una tabla por
    régimen, nunca una columna `cohorte`, por el mismo motivo que
    `clee_radio`/`clee_h3` son dos tablas: con una columna discriminadora un
    `WHERE` olvidado mezcla identidad observada con inferida y los agregados
    salen contaminados en silencio.

    Es, materializada y con índice, la sucesora de las vistas
    `variantes_<metodo>` que `b0006` eliminó: aquéllas eran una consulta
    guardada sobre Silver sin un solo consumidor.
    """

    __tablename__ = "negocios_clee_oficial_variantes"
    __table_args__ = (
        # Busqueda difusa sobre el valor. Requiere la extension pg_trgm, que
        # instala la migracion: es la primera extension del esquema.
        Index(
            "ix_negocios_clee_oficial_variantes_valor",
            "valor",
            postgresql_using="gin",
            postgresql_ops={"valor": "gin_trgm_ops"},
        ),
        # A lo mas UN canonico por (negocio, campo). Indice unico PARCIAL: lo
        # vuelve estructural en vez de convencion, porque con dos canonicos la
        # tabla madre seria indefendible y aqui el INSERT falla.
        Index(
            "ux_negocios_clee_oficial_variantes_canonico",
            "clee",
            "campo",
            unique=True,
            postgresql_where=text("es_canonico"),
        ),
        {"schema": SCHEMA},
    )

    clee: Mapped[str] = mapped_column(
        String(50),
        ForeignKey(f"{SCHEMA}.negocios_clee_oficial.clee", ondelete="CASCADE"),
        primary_key=True,
        comment="El negocio al que pertenece esta variante",
    )
    campo: Mapped[str] = mapped_column(
        String(20),
        primary_key=True,
        comment="Cual de los campos de CAMPOS_CON_VARIANTES vario",
    )
    valor: Mapped[str] = mapped_column(
        String,
        primary_key=True,
        comment=(
            "El valor tal como INEGI lo publico, sin normalizar: la normalizacion "
            "produce el canonico de la tabla madre, no reescribe la evidencia. "
            "Medido: max(nom_estab)=150 y max(raz_social)=249 caracteres, asi que "
            "la PK queda ~10x por debajo del tope de ~2704 bytes del btree"
        ),
    )
    primer_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Primer corte en que uso este valor"
    )
    ultimo_periodo: Mapped[date] = mapped_column(
        Date, nullable=False, comment="Ultimo corte en que lo uso"
    )
    n_cortes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "En cuantos cortes aparecio. Distingue 'se llamo asi cinco cortes' de "
            "'aparecio una vez', que huele a error de captura de la fuente"
        ),
    )
    es_canonico: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        comment=(
            "True si es el valor que publico la tabla madre. Es lo que cierra el "
            "circulo: la canonicalizacion queda auditable en el mismo lugar que su "
            "evidencia, venga de una regla o de un LLM"
        ),
    )

    def __repr__(self) -> str:
        return f"<NegocioCleeOficialVariante({self.clee}, {self.campo}={self.valor!r})>"
