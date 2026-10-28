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
    cortes_observados, huecos, cortes_tras_ultimo, fecha_alta_date,
    nombres, n_nombres, razones_sociales, codigos_act, cambio_actividad,
    municipios, cambio_municipio, cod_postales, desplazamiento_m,
    nom_estab, raz_social, codigo_act, cve_ent, cve_mun, cod_postal,
    latitud, longitud, h3_cell_9
)
WITH cortes(period, idx) AS (VALUES {valores}),
obs AS (
    SELECT
        i.clee_resuelto, c.idx, e.period, e.id,
        e.nom_estab, e.raz_social, e.codigo_act,
        e.cve_ent, e.cve_mun, e.cod_postal,
        e.latitud, e.longitud, e.h3_cell_9, e.fecha_alta_date
    FROM {schema}.establecimientos e
    JOIN {schema}.{identidad} i ON i.id = e.id AND i.period = e.period
    JOIN cortes c ON c.period = e.period
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
        -- Rectangulo envolvente de sus coordenadas: cuatro agregados en vez
        -- de comparar todos los pares de observaciones.
        MIN(latitud) AS lat_min, MAX(latitud) AS lat_max,
        MIN(longitud) AS lon_min, MAX(longitud) AS lon_max
    FROM obs
    GROUP BY clee_resuelto
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
    v.fecha_alta_date,
    v.nombres,
    COALESCE(CARDINALITY(v.nombres), 0) AS n_nombres,
    v.razones_sociales,
    v.codigos_act,
    COALESCE(CARDINALITY(v.codigos_act), 0) > 1 AS cambio_actividad,
    v.municipios,
    COALESCE(CARDINALITY(v.municipios), 0) > 1 AS cambio_municipio,
    v.cod_postales,
    CASE WHEN v.lat_min IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(v.lat_max - v.lat_min) / 2), 2)
            + COS(RADIANS(v.lat_min)) * COS(RADIANS(v.lat_max))
            * POWER(SIN(RADIANS(v.lon_max - v.lon_min) / 2), 2)
        ))
    END AS desplazamiento_m,
    r.nom_estab, r.raz_social, r.codigo_act, r.cve_ent, r.cve_mun, r.cod_postal,
    r.latitud, r.longitud, r.h3_cell_9
FROM ventana v
JOIN representante r USING (clee_resuelto)
WHERE r.rn = 1
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
