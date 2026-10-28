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
consumir. Para los valores está la vista `silver.variantes_<metodo>`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.partitions import existing_partitions
from src.shared.enums import EstatusNegocio, OrigenClee
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

El agregado produce ~10M grupos, cada uno con una decena de `COUNT(DISTINCT
...)` que mantienen su propio conjunto mientras dura el grupo. Con el
`work_mem` global (256 MB, modesto a propósito porque es POR NODO y POR
CONEXIÓN) el hash aggregate se derrama a disco y tarda mucho más. Subirlo
por sesión en las cargas masivas es el patrón que documenta `.env.example`.
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
    directo = OrigenClee.DIRECTO.value
    propagado = OrigenClee.PROPAGADO.value

    return f"""
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
WITH cortes(period, idx) AS (VALUES {valores}),
obs AS (
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
        -- Resumen de variantes: CUANTAS hubo, no cuales. Los valores salen de
        -- la vista `variantes_<metodo>`, que es este mismo join sin agregar.
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
        -- periodo->indice, y son smallint de a lo mas veinte elementos.
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
           empleados_min AS empleados_min_primera,
           ROW_NUMBER() OVER (PARTITION BY clee_resuelto ORDER BY period ASC, id) AS rn
    FROM obs
),
representante AS (
    -- La fila con que el establecimiento "termino": el corte mas reciente,
    -- desempatado por id para que el resultado sea determinista.
    SELECT clee_resuelto, nom_estab, raz_social, codigo_act,
           cve_ent, cve_mun, cod_postal, tipo_asent, latitud, longitud, h3_cell_9,
           per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
           cve_loc, ageb, h3_cell_5,
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


def _vista_variantes_sql(metodo: str, schema: str = DEFAULT_SCHEMA) -> str:
    """SQL de la vista que devuelve las variantes de un establecimiento.

    Es el join que `permanencia_<metodo>` deliberadamente no materializa:
    une los hechos con la tabla de identidad y deja una fila por corte, con
    los campos que varían entre ellos.

    Responde "¿qué nombres/domicilios/teléfonos tuvo ESTE establecimiento?".
    Es barata para eso --`clee_resuelto` está indexado en la tabla de
    identidad y el regreso a los hechos va por su llave primaria-- y cara si
    se pide sin filtro, que es justo la consulta que el análisis no hace.

    Args:
        metodo: clave de `METODOS`.
        schema: schema de las tablas y de la vista.

    Returns:
        str: `CREATE OR REPLACE VIEW` completo.
    """
    identidad = METODOS[metodo]
    return f"""
CREATE OR REPLACE VIEW {schema}.variantes_{metodo} AS
SELECT
    i.clee_resuelto, i.origen, e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cve_loc, e.ageb, e.cod_postal,
    ta.tipo_asent, e.nomb_asent, e.nom_vial, e.numero_ext, e.nom_cencom,
    e.latitud, e.longitud, e.h3_cell_9,
    ep.per_ocu, ep.categoria AS categoria_tamano,
    tu.tipounieco,
    e.telefono, e.correoelec, e.www
FROM {schema}.establecimientos e
JOIN {schema}.{identidad} i ON i.id = e.id AND i.period = e.period
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE i.clee_resuelto IS NOT NULL AND i.clee_resuelto <> ''
"""


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


def crear_vistas(conn: Connection, schema: str = DEFAULT_SCHEMA) -> list[str]:
    """Crea o redefine las dos vistas de cada método.

    `estatus_<metodo>` aplica la regla de cierre; `variantes_<metodo>`
    devuelve los valores que la tabla resume en conteos.

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
        conn.execute(text(_vista_variantes_sql(metodo, schema)))
        creadas.append(f"{schema}.variantes_{metodo}")
    return creadas
