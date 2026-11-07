"""Construye `silver.permanencia_<metodo>` desde los hechos y la identidad.

Colapsa las 82.9M observaciones de `silver.establecimientos` a una fila por
establecimiento resuelto: su ventana de permanencia en el directorio, los
ingredientes de censura y un resumen de cuánto varió entre cortes.

Existe como tabla materializada y no como vista porque el `GROUP BY` sobre
82.9M filas unidas a la tabla de identidades cuesta minutos, y el modelo de
supervivencia la lee decenas de veces --cada partición de la validación
cruzada, cada iteración de variables, el cálculo de SHAP.

**Guarda cuántas variantes hubo, no cuáles.** Los nombres, domicilios y
teléfonos que tuvo un establecimiento ya están en los hechos, alcanzables
por la tabla de identidad; materializarlos como arrays duplicaría ~3-4 GB
para ahorrar un join que es barato justo en el caso en que se necesita --un
establecimiento, o una muestra de ellos. Lo que sí se materializa son los
escalares (`n_nombres`, `cambio_municipio`, ...), porque esos responden las
preguntas agregadas sobre los 10M de un golpe y son lo que el modelo puede
consumir. Los valores se recuperan uniendo `clee_resuelto` con la tabla de
identidad y de ahí a los hechos; `AGENTS.md` §N3 trae la consulta.

**Tampoco guarda el veredicto de cierre**, que se deriva de
`cortes_tras_ultimo` con un `CASE` en la consulta que lo necesite. El
protocolo llama preliminar a la regla, así que el umbral vive donde se
pregunta y no en el esquema: cambiarlo no debe costar 10M de filas ni un
`CREATE OR REPLACE`.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.shared.config import get_settings
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.partitions import existing_partitions
from src.shared.enums import OrigenClee
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_permanencia.log"))

METODOS: dict[str, str] = {
    "radio": "clee_radio",
    "h3": "clee_h3",
}
"""Método -> tabla de identidad y, por convención, tabla destino.

El destino es `permanencia_<metodo>`. Agregar un método es una entrada aquí
más su tabla, sin tocar el SQL.
"""

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra, para la diagonal del rectángulo envolvente."""

SET_WORK_MEM = "SET LOCAL work_mem = '4GB'"
"""Memoria de trabajo para esta consulta y solo para ella.

El agregado produce ~10M grupos, cada uno con una decena de `COUNT(DISTINCT
...)` que mantienen su propio conjunto mientras dura el grupo. Con el
`work_mem` global (256 MB, modesto a propósito porque es POR NODO y POR
CONEXIÓN) el hash aggregate se derrama a disco y tarda mucho más. Subirlo
por sesión en las cargas masivas es el patrón que documenta `.env.example`.
"""


class _Paso(NamedTuple):
    """Un paso del llenado, con qué decir de él mientras corre.

    La descripción no es adorno: es lo que el log muestra durante las horas
    que el paso tarda, y lo último que queda escrito si el proceso muere.

    Attributes:
        nombre: tabla temporal que produce (o la tabla destino, en el
            último), para identificarlo en el log y en `pg_stat_activity`.
        descripcion: qué está haciendo, en una línea.
        sql: la sentencia; `CREATE TEMP TABLE ... AS` salvo el `INSERT`.
    """

    nombre: str
    descripcion: str
    sql: str


_SQL_LATIDO = """
SELECT a.state,
       a.wait_event_type,
       a.wait_event,
       EXTRACT(EPOCH FROM (now() - a.query_start)) AS segundos,
       d.temp_bytes
FROM pg_stat_activity a
JOIN pg_stat_database d ON d.datname = a.datname
WHERE a.pid = :pid
"""
"""Qué está haciendo el backend que ejecuta el `INSERT`.

`wait_event` es el dato útil: dice si el servidor está leyendo los hechos
(`DataFileRead`), derramando el agregado a disco (`BufFileWrite`) o
esperando un lock. `temp_bytes` es de la base entera y no de la consulta
--PostgreSQL no lo expone por sentencia-- así que sirve como proxy del
derrame mientras nada más escriba temporales.

**No se puede reportar "filas escritas hasta ahora":** el `INSERT` vive en
una transacción sin commitear, así que otra conexión ve la tabla destino
vacía hasta el final. De ahí que el progreso se mida por fase y no por
avance.
"""


def tabla_destino(metodo: str) -> str:
    """Nombre de la tabla de permanencia de un método, sin calificar."""
    return f"permanencia_{metodo}"


def _vigilar(pid: int, destino: str, intervalo: int, detener: threading.Event) -> None:
    """Emite un latido cada `intervalo` segundos hasta que se le pida parar.

    Corre en su propio hilo y con su propia conexión: la del `INSERT` está
    bloqueada mientras el servidor trabaja, así que preguntarle a ella es
    imposible por construcción.

    Un fallo del monitoreo no debe tumbar la carga: si la consulta de
    diagnóstico truena, lo registra y sigue intentando en el siguiente
    ciclo.

    Args:
        pid: backend de PostgreSQL que ejecuta el `INSERT`.
        destino: tabla que se está llenando, para el mensaje.
        intervalo: segundos entre latidos.
        detener: se activa cuando el `INSERT` terminó.
    """
    from src.shared.db.session import get_engine

    engine = get_engine()
    inicio = time.monotonic()
    temp_inicial: int | None = None

    while not detener.wait(intervalo):
        try:
            with engine.connect() as vigia:
                fila = vigia.execute(text(_SQL_LATIDO), {"pid": pid}).mappings().first()
        except Exception as e:  # noqa: BLE001  (el monitoreo no tumba la carga)
            logger.warning(f"   latido no disponible: {e}")
            continue

        transcurrido = time.monotonic() - inicio
        if fila is None:
            # El backend ya no existe: o termino (y `detener` esta por
            # activarse) o lo mataron por fuera. Lo segundo importa saberlo.
            logger.warning(
                f"   ⏳ {destino}: el backend {pid} ya no aparece en "
                f"pg_stat_activity tras {transcurrido / 60:.1f} min"
            )
            continue

        if temp_inicial is None:
            temp_inicial = fila["temp_bytes"] or 0
        derramado = (fila["temp_bytes"] or 0) - temp_inicial
        espera = (
            f"{fila['wait_event_type']}:{fila['wait_event']}"
            if fila["wait_event_type"]
            else "ejecutando (sin espera)"
        )
        logger.info(
            f"   ⏳ {destino}: {transcurrido / 60:.1f} min | estado={fila['state']} "
            f"| {espera} | temp +{derramado / 1024**3:.1f} GiB"
        )


@contextmanager
def _latido(conn: Connection, destino: str) -> Generator[None]:
    """Reporta avance mientras el bloque que envuelve ejecuta el `INSERT`.

    Existe por la misma razón que el latido de `shared/db/copy.py`: una
    escritura masiva que no emite nada es indistinguible de un cuelgue
    desde afuera, y la única forma de saberlo era abrir `psql` y mirar
    `pg_stat_activity`. Aquí el costo es mayor que allá --el `INSERT` es UNA
    sentencia de horas, no un bucle de bloques-- así que el latido no puede
    colgarse del flujo: lo emite un hilo aparte.

    El hilo es `daemon`, así que no puede dejar el proceso colgado si algo
    sale mal, y el intervalo sale de `PERMANENCIA_LATIDO_SEGUNDOS`; en 0 ni
    se arranca.

    Args:
        conn: la conexión que va a ejecutar el `INSERT`; de ella se toma el
            backend pid que el hilo vigila.
        destino: tabla que se está llenando, para el mensaje.

    Yields:
        None: el bloque corre con el latido activo.
    """
    intervalo = get_settings().PERMANENCIA_LATIDO_SEGUNDOS
    if intervalo <= 0:
        yield
        return

    pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    detener = threading.Event()
    hilo = threading.Thread(
        target=_vigilar,
        args=(pid, destino, intervalo, detener),
        daemon=True,
        name=f"latido-{destino}",
    )
    hilo.start()
    try:
        yield
    finally:
        detener.set()
        hilo.join(timeout=5)


def _cortes_ordenados(conn: Connection, schema: str = DEFAULT_SCHEMA) -> list[date]:
    """Los cortes presentes en Silver, del más antiguo al más nuevo.

    Salen del catálogo de PostgreSQL --cada corte ES una partición de
    `establecimientos` y su nombre lleva el `YYYY_MM`-- y no de un
    `SELECT DISTINCT period`, que recorrería 82.9M filas para enterarse de
    veinte fechas.

    Args:
        conn: conexión de lectura.
        schema: schema de la tabla de hechos.

    Returns:
        list[date]: cortes ordenados.
    """
    from src.shared.db.partitions import PARTITIONED_TABLE

    prefijo = len(f"{PARTITIONED_TABLE}_")
    cortes = []
    for nombre in existing_partitions(conn, schema=schema):
        ano, mes = nombre[prefijo:].split("_")
        cortes.append(date(int(ano), int(mes), 1))
    return sorted(cortes)


def tabla_obs(metodo: str) -> str:
    """Nombre de la tabla de trabajo de un método, sin calificar."""
    return f"permanencia_obs_{metodo}"


def _expr_bloque(columna: str, bloques: int) -> str:
    """Expresión SQL que asigna una identidad a un bloque.

    Es el primer byte del `md5` de la identidad, módulo el número de
    bloques. Dos propiedades la hacen la llave correcta para trocear:

    - **Es función de la identidad**, así que todas las observaciones de un
      establecimiento caen en el mismo bloque y ningún agregado se parte.
      Trocear por año o por `cve_ent` no cumple esto: la identidad cruza los
      cortes, y los negocios se mudan de municipio --que es justo una de las
      señales que la tabla mide.
    - **Es estable entre versiones de PostgreSQL.** `hashtext()` sería más
      directo pero es una función interna sin garantía de estabilidad; si
      cambiara en un upgrade mayor, una reanudación reasignaría los bloques
      y recalcularía trabajo ya hecho. `md5`, `decode` y `get_byte` están
      documentadas y son inmutables.

    Con `bloques` divisor de 256 el reparto es exactamente uniforme; con
    cualquier otro valor queda un sesgo de a lo más un bloque.

    Args:
        columna: expresión que da la identidad (`clee_resuelto` o `i.clee_resuelto`).
        bloques: cuántos bloques repartir.

    Returns:
        str: expresión entera en `[0, bloques)`.
    """
    return f"(get_byte(decode(md5({columna}), 'hex'), 0) % {bloques})"


def _sql_crear_obs(metodo: str, schema: str = DEFAULT_SCHEMA) -> str:
    """DDL de la tabla de trabajo: los hechos unidos a la identidad.

    Es `UNLOGGED` y no `TEMP` por una razón concreta: una temporal muere en
    el primer `COMMIT`, y el troceo necesita commitear cada corte para que
    un fallo no se lleve lo ya hecho. `UNLOGGED` da lo que importaba de la
    temporal --no escribe WAL, así que cuesta casi lo mismo-- sin esa
    restricción. El precio es que hay que borrarla a mano al terminar; lo
    hace `construir_permanencia`.

    Vive en `silver` con nombre explícito, como las tablas de staging del
    ingest: es transitoria, no parte del esquema.

    Args:
        metodo: clave de `METODOS`.
        schema: schema donde crearla.

    Returns:
        str: `CREATE UNLOGGED TABLE` completo.
    """
    return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {schema}.{tabla_obs(metodo)} (
    clee_resuelto   text    NOT NULL,
    bloque          smallint NOT NULL,
    idx             integer NOT NULL,
    period          date    NOT NULL,
    id              text    NOT NULL,
    nom_estab       text,
    raz_social      text,
    codigo_act      text,
    cve_ent         text,
    cve_mun         text,
    cod_postal      text,
    latitud         double precision,
    longitud        double precision,
    h3_cell_9       bigint,
    fecha_alta_date date,
    telefono        text,
    correoelec      text,
    www             text,
    tipo_asent      text,
    cve_loc         text,
    ageb            text,
    h3_cell_5       bigint,
    nom_cencom      text,
    origen          text,
    per_ocu         text,
    categoria_tamano text,
    empleados_min   integer,
    empleados_max   integer,
    tipounieco      text,
    direccion       text
)
"""


def _sql_llenar_obs(
    metodo: str, corte: date, idx: int, bloques: int, schema: str = DEFAULT_SCHEMA
) -> str:
    """Carga en la tabla de trabajo las observaciones de UN corte.

    Es la unidad de troceo de la primera fase, y es la correcta porque aquí
    el grano es la observación: cada fila pertenece a un solo corte, así que
    partir por corte no parte nada. Además lee exactamente una partición de
    los hechos y una de la identidad --el `WHERE period` las poda-- en vez
    de recorrer los 82.9M.

    El índice del corte entra como literal: ya se calculó desde el catálogo,
    y es lo que convierte una fecha en la posición que usa el análisis.

    Args:
        metodo: clave de `METODOS`.
        corte: el corte a cargar.
        idx: su posición en la secuencia ordenada de cortes.
        bloques: cuántos bloques repartir.
        schema: schema de origen y destino.

    Returns:
        str: `INSERT ... SELECT` de un corte.
    """
    identidad = METODOS[metodo]
    literal = f"DATE '{corte.isoformat()}'"
    return f"""
INSERT INTO {schema}.{tabla_obs(metodo)} (
    clee_resuelto, bloque, idx, period, id,
    nom_estab, raz_social, codigo_act, cve_ent, cve_mun, cod_postal,
    latitud, longitud, h3_cell_9, fecha_alta_date,
    telefono, correoelec, www, tipo_asent,
    cve_loc, ageb, h3_cell_5, nom_cencom, origen,
    per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
    direccion
)
SELECT
    i.clee_resuelto,
    {_expr_bloque("i.clee_resuelto", bloques)},
    {idx},
    e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cod_postal,
    e.latitud, e.longitud, e.h3_cell_9, e.fecha_alta_date,
    e.telefono, e.correoelec, e.www, ta.tipo_asent,
    e.cve_loc, e.ageb, e.h3_cell_5, e.nom_cencom,
    i.origen,
    ep.per_ocu, ep.categoria, ep.empleados_min, ep.empleados_max,
    tu.tipounieco,
    -- El domicilio como UNA cadena: arrays paralelos por componente
    -- perderian que numero va con que calle.
    NULLIF(CONCAT_WS(' | ',
        NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
        NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')
    ), '') AS direccion
FROM {schema}.establecimientos e
JOIN {schema}.{identidad} i ON i.id = e.id AND i.period = e.period
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE e.period = {literal}
  AND i.period = {literal}
  AND i.clee_resuelto IS NOT NULL AND i.clee_resuelto <> ''
"""


def _pasos_bloque(
    metodo: str, bloque: int, ultimo_idx: int, schema: str = DEFAULT_SCHEMA
) -> list[_Paso]:
    """Los pasos que escriben la permanencia de UN bloque de identidades.

    Las tres temporales sí son `TEMP ... ON COMMIT DROP`: viven dentro de la
    transacción del bloque y se van con ella, commitee o falle.

    El troceo por identidad no cambia un solo resultado --los agregados de
    un establecimiento dependen solo de sus propias filas, y todas están en
    el mismo bloque-- y suele ser más RÁPIDO que hacerlo de golpe: el
    `GROUP BY` completo ordena 10M de grupos con una decena de
    `COUNT(DISTINCT)` cada uno y se derrama a disco, mientras que los
    ~300k grupos de un bloque caben en `work_mem`. Evitar el derrame vale
    más que el costo de dar varias vueltas.

    Args:
        metodo: clave de `METODOS`.
        bloque: qué bloque procesar.
        ultimo_idx: posición del último corte publicado, para la censura.
        schema: schema de origen y destino.

    Returns:
        list[_Paso]: pasos del bloque, en orden; el último es el `INSERT`.
    """
    obs = f"{schema}.{tabla_obs(metodo)}"
    destino = tabla_destino(metodo)
    directo = OrigenClee.DIRECTO.value
    propagado = OrigenClee.PROPAGADO.value

    return [
        _Paso(
            "ventana",
            "colapsando a una fila por identidad",
            f"""
CREATE TEMP TABLE ventana ON COMMIT DROP AS
SELECT
    clee_resuelto,
    MIN(period) AS primer_periodo,
    MAX(period) AS ultimo_periodo,
    MIN(idx) AS idx_primer,
    MAX(idx) AS idx_ultimo,
    COUNT(DISTINCT idx) AS cortes_observados,
    MIN(fecha_alta_date) AS fecha_alta_date,
    -- Resumen de variantes: CUANTAS hubo, no cuales. Los valores salen de
    -- este mismo join sin agregar, filtrando por clee_resuelto.
    -- El FILTER descarta nulos y cadenas vacias: un '' no es una
    -- variante, es un dato ausente.
    COUNT(DISTINCT nom_estab) FILTER (
        WHERE nom_estab IS NOT NULL AND nom_estab <> '') AS n_nombres,
    COUNT(DISTINCT codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS n_actividades,
    COUNT(DISTINCT cve_ent || cve_mun) FILTER (
        WHERE cve_ent IS NOT NULL AND cve_mun IS NOT NULL) AS n_municipios,
    COUNT(DISTINCT direccion) FILTER (WHERE direccion IS NOT NULL) AS n_direcciones,
    COUNT(DISTINCT per_ocu) FILTER (
        WHERE per_ocu IS NOT NULL AND per_ocu <> '') AS n_estratos,
    -- Patron de presencia completo: de aqui el analisis deriva cualquier
    -- medida de ausencia sin volver a los 82.9M de hechos. Es el unico
    -- array que se queda: reconstruirlo exigiria rehacer el mapeo
    -- periodo->indice, y son enteros de a lo mas veinte elementos.
    ARRAY_AGG(DISTINCT idx ORDER BY idx) AS cortes_idx,
    -- Cuanto de la identidad es observado frente a inferido.
    COUNT(DISTINCT idx) FILTER (WHERE origen = '{directo}') AS cortes_directo,
    COUNT(DISTINCT idx) FILTER (WHERE origen = '{propagado}') AS cortes_propagado,
    BOOL_OR(nom_cencom IS NOT NULL AND nom_cencom <> '') AS en_centro_comercial,
    -- Contacto: UNION de todos los cortes, no el mas reciente. Para
    -- verificar un cierre sirve cualquier telefono que haya publicado.
    BOOL_OR(telefono IS NOT NULL AND telefono <> '') AS tiene_telefono,
    BOOL_OR(correoelec IS NOT NULL AND correoelec <> '') AS tiene_correo,
    BOOL_OR(www IS NOT NULL AND www <> '') AS tiene_sitio,
    -- Rectangulo envolvente: cota SUPERIOR del movimiento, ciega al
    -- orden. Se lee junto al desplazamiento neto.
    MIN(latitud) AS lat_min, MAX(latitud) AS lat_max,
    MIN(longitud) AS lon_min, MAX(longitud) AS lon_max,
    COUNT(DISTINCT (latitud, longitud)) FILTER (
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL) AS n_posiciones
FROM {obs}
WHERE bloque = {bloque}
GROUP BY clee_resuelto
""",
        ),
        _Paso(
            "primera",
            "tomando la posicion con que empezo cada identidad",
            f"""
-- La posicion con que empezo. Es el otro extremo REAL del recorrido: el
-- desplazamiento neto se mide entre esta y la del representante, no entre
-- las esquinas del rectangulo, que pueden ser puntos donde el
-- establecimiento nunca estuvo.
CREATE TEMP TABLE primera ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, latitud AS lat_primera, longitud AS lon_primera,
       empleados_min AS empleados_min_primera
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee_resuelto, period ASC, id
""",
        ),
        _Paso(
            "representante",
            "tomando el estado con que termino cada identidad",
            f"""
-- La fila con que el establecimiento "termino": el corte mas reciente,
-- desempatado por id para que el resultado sea determinista.
CREATE TEMP TABLE representante ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, nom_estab, raz_social, codigo_act,
       cve_ent, cve_mun, cod_postal, tipo_asent, latitud, longitud, h3_cell_9,
       per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
       cve_loc, ageb, h3_cell_5
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee_resuelto, period DESC, id
""",
        ),
        _Paso(
            destino,
            f"escribiendo el bloque en {schema}.{destino}",
            f"""
INSERT INTO {schema}.{destino} (
    clee_resuelto, es_sintetica,
    primer_periodo, ultimo_periodo, idx_primer, idx_ultimo,
    cortes_observados, huecos, cortes_tras_ultimo,
    duracion_periodos, presente_desde_el_inicio, fecha_alta_date, dias_alta_a_primera,
    per_ocu, categoria_tamano, empleados_min, empleados_max,
    cambio_tamano, delta_empleados_min,
    tipounieco,
    cve_loc, ageb, h3_cell_5, en_centro_comercial,
    cortes_idx, cortes_directo, cortes_propagado,
    n_nombres, cambio_actividad, cambio_municipio, n_direcciones,
    tiene_telefono, tiene_correo, tiene_sitio,
    lat_primera, lon_primera, n_posiciones, desplazamiento_neto_m,
    lat_min, lat_max, lon_min, lon_max, dispersion_max_m,
    nom_estab, raz_social, codigo_act, cve_ent, cve_mun, cod_postal, tipo_asent,
    latitud, longitud, h3_cell_9
)
SELECT
    v.clee_resuelto,
    STARTS_WITH(v.clee_resuelto, 'NEW_') AS es_sintetica,
    v.primer_periodo, v.ultimo_periodo, v.idx_primer, v.idx_ultimo,
    v.cortes_observados,
    (v.idx_ultimo - v.idx_primer + 1) - v.cortes_observados AS huecos,
    {ultimo_idx} - v.idx_ultimo AS cortes_tras_ultimo,
    v.idx_ultimo - v.idx_primer AS duracion_periodos,
    -- Censura por la izquierda: ya estaba en la primera edicion, asi que su
    -- antiguedad real es desconocida. El protocolo exige el indicador.
    v.idx_primer = 0 AS presente_desde_el_inicio,
    v.fecha_alta_date,
    CASE WHEN v.fecha_alta_date IS NULL THEN NULL
         ELSE (v.primer_periodo - v.fecha_alta_date) END AS dias_alta_a_primera,
    r.per_ocu, r.categoria_tamano, r.empleados_min, r.empleados_max,
    v.n_estratos > 1 AS cambio_tamano,
    -- Trayectoria de tamano: ultimo menos primero, en empleados. Respeta el
    -- orden, asi que distingue crecer de encogerse -- que un conteo de
    -- estratos distintos no puede.
    r.empleados_min - p.empleados_min_primera AS delta_empleados_min,
    r.tipounieco,
    r.cve_loc, r.ageb, r.h3_cell_5,
    COALESCE(v.en_centro_comercial, FALSE) AS en_centro_comercial,
    v.cortes_idx,
    COALESCE(v.cortes_directo, 0) AS cortes_directo,
    COALESCE(v.cortes_propagado, 0) AS cortes_propagado,
    v.n_nombres,
    v.n_actividades > 1 AS cambio_actividad,
    v.n_municipios > 1 AS cambio_municipio,
    v.n_direcciones,
    COALESCE(v.tiene_telefono, FALSE) AS tiene_telefono,
    COALESCE(v.tiene_correo, FALSE) AS tiene_correo,
    COALESCE(v.tiene_sitio, FALSE) AS tiene_sitio,
    p.lat_primera, p.lon_primera,
    COALESCE(v.n_posiciones, 0) AS n_posiciones,
    -- Neto: entre dos puntos REALES, respetando el orden temporal.
    CASE WHEN p.lat_primera IS NULL OR r.latitud IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(r.latitud - p.lat_primera) / 2), 2)
            + COS(RADIANS(p.lat_primera)) * COS(RADIANS(r.latitud))
            * POWER(SIN(RADIANS(r.longitud - p.lon_primera) / 2), 2)
        ))
    END AS desplazamiento_neto_m,
    v.lat_min, v.lat_max, v.lon_min, v.lon_max,
    -- Dispersion: la diagonal del rectangulo. Cota superior, no recorrido.
    CASE WHEN v.lat_min IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(v.lat_max - v.lat_min) / 2), 2)
            + COS(RADIANS(v.lat_min)) * COS(RADIANS(v.lat_max))
            * POWER(SIN(RADIANS(v.lon_max - v.lon_min) / 2), 2)
        ))
    END AS dispersion_max_m,
    r.nom_estab, r.raz_social, r.codigo_act, r.cve_ent, r.cve_mun, r.cod_postal,
    r.tipo_asent,
    r.latitud, r.longitud, r.h3_cell_9
FROM ventana v
JOIN primera p USING (clee_resuelto)
JOIN representante r USING (clee_resuelto)
""",
        ),
    ]


def _obs_existe(conn: Connection, metodo: str, schema: str) -> bool:
    """Si la tabla de trabajo de un método ya está creada."""
    return bool(
        conn.execute(
            text("SELECT to_regclass(:nombre) IS NOT NULL"),
            {"nombre": f"{schema}.{tabla_obs(metodo)}"},
        ).scalar()
    )


def _marcar_obs(conn: Connection, metodo: str, schema: str) -> None:
    """Deja fechada la tabla de trabajo, para validar reanudaciones.

    La marca va en el COMMENT de la tabla y no en una columna: una columna
    de fecha serían 8 bytes por cada una de las 82.9M filas para guardar un
    solo dato, y el comentario se va solo cuando la tabla se borra.

    Args:
        conn: conexión con permisos de DDL.
        metodo: clave de `METODOS`.
        schema: schema de la tabla.
    """
    conn.execute(
        text(
            f"COMMENT ON TABLE {schema}.{tabla_obs(metodo)} IS "
            f"'armado_en={datetime.now(UTC).isoformat()}'"
        )
    )


def _identidad_mas_nueva_que_obs(conn: Connection, metodo: str, schema: str) -> bool:
    """Si la resolución CLEE se re-corrió después de armar la tabla de trabajo.

    Reanudar en ese caso sería **incorrecto**, no solo inútil: los bloques
    ya escritos vendrían de una resolución y los pendientes de otra, y la
    tabla final mezclaría dos conjuntos de identidades sin que nada lo
    delate. Las `NEW_*` ni siquiera son estables entre corridas, así que la
    misma fila puede aparecer con dos identidades distintas.

    Compara la marca que dejó `_marcar_obs` contra `resuelto_en`, la columna
    que la resolución escribe justamente para poder fechar un resultado.

    Args:
        conn: conexión de lectura.
        metodo: clave de `METODOS`.
        schema: schema de las tablas.

    Returns:
        bool: True si la identidad cambió y hay que rehacer desde cero.
    """
    marca = conn.execute(
        text(f"SELECT obj_description('{schema}.{tabla_obs(metodo)}'::regclass, 'pg_class')")
    ).scalar()
    if not marca or not marca.startswith("armado_en="):
        # Sin marca no se puede afirmar que la identidad siga siendo la
        # misma, y el riesgo de mezclarlas no se corre a la ligera.
        return True

    return bool(
        conn.execute(
            text(f"SELECT max(resuelto_en) > :armado FROM {schema}.{METODOS[metodo]}"),
            {"armado": marca.removeprefix("armado_en=")},
        ).scalar()
    )


def _cortes_pendientes(
    conn: Connection, metodo: str, cortes: list[date], schema: str
) -> list[tuple[int, date]]:
    """Los cortes que aún no están en la tabla de trabajo, con su índice.

    El avance se deriva de la propia tabla en vez de un registro aparte: un
    corte que falló a la mitad hizo rollback y no dejó ni una fila, así que
    "tiene filas" equivale a "está completo".

    Args:
        conn: conexión de lectura.
        metodo: clave de `METODOS`.
        cortes: todos los cortes, ordenados.
        schema: schema de las tablas.

    Returns:
        list[tuple[int, date]]: pares (índice, corte) por cargar.
    """
    hechos = set(
        conn.execute(text(f"SELECT DISTINCT period FROM {schema}.{tabla_obs(metodo)}"))
        .scalars()
        .all()
    )
    return [(i, c) for i, c in enumerate(cortes) if c not in hechos]


def _bloques_pendientes(conn: Connection, metodo: str, bloques: int, schema: str) -> list[int]:
    """Los bloques que aún no están en la tabla destino.

    Mismo criterio que con los cortes: el `INSERT` de un bloque es una
    transacción, así que o escribió todas sus identidades o ninguna.

    Args:
        conn: conexión de lectura.
        metodo: clave de `METODOS`.
        bloques: cuántos bloques en total.
        schema: schema de las tablas.

    Returns:
        list[int]: bloques por procesar, en orden.
    """
    escritos = set(
        conn.execute(
            text(f"""
            SELECT DISTINCT {_expr_bloque("clee_resuelto", bloques)}
            FROM {schema}.{tabla_destino(metodo)}
            """)
        )
        .scalars()
        .all()
    )
    return [b for b in range(bloques) if b not in escritos]


def _ejecutar(conn: Connection, paso: _Paso, etiqueta: str) -> int:
    """Corre un paso con latido y lo registra al terminar.

    Args:
        conn: conexión dentro de una transacción.
        paso: qué ejecutar.
        etiqueta: prefijo del log, p. ej. `bloque 7/32`.

    Returns:
        int: filas que produjo.
    """
    inicio = time.monotonic()
    with _latido(conn, f"{etiqueta} {paso.nombre}"):
        resultado = conn.execute(text(paso.sql))
    segundos = time.monotonic() - inicio
    logger.info(f"      {etiqueta} {paso.nombre}: {resultado.rowcount:,} filas en {segundos:.1f}s")
    return resultado.rowcount


def construir_permanencia(
    engine: Engine, metodo: str, schema: str = DEFAULT_SCHEMA, force: bool = False
) -> int:
    """Construye la tabla de permanencia de un método, por partes y reanudable.

    El trabajo se parte en dos fases, cada una con la llave de troceo que su
    grano admite:

    1. **Por corte** (20 vueltas) se llena `permanencia_obs_<metodo>`, que
       une los hechos con la identidad. Aquí el grano es la observación, así
       que partir por corte no parte nada, y cada vuelta lee una sola
       partición de los hechos.
    2. **Por bloque de identidad** (`PERMANENCIA_BLOQUES` vueltas) se
       agregan y se escriben. Aquí el grano es el establecimiento, que
       **cruza** los cortes: trocear por año daría agregados calculados
       sobre un fragmento de su vida. La llave es `_expr_bloque`.

    **Cada vuelta es su propia transacción y commitea.** Eso es el punto: un
    fallo en la vuelta 14 no se lleva las 13 anteriores. El precio es que se
    pierde el reemplazo todo-o-nada --mientras corre, la tabla destino está
    parcialmente llena-- y se acepta porque es una tabla derivada que solo
    lee el análisis entre corridas, y rehacerla no cuesta datos.

    **Por default reanuda**, misma convención que la resolución CLEE, que
    salta los cortes que ya resolvió: el avance se deriva de las tablas, sin
    registro aparte. `force=True` tira la tabla de trabajo y la destino y
    empieza de cero. Si la resolución CLEE se re-corrió desde la última vez,
    se niega a reanudar: ver `_identidad_mas_nueva_que_obs`.

    Args:
        engine: engine con permisos de escritura; esta función controla sus
            transacciones, una por vuelta.
        metodo: clave de `METODOS` (`radio` o `h3`).
        schema: schema de origen y destino.
        force: si True, descarta lo que haya y reconstruye entero.

    Returns:
        int: filas en la tabla destino al terminar.

    Raises:
        ValueError: si el método no está registrado.
        RuntimeError: si hay avance previo pero la identidad cambió y no se
            pidió `force`.
    """
    if metodo not in METODOS:
        raise ValueError(f"Metodo desconocido '{metodo}'. Opciones: {list(METODOS)}")

    bloques = max(get_settings().PERMANENCIA_BLOQUES, 1)
    destino = tabla_destino(metodo)
    obs = tabla_obs(metodo)

    with engine.begin() as conn:
        cortes = _cortes_ordenados(conn, schema)
        if not cortes:
            logger.warning(
                f"   {metodo}: no hay cortes en {schema}.establecimientos, nada que construir"
            )
            return 0

        if force:
            logger.info(f"   {metodo}: --force, se descarta el avance previo")
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.{obs}"))
            conn.execute(text(f"TRUNCATE {schema}.{destino}"))
        elif _obs_existe(conn, metodo, schema) and _identidad_mas_nueva_que_obs(
            conn, metodo, schema
        ):
            raise RuntimeError(
                f"{schema}.{METODOS[metodo]} se re-resolvio despues de armar {schema}.{obs}: "
                f"reanudar mezclaria identidades de dos corridas. Corre con --force."
            )

        nueva = not _obs_existe(conn, metodo, schema)
        conn.execute(text(_sql_crear_obs(metodo, schema)))
        if nueva:
            _marcar_obs(conn, metodo, schema)
        pendientes = _cortes_pendientes(conn, metodo, cortes, schema)

    hechos = len(cortes) - len(pendientes)
    logger.info(
        f"   {metodo} -> {schema}.{destino}: {len(cortes)} cortes "
        f"({cortes[0]} .. {cortes[-1]}) en {bloques} bloques"
    )
    if hechos:
        logger.info(f"   reanudando: {hechos}/{len(cortes)} cortes ya estaban en {obs}")

    # ---- Fase 1: los hechos unidos a la identidad, un corte por vuelta ----
    inicio = time.monotonic()
    for n, (idx, corte) in enumerate(pendientes, start=hechos + 1):
        etiqueta = f"obs {n}/{len(cortes)}"
        logger.info(f"   ▶ {etiqueta}: cargando el corte {corte}...")
        with engine.begin() as conn:
            conn.execute(text(SET_WORK_MEM))
            with _latido(conn, etiqueta):
                filas = conn.execute(
                    text(_sql_llenar_obs(metodo, corte, idx, bloques, schema))
                ).rowcount
        logger.info(f"   ✔ {etiqueta}: {corte} -> {filas:,} observaciones")

    with engine.begin() as conn:
        total_obs = conn.execute(text(f"SELECT count(*) FROM {schema}.{obs}")).scalar_one()
        if total_obs == 0:
            logger.warning(
                f"   ⚠️ {destino} vacia: {schema}.{METODOS[metodo]} sin poblar. "
                f"Corre primero `transform --only clee-{metodo}`."
            )
            conn.execute(text(f"DROP TABLE IF EXISTS {schema}.{obs}"))
            return 0
        # Indice y estadisticas DESPUES de llenar: construirlo antes
        # costaria mantenerlo en los 20 INSERT, y una tabla sin ANALYZE deja
        # al planificador armando los bloques a ciegas (el autovacuum no
        # toca las UNLOGGED recien creadas a tiempo).
        logger.info(f"   ▶ indice y estadisticas sobre {total_obs:,} observaciones...")
        conn.execute(text(f"CREATE INDEX IF NOT EXISTS ix_{obs}_bloque ON {schema}.{obs} (bloque)"))
        conn.execute(text(f"ANALYZE {schema}.{obs}"))
        faltan = _bloques_pendientes(conn, metodo, bloques, schema)

    if len(faltan) < bloques:
        logger.info(f"   reanudando: {bloques - len(faltan)}/{bloques} bloques ya escritos")

    # ---- Fase 2: los agregados, un bloque de identidades por vuelta ----
    ultimo_idx = len(cortes) - 1
    for bloque in faltan:
        etiqueta = f"bloque {bloque + 1}/{bloques}"
        inicio_bloque = time.monotonic()
        with engine.begin() as conn:
            conn.execute(text(SET_WORK_MEM))
            escritas = 0
            for paso in _pasos_bloque(metodo, bloque, ultimo_idx, schema):
                escritas = _ejecutar(conn, paso, etiqueta)
        logger.info(
            f"   ✔ {etiqueta}: {escritas:,} establecimientos en "
            f"{(time.monotonic() - inicio_bloque) / 60:.1f} min"
        )

    with engine.begin() as conn:
        total = conn.execute(text(f"SELECT count(*) FROM {schema}.{destino}")).scalar_one()
        conn.execute(text(f"DROP TABLE IF EXISTS {schema}.{obs}"))

    minutos = (time.monotonic() - inicio) / 60
    logger.info(f"   ✅ {destino}: {total:,} establecimientos en {minutos:.1f} min")
    return total
