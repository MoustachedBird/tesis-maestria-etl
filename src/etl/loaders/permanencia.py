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
from datetime import date
from pathlib import Path
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection

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


def _pasos(metodo: str, cortes: list[date], schema: str = DEFAULT_SCHEMA) -> list[_Paso]:
    """Los pasos que llenan la tabla de permanencia de un método, en orden.

    El trabajo se parte en tablas temporales con nombre en vez de ir como un
    solo `INSERT ... SELECT` con CTEs. La razón es de operación, no de SQL:
    una sentencia de horas es opaca --si muere, el log dice que empezó y
    nada más-- mientras que cinco pasos dicen cuál falló, cuántas filas
    produjo cada uno y cuánto tardó.

    No es un costo que se paga por el diagnóstico: `obs` se materializaba de
    todos modos, porque PostgreSQL materializa un CTE referenciado más de
    una vez y este lo usan tres. Lo que cambia es que ahora se puede
    `ANALYZE`: una tabla temporal nace sin estadísticas y el autovacuum no
    las toca, así que sin esto el planificador arma los joins finales a
    ciegas.

    Las temporales son `ON COMMIT DROP`: viven dentro de la transacción del
    llamador y se van con ella, así que un fallo no deja basura.

    Los cortes entran como `VALUES` y no se consultan dentro del SQL: ya se
    leyeron del catálogo, y su posición en la secuencia es lo que convierte
    una fecha en el índice que usa el análisis.

    Args:
        metodo: clave de `METODOS`.
        cortes: cortes ordenados, del más antiguo al más nuevo.
        schema: schema de origen y destino.

    Returns:
        list[_Paso]: pasos a ejecutar en orden; el último es el `INSERT`.
    """
    identidad = METODOS[metodo]
    destino = tabla_destino(metodo)
    ultimo_idx = len(cortes) - 1

    valores = ", ".join(f"(DATE '{c.isoformat()}', {i})" for i, c in enumerate(cortes))
    directo = OrigenClee.DIRECTO.value
    propagado = OrigenClee.PROPAGADO.value

    return [
        _Paso(
            "obs",
            f"uniendo los hechos con {identidad} y resolviendo catalogos",
            f"""
CREATE TEMP TABLE obs ON COMMIT DROP AS
WITH cortes(period, idx) AS (VALUES {valores})
SELECT
    i.clee_resuelto, c.idx, e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cod_postal,
    e.latitud, e.longitud, e.h3_cell_9, e.fecha_alta_date,
    e.telefono, e.correoelec, e.www, ta.tipo_asent,
    e.cve_loc, e.ageb, e.h3_cell_5, e.nom_cencom,
    i.origen,
    ep.per_ocu, ep.categoria AS categoria_tamano,
    ep.empleados_min, ep.empleados_max,
    tu.tipounieco,
    -- El domicilio como UNA cadena: arrays paralelos por componente
    -- perderian que numero va con que calle.
    NULLIF(CONCAT_WS(' | ',
        NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
        NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')
    ), '') AS direccion
FROM {schema}.establecimientos e
JOIN {schema}.{identidad} i ON i.id = e.id AND i.period = e.period
JOIN cortes c ON c.period = e.period
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE i.clee_resuelto IS NOT NULL AND i.clee_resuelto <> ''
""",
        ),
        _Paso(
            "ventana",
            "colapsando a una fila por identidad (el agregado mas caro)",
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
FROM obs
GROUP BY clee_resuelto
""",
        ),
        _Paso(
            "primera",
            "tomando la posicion con que empezo cada identidad",
            """
-- La posicion con que empezo. Es el otro extremo REAL del recorrido: el
-- desplazamiento neto se mide entre esta y la del representante, no entre
-- las esquinas del rectangulo, que pueden ser puntos donde el
-- establecimiento nunca estuvo.
--
-- DISTINCT ON y no ROW_NUMBER: con tabla temporal, filtrar rn = 1 despues
-- obligaria a materializar las 82.9M filas numeradas para quedarse con 10M.
CREATE TEMP TABLE primera ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, latitud AS lat_primera, longitud AS lon_primera,
       empleados_min AS empleados_min_primera
FROM obs
ORDER BY clee_resuelto, period ASC, id
""",
        ),
        _Paso(
            "representante",
            "tomando el estado con que termino cada identidad",
            """
-- La fila con que el establecimiento "termino": el corte mas reciente,
-- desempatado por id para que el resultado sea determinista.
CREATE TEMP TABLE representante ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, nom_estab, raz_social, codigo_act,
       cve_ent, cve_mun, cod_postal, tipo_asent, latitud, longitud, h3_cell_9,
       per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
       cve_loc, ageb, h3_cell_5
FROM obs
ORDER BY clee_resuelto, period DESC, id
""",
        ),
        _Paso(
            destino,
            f"escribiendo {schema}.{destino}",
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


def construir_permanencia(conn: Connection, metodo: str, schema: str = DEFAULT_SCHEMA) -> int:
    """Reconstruye la tabla de permanencia de un método, entera.

    Es reconstrucción total y no incremental porque las identidades
    sintéticas `NEW_*` no son estables entre corridas: un `--force` de la
    resolución les asigna otro id, así que no hay nada que preservar.

    `TRUNCATE`, temporales e `INSERT` van en la transacción del llamador: la
    tabla no está particionada, así que no hay swap que hacer y el reemplazo
    es atómico por la transacción. Si algo falla a la mitad, la tabla queda
    como estaba y las temporales se van con el rollback.

    Cada paso se registra antes de empezar y al terminar, con sus filas y su
    tiempo, así que el log dice en qué paso se quedó si el proceso muere.

    Args:
        conn: conexión con permisos de escritura; el llamador controla la
            transacción.
        metodo: clave de `METODOS` (`radio` o `h3`).
        schema: schema de origen y destino.

    Returns:
        int: filas escritas.

    Raises:
        ValueError: si el método no está registrado.
    """
    if metodo not in METODOS:
        raise ValueError(f"Metodo desconocido '{metodo}'. Opciones: {list(METODOS)}")

    cortes = _cortes_ordenados(conn, schema)
    if not cortes:
        logger.warning(
            f"   {metodo}: no hay cortes en {schema}.establecimientos, nada que construir"
        )
        return 0

    destino = tabla_destino(metodo)
    pasos = _pasos(metodo, cortes, schema)
    logger.info(
        f"   {metodo} -> {schema}.{destino}: colapsando {len(cortes)} cortes "
        f"({cortes[0]} .. {cortes[-1]}) en {len(pasos)} pasos..."
    )

    inicio = time.monotonic()
    conn.execute(text(SET_WORK_MEM))
    conn.execute(text(f"TRUNCATE {schema}.{destino}"))

    escritas = 0
    for n, paso in enumerate(pasos, start=1):
        etiqueta = f"[{n}/{len(pasos)}] {paso.nombre}"
        logger.info(f"   ▶ {etiqueta}: {paso.descripcion}...")
        inicio_paso = time.monotonic()

        with _latido(conn, etiqueta):
            resultado = conn.execute(text(paso.sql))
        escritas = resultado.rowcount
        min_paso = (time.monotonic() - inicio_paso) / 60

        # ANALYZE porque una temporal nace sin estadisticas y el autovacuum
        # no las toca: sin esto el planificador arma los pasos siguientes a
        # ciegas. En el ultimo paso no hay nada que analizar.
        if n < len(pasos):
            conn.execute(text(f"ANALYZE {paso.nombre}"))

        logger.info(
            f"   ✔ {etiqueta}: {escritas:,} filas en {min_paso:.1f} min "
            f"({escritas / max(min_paso * 60, 1):,.0f} filas/s)"
        )

    minutos = (time.monotonic() - inicio) / 60

    if escritas == 0:
        logger.warning(
            f"   ⚠️ {destino} vacia: {schema}.{METODOS[metodo]} sin poblar. "
            f"Corre primero `transform --only clee-{metodo}`."
        )
    else:
        logger.info(
            f"   ✅ {destino}: {escritas:,} establecimientos en {minutos:.1f} min "
            f"({escritas / max(minutos * 60, 1):,.0f} filas/s)"
        )

    return escritas
