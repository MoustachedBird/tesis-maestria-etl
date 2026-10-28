"""Construye `silver.permanencia_<metodo>` desde los hechos y la identidad.

Colapsa las 82.9M observaciones de `silver.establecimientos` a una fila por
establecimiento resuelto: su ventana de permanencia en el directorio, los
alias que tuvo y los ingredientes de censura.

Existe como tabla materializada y no como vista porque el `GROUP BY` sobre
82.9M filas unidas a la tabla de identidades cuesta minutos, y el modelo de
supervivencia la lee decenas de veces --cada partición de la validación
cruzada, cada iteración de variables, el cálculo de SHAP.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.partitions import existing_partitions
from src.shared.enums import EstatusNegocio
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

CORTES_PARA_CIERRE = 2
"""Cortes consecutivos de ausencia que la regla toma como cierre.

Viene del protocolo: "se considerará cerrado el establecimiento que deja de
aparecer en dos cortes consecutivos y no reaparece después, lo que reduce el
riesgo de confundir un cierre con una omisión del directorio a costa de
fechar el evento con menor precisión".

**La regla es preliminar** y de ahí que viva aquí y se materialice en una
VISTA en vez de una columna: cambiar el umbral redefine la vista en un
segundo, mientras que una columna obligaría a reconstruir 10M de filas.
"""

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra, para la diagonal del rectángulo envolvente."""

SET_WORK_MEM = "SET LOCAL work_mem = '4GB'"
"""Memoria de trabajo para esta consulta y solo para ella.

El agregado produce ~10M grupos con cinco arrays cada uno; con el `work_mem`
global (256 MB, modesto a propósito porque es POR NODO y POR CONEXIÓN) el
hash aggregate se derrama a disco y tarda mucho más. Subirlo por sesión en
las cargas masivas es el patrón que documenta `.env.example`.
"""


def tabla_destino(metodo: str) -> str:
    """Nombre de la tabla de permanencia de un método, sin calificar."""
    return f"permanencia_{metodo}"


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


def _build_sql(metodo: str, cortes: list[date], schema: str = DEFAULT_SCHEMA) -> str:
    """SQL que llena la tabla de permanencia de un método.

    Los cortes entran como `VALUES` y no se consultan dentro del SQL: ya se
    leyeron del catálogo, y su posición en la secuencia es lo que convierte
    una fecha en el índice que usa el análisis.

    Args:
        metodo: clave de `METODOS`.
        cortes: cortes ordenados, del más antiguo al más nuevo.
        schema: schema de origen y destino.

    Returns:
        str: `INSERT ... SELECT` completo.
    """
    identidad = METODOS[metodo]
    destino = tabla_destino(metodo)
    ultimo_idx = len(cortes) - 1

    valores = ", ".join(f"(DATE '{c.isoformat()}', {i})" for i, c in enumerate(cortes))

    return f"""
INSERT INTO {schema}.{destino} (
    clee_resuelto, es_sintetica,
    primer_periodo, ultimo_periodo, idx_primer, idx_ultimo,
    cortes_observados, huecos, cortes_tras_ultimo,
    duracion_periodos, presente_desde_el_inicio, fecha_alta_date, dias_alta_a_primera,
    nombres, n_nombres, razones_sociales, codigos_act, cambio_actividad,
    municipios, cambio_municipio, cod_postales,
    direcciones, n_direcciones, tipos_asent,
    telefonos, correos, sitios,
    lat_primera, lon_primera, n_posiciones, desplazamiento_neto_m,
    lat_min, lat_max, lon_min, lon_max, dispersion_max_m,
    nom_estab, raz_social, codigo_act, cve_ent, cve_mun, cod_postal,
    latitud, longitud, h3_cell_9
)
WITH cortes(period, idx) AS (VALUES {valores}),
obs AS (
    SELECT
        i.clee_resuelto, c.idx, e.period, e.id,
        e.nom_estab, e.raz_social, e.codigo_act,
        e.cve_ent, e.cve_mun, e.cod_postal,
        e.latitud, e.longitud, e.h3_cell_9, e.fecha_alta_date,
        e.telefono, e.correoelec, e.www, ta.tipo_asent,
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
    WHERE i.clee_resuelto IS NOT NULL AND i.clee_resuelto <> ''
),
ventana AS (
    SELECT
        clee_resuelto,
        MIN(period) AS primer_periodo,
        MAX(period) AS ultimo_periodo,
        MIN(idx) AS idx_primer,
        MAX(idx) AS idx_ultimo,
        COUNT(DISTINCT idx) AS cortes_observados,
        MIN(fecha_alta_date) AS fecha_alta_date,
        -- Arrays de variantes. El FILTER descarta nulos y cadenas vacias:
        -- un array con un '' no es una variante, es un dato ausente.
        ARRAY_AGG(DISTINCT nom_estab) FILTER (
            WHERE nom_estab IS NOT NULL AND nom_estab <> '') AS nombres,
        ARRAY_AGG(DISTINCT raz_social) FILTER (
            WHERE raz_social IS NOT NULL AND raz_social <> '') AS razones_sociales,
        ARRAY_AGG(DISTINCT codigo_act) FILTER (
            WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS codigos_act,
        ARRAY_AGG(DISTINCT cve_ent || cve_mun) FILTER (
            WHERE cve_ent IS NOT NULL AND cve_mun IS NOT NULL) AS municipios,
        ARRAY_AGG(DISTINCT cod_postal) FILTER (
            WHERE cod_postal IS NOT NULL AND cod_postal <> '') AS cod_postales,
        ARRAY_AGG(DISTINCT direccion) FILTER (WHERE direccion IS NOT NULL) AS direcciones,
        ARRAY_AGG(DISTINCT tipo_asent) FILTER (
            WHERE tipo_asent IS NOT NULL AND tipo_asent <> '') AS tipos_asent,
        -- Contacto: UNION de todos los cortes, no el mas reciente. Para
        -- verificar un cierre sirve cualquier telefono que haya publicado.
        ARRAY_AGG(DISTINCT telefono) FILTER (
            WHERE telefono IS NOT NULL AND telefono <> '') AS telefonos,
        ARRAY_AGG(DISTINCT correoelec) FILTER (
            WHERE correoelec IS NOT NULL AND correoelec <> '') AS correos,
        ARRAY_AGG(DISTINCT www) FILTER (WHERE www IS NOT NULL AND www <> '') AS sitios,
        -- Rectangulo envolvente: cota SUPERIOR del movimiento, ciega al
        -- orden. Se lee junto al desplazamiento neto de abajo.
        MIN(latitud) AS lat_min, MAX(latitud) AS lat_max,
        MIN(longitud) AS lon_min, MAX(longitud) AS lon_max,
        COUNT(DISTINCT (latitud, longitud)) FILTER (
            WHERE latitud IS NOT NULL AND longitud IS NOT NULL) AS n_posiciones
    FROM obs
    GROUP BY clee_resuelto
),
primera AS (
    -- La posicion con que empezo. Es el otro extremo REAL del recorrido:
    -- el desplazamiento neto se mide entre esta y la del representante, no
    -- entre las esquinas del rectangulo, que pueden ser puntos donde el
    -- establecimiento nunca estuvo.
    SELECT clee_resuelto, latitud AS lat_primera, longitud AS lon_primera,
           ROW_NUMBER() OVER (PARTITION BY clee_resuelto ORDER BY period ASC, id) AS rn
    FROM obs
),
representante AS (
    -- La fila con que el establecimiento "termino": el corte mas reciente,
    -- desempatado por id para que el resultado sea determinista.
    SELECT clee_resuelto, nom_estab, raz_social, codigo_act,
           cve_ent, cve_mun, cod_postal, latitud, longitud, h3_cell_9,
           ROW_NUMBER() OVER (PARTITION BY clee_resuelto ORDER BY period DESC, id) AS rn
    FROM obs
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
    v.nombres,
    COALESCE(CARDINALITY(v.nombres), 0) AS n_nombres,
    v.razones_sociales,
    v.codigos_act,
    COALESCE(CARDINALITY(v.codigos_act), 0) > 1 AS cambio_actividad,
    v.municipios,
    COALESCE(CARDINALITY(v.municipios), 0) > 1 AS cambio_municipio,
    v.cod_postales,
    v.direcciones,
    COALESCE(CARDINALITY(v.direcciones), 0) AS n_direcciones,
    v.tipos_asent,
    v.telefonos, v.correos, v.sitios,
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
    r.latitud, r.longitud, r.h3_cell_9
FROM ventana v
JOIN primera p USING (clee_resuelto)
JOIN representante r USING (clee_resuelto)
WHERE p.rn = 1 AND r.rn = 1
"""


def construir_permanencia(conn: Connection, metodo: str, schema: str = DEFAULT_SCHEMA) -> int:
    """Reconstruye la tabla de permanencia de un método, entera.

    Es reconstrucción total y no incremental porque las identidades
    sintéticas `NEW_*` no son estables entre corridas: un `--force` de la
    resolución les asigna otro id, así que no hay nada que preservar.

    `TRUNCATE` e `INSERT` van en la transacción del llamador: la tabla no
    está particionada, así que no hay swap que hacer y el reemplazo es
    atómico por la transacción.

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
    logger.info(
        f"   {metodo} -> {schema}.{destino}: colapsando {len(cortes)} cortes "
        f"({cortes[0]} .. {cortes[-1]})..."
    )

    conn.execute(text(SET_WORK_MEM))
    conn.execute(text(f"TRUNCATE {schema}.{destino}"))
    resultado = conn.execute(text(_build_sql(metodo, cortes, schema)))
    escritas = resultado.rowcount

    if escritas == 0:
        logger.warning(
            f"   ⚠️ {destino} vacia: {schema}.{METODOS[metodo]} sin poblar. "
            f"Corre primero `transform --only clee-{metodo}`."
        )
    else:
        logger.info(f"   ✅ {destino}: {escritas:,} establecimientos")

    return escritas


def _vista_sql(metodo: str, schema: str = DEFAULT_SCHEMA) -> str:
    """SQL de la vista que aplica la regla de cierre a un método.

    El estatus es una VISTA y no una columna de `permanencia_<metodo>`
    porque la regla es preliminar: `docs/findings.md` §2 mide que la tasa de
    desaparición oscila por un factor de 400 entre pares de cortes, lo que
    sugiere que buena parte de las ausencias son reasignación de claves. Si
    la validación externa mueve el umbral, redefinir una vista cuesta un
    segundo y reconstruir una columna cuesta 10M de filas.

    `reaparecio` va en la vista porque cambia cómo se lee el estatus: un
    establecimiento que ya desapareció y volvió demostró, para sí mismo, que
    una ausencia no implica cierre.

    Args:
        metodo: clave de `METODOS`.
        schema: schema de la tabla y de la vista.

    Returns:
        str: `CREATE OR REPLACE VIEW` completo.
    """
    origen = tabla_destino(metodo)
    return f"""
CREATE OR REPLACE VIEW {schema}.estatus_{metodo} AS
SELECT
    p.*,
    CASE
        WHEN p.cortes_tras_ultimo = 0 THEN '{EstatusNegocio.ACTIVO.value}'
        WHEN p.cortes_tras_ultimo >= {CORTES_PARA_CIERRE} THEN '{EstatusNegocio.CERRADO.value}'
        ELSE '{EstatusNegocio.INCIERTO.value}'
    END AS estatus,
    p.huecos > 0 AS reaparecio
FROM {schema}.{origen} p
"""


def crear_vistas_estatus(conn: Connection, schema: str = DEFAULT_SCHEMA) -> list[str]:
    """Crea o redefine la vista de estatus de cada método.

    Idempotente: `CREATE OR REPLACE`. Se llama desde la migración y puede
    re-ejecutarse cuando cambie `CORTES_PARA_CIERRE`.

    Args:
        conn: conexión con permisos de DDL.
        schema: schema de las tablas y vistas.

    Returns:
        list[str]: nombres de las vistas creadas.
    """
    creadas = []
    for metodo in METODOS:
        conn.execute(text(_vista_sql(metodo, schema)))
        creadas.append(f"{schema}.estatus_{metodo}")
    return creadas
