"""Construye `gold.negocios_clee_oficial` y su tabla de variantes.

La cohorte es **solo los cortes en que INEGI publica el CLEE**. Ahí la
identidad es observada y no inferida: no hay `NEW_*` que consolidar, no hay
error de propagación que arrastrar, y los tres métodos de resolución
coinciden trivialmente. Medido el 2026-11-08 son 9 cortes (2021-05 → 2026-05)
con cobertura del 100%, frente a 11 al 0%. El alcance está en
`gold_cohorte_2021.md`; las reglas por campo en `normalizacion_gold.md` §§4-6.

**No lee ninguna `silver.clee_<metodo>`.** La identidad sale del `clee` de
los propios hechos, que es lo que "oficial" significa. Por eso este loader no
depende de la etapa de resolución y por eso su reloj de frescura no es
`max(resuelto_en)` sino **la ventana**: lo que invalida esta tabla es que un
corte entre o salga de la lista de oficiales, no que alguien re-resuelva
identidades que aquí no se usan.

**Esta capa sí materializa valores**, al contrario de Silver. `b0007` sacó de
`permanencia` el valor "del corte más reciente" porque esas tablas son
reconstruibles a voluntad y el join entrega mejor; Gold es lo contrario por
definición: es la capa citable, el artefacto que la tesis reporta, y cada
fila lleva ventana, método y versión de reglas para que siga significando lo
mismo dentro de un año.

**Todo lo que escribe es determinista.** El nombre canónico es la *moda*
desempatada por el más reciente, que es el baseline obligatorio de
`normalizacion_gold.md` §6.4 --el LLM se adopta solo si gana en una muestra
etiquetada, y si empata se queda esto, que es gratis y reproducible. El SCIAN
canónico es el ancestro común por prefijo; cuando la discrepancia cruza rama
queda **NULL**, que es la señal de "esto hay que juzgarlo" y no un valor
inventado.

El troceo, el reanudar, el latido y el registro en `meta` son de
`etl/utils/troceado.py`: aquí solo está el SQL de esta capa.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.etl.utils.troceado import Agregado, Paso, construir, expr_bloque, latido
from src.meta.tracker import MetadataTracker
from src.shared.config import Settings, get_settings
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.models.gold import ORIGEN_NOMBRE_MODA, SCHEMA
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_gold.log"))

TABLA = "negocios_clee_oficial"
"""La tabla madre: un negocio de la cohorte por fila."""

JOB_NAME = "gold_negocios_clee_oficial"
"""Nombre de la corrida en `meta.job_runs`."""

JOB_TYPE = "GOLD"
"""Etapa propia, no `TRANSFORM` ni `PERMANENCIA`.

`/ops/health/pipeline` reporta la última corrida POR TIPO, así que mezclarla
con otra etapa haría que un éxito ajeno tapara un fallo de Gold.
"""

METODO_BASE = "inegi"
"""De dónde salió la identidad, para `metodo_base`.

No es `radio` ni `h3`: en esta cohorte la identidad **no se resolvió**, la
publicó INEGI en el campo `clee` de los hechos. Poner el nombre de un método
de propagación sería falso justamente en la columna cuyo trabajo es decir de
dónde vino la identidad.
"""

SQL_CVE_GEO = """
        CASE
            WHEN cve_ent IS NULL OR cve_ent = '' THEN NULL
            WHEN cve_mun IS NULL OR cve_mun = '' THEN cve_ent
            WHEN cve_loc IS NULL OR cve_loc = '' THEN cve_ent || cve_mun
            WHEN ageb    IS NULL OR ageb    = '' THEN cve_ent || cve_mun || cve_loc
            ELSE cve_ent || cve_mun || cve_loc || ageb
        END"""
"""Llave geografica jerarquica, cortada en el primer nivel ausente.

El `CASE` no es defensivo: concatenar a ciegas con un nivel nulo en medio
--entidad + municipio + AGEB, sin localidad-- produciria una llave
**inventada**, con el AGEB ocupando la posicion de la localidad. Asi el valor
es siempre un prefijo valido, y su LARGO dice hasta que nivel llega: 2
entidad, 5 municipio, 9 localidad, mas AGEB.

Guardarla concatenada y no en columnas sueltas tiene la misma razon: `cve_mun`
no es unico a nivel nacional sin `cve_ent`, ni `cve_loc` sin los dos. Con los
componentes sueltos, comparar la geografia inicial contra la final podria
emparejar un municipio de un estado con el de otro.
"""

DIAS_POR_ANIO = 365.25
"""Divisor para pasar de dias a anios, con el cuarto de dia de los bisiestos.

365 daria edades infladas ~0.07% por anio; 365.25 es la convencion juliana y
deja el error por debajo de un dia por siglo. Importa porque la edad entra al
modelo de supervivencia como variable continua.
"""

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra, para la diagonal del rectángulo envolvente."""

_SQL_VENTANA_OFICIAL = """
SELECT period
FROM {schema}.establecimientos
GROUP BY period
HAVING count(*) FILTER (WHERE clee IS NOT NULL AND clee <> '')
       >= {minima} * count(*)
ORDER BY period
"""
"""Qué cortes son oficiales, calculado y no supuesto.

Es el criterio de `gold_cohorte_2021.md` §1.1, el mismo 99.9% que ya usaba
`scripts/evaluate_masked_clee.py`. Se calcula al arrancar y queda en
`ventana_cortes` de cada fila: si mañana entra un corte, la N y todas las
tasas cambian, y eso es lo que lo explica.

Deliberadamente **no** usa `clee_resolver._get_periods_with_clee_coverage`,
que clasifica con un `EXISTS`: ahí bastaría UNA fila con `clee` para declarar
oficial a un corte con 10% de cobertura. Gold calcula la suya.
"""


class CohorteOficial(Agregado):
    """El SQL de `gold.negocios_clee_oficial`, para que `troceado` lo corra.

    **No copia `silver.permanencia_<metodo>`**, y no es una optimización
    perdida: esa tabla agrega sobre los 20 cortes y una fila suya mezcla los
    dos regímenes --un negocio visto en 2019 (identidad propagada) y en 2021
    (oficial) comparte `clee_resuelto`. Filtrar por `cortes_directo > 0`
    daría la cohorte correcta con la ventana contaminada. Y los `idx` se
    renumeran dentro de la ventana oficial: heredar los de la secuencia de 20
    rompería `presente_desde_el_inicio` y `duracion_periodos` sin un solo
    error visible (`gold_cohorte_2021.md` §4).
    """

    def __init__(self, schema: str = DEFAULT_SCHEMA):
        """
        Args:
            schema: schema de ORIGEN (los hechos). El destino es `gold`
                siempre: es la capa que lee el API con `api_ro`.
        """
        super().__init__(
            nombre=TABLA,
            schema=SCHEMA,
            tabla=TABLA,
            # Formalmente no hay tabla de identidad: la identidad viene en los
            # hechos. Se nombra `establecimientos` para que `source_path` de
            # `meta.process_tasks` no mienta sobre de donde salio la fila.
            identidad="establecimientos",
            logger=logger,
            job_name=JOB_NAME,
            job_type=JOB_TYPE,
            llave_destino="clee",
            origen_schema=schema,
        )
        self._ventana: list[date] | None = None

    # ----- lo que esta capa define distinto de Silver -----

    def cortes(self, conn: Connection) -> list[date]:
        """Los cortes OFICIALES, que son la ventana de esta cohorte.

        Se memoiza porque es un recorrido de los 82.9M hechos y la máquina la
        pide tres veces en una corrida (la ventana, la huella y el chequeo de
        obsolescencia). Una vez por proceso es suficiente: dentro de una
        corrida la ventana no puede cambiar.

        Args:
            conn: conexión de lectura.

        Returns:
            list[date]: cortes oficiales, del más viejo al más nuevo.
        """
        if self._ventana is None:
            minima = get_settings().GOLD_COBERTURA_CLEE_MINIMA
            # Recorre los 82.9M hechos contando `clee` por corte, asi que
            # tarda y hay que decir que esta pasando: sin esta linea el
            # arranque parece un cuelgue.
            self.logger.info(
                f"   ▶ midiendo la ventana oficial sobre {self.origen_schema}."
                f"establecimientos (cobertura minima de clee: {minima:.1%})..."
            )
            inicio = time.monotonic()
            with latido(conn, "ventana oficial", self.logger):
                self._ventana = list(
                    conn.execute(
                        text(_SQL_VENTANA_OFICIAL.format(schema=self.origen_schema, minima=minima))
                    )
                    .scalars()
                    .all()
                )
            segundos = time.monotonic() - inicio
            if self._ventana:
                self.logger.info(
                    f"   ✔ ventana oficial: {len(self._ventana)} cortes "
                    f"({self._ventana[0]} .. {self._ventana[-1]}) en {segundos:.1f}s"
                )
                self.logger.info("   cortes: " + ", ".join(c.isoformat() for c in self._ventana))
            else:
                self.logger.warning(
                    f"   ⚠️ ningun corte alcanza el {minima:.1%} de cobertura de clee "
                    f"(revisado en {segundos:.1f}s)"
                )
        return self._ventana

    def huella_origen(self, conn: Connection) -> str:
        """La ventana oficial, que es lo que define a esta cohorte.

        No `max(resuelto_en)`: Gold no lee ninguna tabla de identidad. Y una
        firma en vez de una fecha porque el cambio relevante va en los dos
        sentidos --un corte puede ENTRAR a la ventana, y también salir si
        INEGI corrige una entrega-- y un `>` sobre timestamps solo vería uno.
        """
        return "ventana=" + ",".join(c.isoformat() for c in self.cortes(conn))

    def obsoleto(self, conn: Connection, construido_en: datetime) -> bool:
        """Si la tabla vigente se construyó con otra ventana.

        La comparación es contra `ventana_cortes` de la propia tabla, que es
        exactamente para esto: la fila dice con qué cortes nació, así que
        decidir si sigue vigente no depende de ningún registro externo.

        `construido_en` no se usa --y el parámetro se queda porque es el
        contrato de la máquina--: aquí la pregunta no es *cuándo* se
        construyó sino *con qué*. Una tabla construida hace un año con la
        ventana vigente sigue siendo correcta; una de ayer con otra ventana,
        no.
        """
        guardada = conn.execute(
            text(f"SELECT ventana_cortes FROM {self.schema}.{self.tabla} LIMIT 1")
        ).scalar()
        if guardada is None:
            # Tabla vacia: no hay nada obsoleto que rehacer. La maquina ya
            # distingue este caso por el conteo de filas.
            return False
        return list(guardada) != self.cortes(conn)

    def contexto_meta(self, conn: Connection) -> dict:
        """La ventana y las reglas, que es lo que vuelve interpretable un conteo.

        `gold_cohorte_2021.md` §1.1 lo pide explícitamente: la ventana se
        calcula al arrancar y **se registra en el log y en `meta`**. Sin ella
        en `meta`, "¿por qué la corrida de marzo dio 5.1M y la de abril
        5.4M?" solo se contesta abriendo dos logs -- cuando la respuesta es
        que entró un corte.
        """
        ajustes = get_settings()
        ventana = self.cortes(conn)
        return {
            "ventana_cortes": [c.isoformat() for c in ventana],
            "n_cortes_oficiales": len(ventana),
            "cobertura_clee_minima": ajustes.GOLD_COBERTURA_CLEE_MINIMA,
            "metodo_base": METODO_BASE,
            "version_reglas": ajustes.GOLD_VERSION_REGLAS,
            "cierre_meses_ausente": ajustes.CIERRE_MESES_AUSENTE,
        }

    @property
    def pista_vacio(self) -> str:
        """Lo que falta aquí es el ingest, no una resolución."""
        return (
            "Ningun corte alcanza la cobertura de clee exigida: corre el ingest "
            "de DENUE (`pipeline`) o revisa GOLD_COBERTURA_CLEE_MINIMA."
        )

    def validar(self, conn: Connection) -> list[str]:
        """Invariantes de `gold_cohorte_2021.md` §5.1, contra los datos reales.

        Cada uno cubre una forma concreta de equivocarse en silencio, y
        ninguno levantaria un error por su cuenta: una tabla con una identidad
        sintetica, o con una edad negativa, se ve perfectamente bien hasta que
        alguien la cita.

        Uno de los de §5.1 **no se consulta**: "ninguna observacion
        `(id, period)` contribuye a dos filas" esta garantizado por
        construccion, porque `(id, period)` es la PK de
        `silver.establecimientos` --asi que cada observacion tiene UN `clee`--
        y el agregado es un `GROUP BY clee`. Una consulta ahi gastaria un
        recorrido de 51.5M filas para confirmar lo que el esquema impide.

        Args:
            conn: conexion de lectura, con la tabla ya escrita.

        Returns:
            list[str]: una linea por violacion, con el conteo y un ejemplo.
        """
        tabla = f"{self.schema}.{self.tabla}"
        ventana = self.cortes(conn)
        n_cortes = len(ventana)
        primer_corte, ultimo_corte = ventana[0], ventana[-1]

        chequeos: list[tuple[str, str]] = [
            (
                "hay identidades sinteticas NEW_* en una cohorte que solo admite CLEE de INEGI",
                # STARTS_WITH y no LIKE: el `_` de `NEW_` es un comodin en LIKE.
                f"SELECT count(*), min(clee) FROM {tabla} WHERE STARTS_WITH(clee, 'NEW_')",
            ),
            (
                f"hay negocios con mas cortes observados que los {n_cortes} de la ventana",
                f"SELECT count(*), min(clee) FROM {tabla} WHERE cortes_observados > {n_cortes}",
            ),
            (
                "hay negocios cuya ventana_cortes no es la de esta corrida",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE ventana_cortes <> {self._ventana_literal()}",
            ),
            (
                "hay periodos fuera de la ventana oficial",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE primer_periodo < DATE '{primer_corte.isoformat()}' "
                f"   OR ultimo_periodo > DATE '{ultimo_corte.isoformat()}'",
            ),
            (
                "hay `periodos` cuyo largo no coincide con `cortes_observados`",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE cardinality(periodos) <> cortes_observados",
            ),
            # Los dos que siguen son del proposito de la tabla: si la edad o el
            # veredicto estan mal, lo que se muestrea y se verifica con agentes
            # no significa nada.
            (
                "hay edades NEGATIVAS, o sea fecha de alta posterior al ultimo corte",
                f"SELECT count(*), min(clee) FROM {tabla} WHERE edad_anios < 0",
            ),
            (
                "`cerrado` no coincide con el umbral de meses de la corrida",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE cerrado <> (meses_sin_aparecer >= "
                f"{get_settings().CIERRE_MESES_AUSENTE})",
            ),
            (
                "hay meses de ausencia negativos, o sea un ultimo_periodo posterior a la ventana",
                f"SELECT count(*), min(clee) FROM {tabla} WHERE meses_sin_aparecer < 0",
            ),
            (
                "hay negocios presentes en el ultimo corte marcados como cerrados",
                f"SELECT count(*), min(clee) FROM {tabla} WHERE cortes_tras_ultimo = 0 AND cerrado",
            ),
            (
                "hay clusters sospechosos con nombre, que seria una afirmacion falsa",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE cluster_sospechoso AND nombre IS NOT NULL",
            ),
            (
                "hay nombres sin decir quien los eligio",
                f"SELECT count(*), min(clee) FROM {tabla} "
                f"WHERE nombre IS NOT NULL AND nombre_origen IS NULL",
            ),
        ]

        violaciones = []
        for descripcion, sql in chequeos:
            cuantos, ejemplo = conn.execute(text(sql)).one()
            if cuantos:
                violaciones.append(f"{descripcion}: {cuantos:,} filas (p. ej. {ejemplo})")
        return violaciones

    # ----- las tres piezas que la maquina pide -----

    def sql_crear_obs(self) -> str:
        """DDL de la tabla de trabajo: los hechos de los cortes oficiales.

        Lleva los catálogos ya resueltos a texto y las coordenadas ya
        enriquecidas, para que la fase de agregados no vuelva a unir nada.

        `UNLOGGED` y no `TEMP` porque el troceo commitea cada corte y una
        temporal muere en el primer `COMMIT`. Vive en `gold` porque es suya y
        es transitoria; la máquina la borra al terminar.

        **No lleva columna `origen`**, al contrario de la de permanencia: en
        esta cohorte toda identidad es directa, así que la columna sería una
        constante en 51.5M filas.
        """
        return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {self.obs_schema}.{self.obs} (
    clee             text     NOT NULL,
    bloque           smallint NOT NULL,
    idx              integer  NOT NULL,
    period           date     NOT NULL,
    id               text     NOT NULL,
    nom_estab        text,
    raz_social       text,
    codigo_act       text,
    cve_ent          text,
    cve_mun          text,
    cve_loc          text,
    ageb             text,
    cod_postal       text,
    tipo_asent       text,
    latitud          double precision,
    longitud         double precision,
    h3_cell_9        bigint,
    fecha_alta_date  date,
    telefono         text,
    correoelec       text,
    www              text,
    nom_cencom       text,
    per_ocu          text,
    categoria_tamano text,
    empleados_min    integer,
    tipounieco       text,
    direccion        text
)
"""

    def sql_llenar_obs(self, corte: date, idx: int, bloques: int) -> str:
        """Carga las observaciones OFICIALES de un corte.

        Dos filtros, y el segundo es de la cohorte y no una optimización: una
        fila sin `clee` dentro de un corte oficial (ese ≤0.1% que el umbral
        tolera) **no tiene identidad observada**, así que no entra. El negocio
        sí entra si aparece con `clee` en otro corte oficial
        (`gold_cohorte_2021.md` §2).

        El `idx` es la posición dentro de la VENTANA OFICIAL, no en la
        secuencia de 20 cortes. Es la línea que hace que `idx_primer = 0`
        signifique "estaba en el primer corte con CLEE".

        Args:
            corte: el corte a cargar.
            idx: su posición dentro de la ventana oficial.
            bloques: cuántos bloques repartir.

        Returns:
            str: `INSERT ... SELECT` de un corte.
        """
        schema = self.origen_schema
        literal = f"DATE '{corte.isoformat()}'"
        return f"""
INSERT INTO {self.obs_schema}.{self.obs} (
    clee, bloque, idx, period, id,
    nom_estab, raz_social, codigo_act,
    cve_ent, cve_mun, cve_loc, ageb, cod_postal, tipo_asent,
    latitud, longitud, h3_cell_9,
    fecha_alta_date, telefono, correoelec, www, nom_cencom,
    per_ocu, categoria_tamano, empleados_min, tipounieco,
    direccion
)
SELECT
    e.clee,
    {expr_bloque("e.clee", bloques)},
    {idx},
    e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cve_loc, e.ageb, e.cod_postal, ta.tipo_asent,
    e.latitud, e.longitud, e.h3_cell_9,
    e.fecha_alta_date, e.telefono, e.correoelec, e.www, e.nom_cencom,
    ep.per_ocu, ep.categoria, ep.empleados_min,
    tu.tipounieco,
    -- El domicilio como UNA cadena: arrays paralelos por componente
    -- perderian que numero va con que calle.
    NULLIF(CONCAT_WS(' | ',
        NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
        NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')
    ), '') AS direccion
FROM {schema}.establecimientos e
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE e.period = {literal}
  AND e.clee IS NOT NULL AND e.clee <> ''
"""

    def pasos_bloque(self, bloque: int, ultimo_idx: int) -> list[Paso]:
        """Los pasos que escriben un bloque de negocios y su evidencia.

        Cinco, y el orden no es negociable en un punto: `nombres` tiene que
        estar antes de `canonico`, porque la moda sale de esos conteos. Los
        cinco van en la misma transacción del bloque.

        Args:
            bloque: qué bloque procesar.
            ultimo_idx: posición del último corte oficial, para la censura.

        Returns:
            list[Paso]: pasos del bloque, en orden.
        """
        n_cortes = ultimo_idx + 1
        ventana_sql = self._ventana_literal()
        ajustes = get_settings()
        meses = ajustes.CIERRE_MESES_AUSENTE

        return [
            self._paso_ventana(bloque),
            self._paso_primera(bloque),
            self._paso_ultima(bloque),
            self._paso_reocupado(meses),
            self._paso_nombres(bloque),
            self._paso_canonico(),
            self._paso_insert(ultimo_idx, n_cortes, ventana_sql, ajustes),
        ]

    def _sql_meses_sin_aparecer(self, ultimo_observado: str) -> str:
        """Meses entre el ultimo corte en que aparecio y el ultimo de la ventana.

        Es una diferencia de CALENDARIO y no de dias entre 30: los cortes caen
        el dia 1, asi que la resta de anios y meses es exacta, mientras que
        dividir dias daria 5.9 o 6.1 segun el mes y volveria ruidoso el eje
        sobre el que se aplica la regla de cierre.

        Args:
            ultimo_observado: expresion SQL de tipo `date` con el ultimo corte
                en que el negocio aparecio.

        Returns:
            str: expresion entera; 0 si aparecio en el corte final.
        """
        ultimo_corte = self._ventana[-1].isoformat()
        return (
            f"((EXTRACT(YEAR FROM DATE '{ultimo_corte}') - EXTRACT(YEAR FROM {ultimo_observado})) * 12"
            f" + (EXTRACT(MONTH FROM DATE '{ultimo_corte}')"
            f" - EXTRACT(MONTH FROM {ultimo_observado})))::int"
        )

    def _ventana_literal(self) -> str:
        """La ventana como literal SQL, para grabarla en cada fila.

        Sale del cache que llenó `cortes()`: cuando la máquina llega a los
        bloques ya la calculó, así que aquí no hace falta una conexión.
        """
        if not self._ventana:
            raise RuntimeError("la ventana oficial no se ha calculado; falta llamar a cortes()")
        fechas = ", ".join(f"DATE '{c.isoformat()}'" for c in self._ventana)
        return f"ARRAY[{fechas}]::date[]"

    def _paso_ventana(self, bloque: int) -> Paso:
        """Un solo `GROUP BY` con todo lo que es agregado sobre la vida.

        Solo lo que la tabla publica o consume. Los conteos que quedaron sin
        destino --`n_direcciones`, el rectángulo envolvente, la dispersión--
        salieron de aquí: ya están en `silver.permanencia_<metodo>`, que es
        justo la redundancia que este rediseño elimina.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "ventana",
            "colapsando a una fila por negocio",
            f"""
CREATE TEMP TABLE ventana ON COMMIT DROP AS
SELECT
    clee,
    MIN(period) AS primer_periodo,
    MAX(period) AS ultimo_periodo,
    MIN(idx) AS idx_primer,
    MAX(idx) AS idx_ultimo,
    -- Meses de ausencia, no cortes: los intervalos de la ventana son 6, 6,
    -- 6, 12, 6, 6, 6, 12 meses, asi que un mismo `cortes_tras_ultimo` puede
    -- significar el doble de evidencia. Es el ingrediente de la regla de
    -- cierre, y se calcula aqui una vez para que el veredicto y la busqueda
    -- del local reocupado lean el MISMO numero.
    {self._sql_meses_sin_aparecer("MAX(period)")} AS meses_sin_aparecer,
    COUNT(DISTINCT idx) AS cortes_observados,
    -- Una fecha de alta POSTERIOR al corte en que aparece la fila es
    -- imposible por definicion del campo (el alta precede a la aparicion en
    -- el directorio), asi que es un dato roto y no cuenta: un MIN que lo
    -- dejara pasar daria edades negativas, el invariante que lo vigila.
    MIN(fecha_alta_date) FILTER (WHERE fecha_alta_date <= period) AS fecha_alta_date,
    -- Los cortes EXACTOS en que aparecio: es lo que se publica en `periodos`,
    -- y de ahi el analisis deriva cualquier patron de presencia sin volver a
    -- los 51.5M hechos.
    ARRAY_AGG(DISTINCT period ORDER BY period) AS periodos,
    -- Cuantos valores distintos tuvo. Los valores en si salen de
    -- silver.establecimientos filtrando por clee: aqui solo hace falta el
    -- conteo, para detectar el cluster sobre-fusionado.
    COUNT(DISTINCT nom_estab) FILTER (
        WHERE nom_estab IS NOT NULL AND nom_estab <> '') AS n_nombres,
    COUNT(DISTINCT codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS n_actividades,
    COUNT(DISTINCT per_ocu) FILTER (
        WHERE per_ocu IS NOT NULL AND per_ocu <> '') AS n_estratos,
    -- Ancestro comun del SCIAN, por los extremos: el prefijo comun de un
    -- conjunto ordenado de cadenas es el prefijo comun del minimo y el
    -- maximo, asi que dos escalares bastan y no hay que materializar el
    -- conjunto. La decision de que nivel usar esta en el INSERT.
    MIN(codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS act_min,
    MAX(codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS act_max,
    -- NO hay banderas `tiene_*`. Con los valores concretos materializados
    -- --empleados y tamano inicial y final, las dos posiciones, la llave
    -- geografica-- un NULL dice lo mismo que decian las quince columnas, y
    -- la cobertura por campo ya esta en silver.permanencia_<metodo>.
    COUNT(DISTINCT (latitud, longitud)) FILTER (
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL) AS n_posiciones
FROM {obs}
WHERE bloque = {bloque}
GROUP BY clee
""",
        )

    def _paso_primera(self, bloque: int) -> Paso:
        """El extremo inicial del recorrido, para el desplazamiento y el delta."""
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "primera",
            "tomando el estado con que empezo cada negocio",
            f"""
-- Solo lo que hace falta para las medidas de trayectoria. Ninguno de estos
-- valores se guarda: se consumen aqui y lo que persiste es el escalar.
CREATE TEMP TABLE primera ON COMMIT DROP AS
SELECT DISTINCT ON (clee)
       clee, latitud, longitud, empleados_min, categoria_tamano,
       {SQL_CVE_GEO} AS cve_geo
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee, period ASC, id
""",
        )

    def _paso_ultima(self, bloque: int) -> Paso:
        """El extremo final del recorrido, solo para los escalares derivados.

        Ya no es un "corte de referencia": desde que la geografia vive en
        variantes, de la ultima observacion no se PUBLICA nada. Lo que se
        consume aqui son dos cosas que solo tienen sentido contra el primer
        corte --la posicion, para el desplazamiento neto, y la cota de
        empleados, para el delta-- y lo que persiste son esos dos escalares.

        El desempate por `id` mantiene el resultado determinista cuando un
        negocio tiene dos filas en el mismo corte.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "ultima",
            "tomando el estado con que termino cada negocio",
            f"""
CREATE TEMP TABLE ultima ON COMMIT DROP AS
SELECT DISTINCT ON (clee)
       clee, latitud, longitud, empleados_min, categoria_tamano, h3_cell_9,
       {SQL_CVE_GEO} AS cve_geo
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee, period DESC, id
""",
        )

    def _paso_reocupado(self, meses: int) -> Paso:
        """Si OTRO negocio aparecio en la misma celda H3 despues del ultimo.

        Es evidencia **corroborante** del cierre, no prueba: en una celda res
        9 (~174 m) urbana caben decenas de negocios, asi que otro ocupante no
        significa que este haya cerrado. Pero es justo lo que un verificador
        humano buscaria, y sirve para estratificar la muestra: donde esto es
        True, la hipotesis de cierre es mas creible.

        **Solo se evalua para los que la regla declara cerrados** (los que
        llevan `meses` o mas sin aparecer). Para los activos no significa nada
        --el vecino existe y los dos siguen ahi-- y evaluarlo costaria una
        busqueda por cada uno de los 9.58M.

        El `EXISTS` es un semi-join: se detiene en la primera coincidencia en
        vez de contar todos los vecinos, que en una celda densa serian
        cientos. Y entra por `ix_estab_h3_9` con la poda de particiones que da
        el `period >`.

        **OJO AL COSTO:** esto agrega una busqueda por negocio cerrado del
        bloque contra `silver.establecimientos`. Es el unico paso que sale de
        la tabla de trabajo, asi que es el candidato a sorprender: si el
        bloque 1 tarda mucho mas que antes, este paso es el sospechoso y se
        quita en una linea.
        """
        return Paso(
            "reocupado",
            "buscando si otro negocio ocupo el local",
            f"""
CREATE TEMP TABLE reocupado ON COMMIT DROP AS
SELECT u.clee
FROM ultima u
JOIN ventana v USING (clee)
WHERE u.h3_cell_9 IS NOT NULL
  AND v.meses_sin_aparecer >= {meses}
  AND EXISTS (
      SELECT 1 FROM {self.origen_schema}.establecimientos e
      WHERE e.h3_cell_9 = u.h3_cell_9
        AND e.period > v.ultimo_periodo
        AND e.clee IS NOT NULL AND e.clee <> '' AND e.clee <> u.clee
  )
""",
        )

    def _paso_nombres(self, bloque: int) -> Paso:
        """Cuantos cortes uso cada nombre, para poder elegir la moda.

        Es una temporal y ya: **no se persiste nada**. El diseno anterior
        escribia esto en una tabla `..._variantes` de ~100M filas, y era
        redundante -- los valores distintos de cualquier campo salen de
        `silver.establecimientos` filtrando por `clee`, que los tiene mejor
        tipados. Aqui solo hacen falta los conteos, y solo del nombre.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "nombres",
            "contando en cuantos cortes se uso cada nombre",
            f"""
CREATE TEMP TABLE nombres ON COMMIT DROP AS
SELECT clee, nom_estab,
       COUNT(DISTINCT idx) AS n_cortes,
       MAX(idx) AS ultimo_idx
FROM {obs}
WHERE bloque = {bloque} AND nom_estab IS NOT NULL AND nom_estab <> ''
GROUP BY clee, nom_estab
""",
        )

    def _paso_canonico(self) -> Paso:
        """El nombre unificado, en UNA pasada: la moda sobre los cortes.

        Es un `DISTINCT ON` y no una subconsulta lateral por fila, y la
        diferencia es de complejidad, no de estilo. Un `LEFT JOIN LATERAL`
        con `LIMIT 1` fuerza un *nested loop*: una busqueda por cada
        identidad del bloque, y cada una recorriendo la temporal completa
        porque no tiene indice. Medido el 2026-11-09: ~300k identidades x
        ~1M filas dejaron el `INSERT` del bloque corriendo **21 minutos sin
        terminar**, o sea ~11 horas proyectadas.

        **Es el baseline, no la respuesta final.** El objetivo es que un LLM
        unifique el nombre a partir de todas las variantes; hasta que esa
        etapa exista, la moda es lo que se publica y `nombre_origen` dice que
        fue ella. Cuando el LLM corra, sobrescribe la columna y cambia ese
        campo a `llm`, sin migracion.

        El desempate final por `nom_estab` no es decorativo: sin el, dos
        nombres con el mismo conteo y el mismo `ultimo_idx` darian un
        resultado dependiente del plan, y la tabla dejaria de ser
        reproducible.
        """
        return Paso(
            "canonico",
            "eligiendo el nombre de cada negocio (moda)",
            """
CREATE TEMP TABLE canonico ON COMMIT DROP AS
SELECT DISTINCT ON (clee) clee, nom_estab AS nombre
FROM nombres
ORDER BY clee, n_cortes DESC, ultimo_idx DESC, nom_estab
""",
        )

    def _paso_insert(
        self, ultimo_idx: int, n_cortes: int, ventana_sql: str, ajustes: Settings
    ) -> Paso:
        """La fila del negocio: ventana, edad, veredicto y los dos extremos."""
        version = ajustes.GOLD_VERSION_REGLAS
        meses = ajustes.CIERRE_MESES_AUSENTE

        # Distancia entre los dos extremos REALES del recorrido. Respeta el
        # orden temporal, asi que contesta "termino en otro lado?" y no
        # "cuanto deambulo?" -- que es lo que mediria el rectangulo.
        distancia = f"""CASE WHEN p.latitud IS NULL OR u.latitud IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(u.latitud - p.latitud) / 2), 2)
            + COS(RADIANS(p.latitud)) * COS(RADIANS(u.latitud))
            * POWER(SIN(RADIANS(u.longitud - p.longitud) / 2), 2)
        ))
    END"""

        # Mudanza a nivel MUNICIPIO, no de coordenada: el ruido de GPS mueve
        # el punto sin cambiar el municipio. Y solo se afirma cuando las dos
        # llaves se conocen -- con una nula, `IS DISTINCT FROM` diria que se
        # mudo cuando lo que pasa es que falta el dato.
        se_mudo = """(p.cve_geo IS NOT NULL AND u.cve_geo IS NOT NULL
         AND LEFT(p.cve_geo, 5) <> LEFT(u.cve_geo, 5))"""

        return Paso(
            TABLA,
            f"escribiendo el bloque en {self.schema}.{TABLA}",
            f"""
INSERT INTO {self.schema}.{TABLA} (
    clee, nombre, nombre_origen, n_nombres,
    periodos, primer_periodo, ultimo_periodo, cortes_observados,
    huecos, cortes_tras_ultimo,
    fecha_alta, edad_anios, dias_alta_a_primera,
    meses_sin_aparecer, local_reocupado, cerrado,
    empleados_inicial, empleados_final,
    tamano_inicial, tamano_final, cambio_tamano,
    cve_geo_inicial, cve_geo_final,
    lat_inicial, lon_inicial, lat_final, lon_final, h3_cell_9_final,
    se_mudo, distancia_mudanza_m, n_posiciones,
    codigo_act, cambio_actividad,
    cluster_sospechoso,
    ventana_cortes, metodo_base, version_reglas
)
SELECT
    v.clee,
    -- Un cluster con mas nombres que cortes agrupo mas de un establecimiento:
    -- ahi cualquier nombre unificado seria una afirmacion falsa, asi que se
    -- queda sin nombre y las variantes siguen en silver para mirarlas.
    CASE WHEN v.n_nombres > {n_cortes} THEN NULL ELSE nom.nombre END AS nombre,
    CASE WHEN v.n_nombres > {n_cortes} OR nom.nombre IS NULL THEN NULL
         ELSE '{ORIGEN_NOMBRE_MODA}' END AS nombre_origen,
    v.n_nombres,
    v.periodos,
    v.primer_periodo, v.ultimo_periodo, v.cortes_observados,
    -- Cortes DENTRO de su ventana en que no aparecio: desaparecio y volvio.
    (v.idx_ultimo - v.idx_primer + 1) - v.cortes_observados AS huecos,
    -- Cortes publicados DESPUES del ultimo en que aparecio.
    {ultimo_idx} - v.idx_ultimo AS cortes_tras_ultimo,
    v.fecha_alta_date,
    -- EDAD: de la fecha de alta al ULTIMO CORTE EN QUE APARECIO, no a hoy.
    -- Con "hoy" la tabla dejaria de ser reproducible: la misma fila daria
    -- una edad distinta cada vez que se consultara. Para un negocio activo
    -- esto es una edad censurada por la derecha, que es justo lo que el
    -- modelo de supervivencia consume.
    CASE WHEN v.fecha_alta_date IS NULL THEN NULL
         ELSE ROUND(
            ((v.ultimo_periodo - v.fecha_alta_date) / {DIAS_POR_ANIO})::numeric, 2)
    END AS edad_anios,
    CASE WHEN v.fecha_alta_date IS NULL THEN NULL
         ELSE (v.primer_periodo - v.fecha_alta_date) END AS dias_alta_a_primera,
    -- VEREDICTO. Es una HIPOTESIS: el DENUE no reporta cierres, solo deja de
    -- listar. La regla es EN MESES, no en cortes (ver `ventana`); el umbral
    -- queda en `version_reglas` y los ingredientes (`meses_sin_aparecer`,
    -- `huecos`) se conservan para recalcularla sin reconstruir la tabla.
    v.meses_sin_aparecer,
    -- NULL para los que no se declaran cerrados: ahi la pregunta no
    -- significa nada, el vecino existe y los dos siguen abiertos.
    CASE WHEN v.meses_sin_aparecer >= {meses}
         THEN (ro.clee IS NOT NULL) ELSE NULL END AS local_reocupado,
    v.meses_sin_aparecer >= {meses} AS cerrado,
    -- LOS DOS EXTREMOS de la trayectoria. `permanencia` los consume para
    -- calcular su delta y los tira; aqui se quedan, que es la mitad de la
    -- razon de ser de esta tabla.
    p.empleados_min AS empleados_inicial,
    u.empleados_min AS empleados_final,
    p.categoria_tamano AS tamano_inicial,
    u.categoria_tamano AS tamano_final,
    v.n_estratos > 1 AS cambio_tamano,
    p.cve_geo AS cve_geo_inicial,
    u.cve_geo AS cve_geo_final,
    p.latitud AS lat_inicial, p.longitud AS lon_inicial,
    u.latitud AS lat_final,   u.longitud AS lon_final,
    u.h3_cell_9 AS h3_cell_9_final,
    {se_mudo} AS se_mudo,
    {distancia} AS distancia_mudanza_m,
    COALESCE(v.n_posiciones, 0) AS n_posiciones,
    -- SCIAN canonico: el ANCESTRO COMUN mas especifico por prefijo. La
    -- jerarquia ES el codigo (sector=2, subsector=3, rama=4), asi que no
    -- hace falta unir el catalogo. Si la discrepancia cruza rama queda NULL:
    -- es una decision de juicio, y un valor inventado seria peor.
    CASE
        WHEN v.act_min IS NULL THEN NULL
        WHEN v.act_min = v.act_max THEN v.act_min
        WHEN LEFT(v.act_min, 5) = LEFT(v.act_max, 5) THEN LEFT(v.act_min, 5)
        WHEN LEFT(v.act_min, 4) = LEFT(v.act_max, 4) THEN LEFT(v.act_min, 4)
        ELSE NULL
    END AS codigo_act,
    v.n_actividades > 1 AS cambio_actividad,
    v.n_nombres > {n_cortes} AS cluster_sospechoso,
    {ventana_sql}, '{METODO_BASE}', '{version}'
FROM ventana v
JOIN primera p USING (clee)
JOIN ultima u USING (clee)
LEFT JOIN canonico nom USING (clee)
LEFT JOIN reocupado ro USING (clee)
""",
        )


def construir_cohorte_oficial(
    engine: Engine,
    tracker: MetadataTracker,
    schema: str = DEFAULT_SCHEMA,
    force: bool = False,
) -> int:
    """Construye la cohorte oficial de Gold y la registra en `meta`.

    Punto de entrada del CLI. Qué decide la máquina --reanudar, saltarse el
    trabajo hecho, o reconstruir porque la ventana cambió-- está en
    `troceado.construir`.

    Lo que descarta al reconstruir es solo lo suyo: su tabla de trabajo y las
    dos tablas de `gold`. Los hechos de Silver, las `clee_<metodo>`, las
    `permanencia_<metodo>` y `meta` quedan intactos.

    Args:
        engine: engine con permisos de escritura; la máquina controla sus
            transacciones, una por vuelta.
        tracker: registro de `meta` donde queda la corrida.
        schema: schema de ORIGEN (los hechos); el destino es `gold`.
        force: si True, descarta lo que haya y reconstruye entero.

    Returns:
        int: negocios en la cohorte al terminar.
    """
    return construir(engine, CohorteOficial(schema), tracker, force)
