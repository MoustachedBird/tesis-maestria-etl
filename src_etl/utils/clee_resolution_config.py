# =============================================================================
# UMBRALES DE ENTITY RESOLUTION (Facil de calibrar)
#
# Estos parametros controlan la logica de matching entre registros de
# diferentes periodos. Ajustar con cuidado: valores mas bajos = mas matches
# (mas falsos positivos), valores mas altos = menos matches (mas falsos negativos).
# =============================================================================

import os

# --- RADIO ESPACIAL (pre-filtro BallTree) ---
# Define el radio maximo (metros) para buscar candidatos en la Knowledge Base.
# Solo negocios dentro de este radio son evaluados por la cascada de matching.
# 500m cubre mudanzas dentro del mismo barrio/colonia.
SPATIAL_RADIUS_M = 500

# --- GATE OBLIGATORIO: ACTIVIDAD ECONOMICA (SCIAN) ---
# Umbral minimo de similitud fuzzy en nombre_act (actividad SCIAN).
# Si no se supera, el candidato se descarta sin evaluar otros campos.
# Valor recomendado: 60 (permisivo con variaciones de redaccion).
ACTIVITY_GATE_THRESH = 60

# --- MATCH DIRECTO POR NOMBRE COMERCIAL ---
# Umbral de fuzzy (token_sort_ratio) en nom_estab para match directo.
# Si se supera, se declara match sin necesidad de otros campos.
BUSINESS_NAME_MATCH_THRESH = 75

# --- MATCH DIRECTO POR RAZON SOCIAL ---
# Umbral de fuzzy (token_sort_ratio) en raz_social para match directo.
# Mas estricto que nombre comercial porque razones sociales pueden repetirse
# entre negocios distintos de un mismo dueno.
LEGAL_NAME_MATCH_THRESH = 80

# --- MATCH COMBINADO (senales debiles acumuladas) ---
# Si ningun campo individual supera su umbral de match directo, se cuentan
# "senales" coincidentes. Si se alcanzan MIN_COMBINED_SIGNALS senales,
# se declara match.
#
# Senales que se cuentan:
#   - Nombre comercial parcial (fuzzy > PARTIAL_NAME_THRESH)
#   - Telefono exacto (7+ digitos)
#   - Email exacto (5+ chars)
#   - Web exacto (5+ chars)
#   - Codigo postal igual
MIN_COMBINED_SIGNALS = 2

# Umbral de fuzzy en nom_estab para contar como "senal parcial" en match combinado.
# Mas bajo que BUSINESS_NAME_MATCH_THRESH porque aqui no basta solo.
PARTIAL_NAME_THRESH = 60

# --- LONGITUDES MINIMAS PARA CONTACTO VALIDO ---
# Largo minimo de telefono limpio para considerar como senal valida
MIN_TEL_LENGTH = 7
# Largo minimo de email para considerar como senal valida
MIN_EMAIL_LENGTH = 5
# Largo minimo de web para considerar como senal valida
MIN_WEB_LENGTH = 5

# --- PARALELISMO (multiprocessing) ---
# Numero de workers para el loop de matching en propagate_clees().
# None = auto (cpu_count - 3, minimo 1).
# Poner 1 para desactivar paralelismo (modo debug/secuencial).
MATCH_WORKERS: int | None = int(os.environ.get("MATCH_WORKERS", "8"))

# Tamano minimo de filas pendientes para activar multiprocessing.
# Si hay menos filas que esto, el overhead de fork no vale la pena.
MATCH_PARALLEL_THRESHOLD = 50_000

# --- PARTICIONAMIENTO DE KB (control de RAM) ---
# Numero maximo de filas por particion de la KB de referencia.
# Si la KB excede este umbral, se divide en particiones que se procesan
# secuencialmente contra el target, evitando OOM en maquinas con RAM limitada.
# Cada particion construye su propio BallTree y materializa su propia copia
# de las listas _shared_kb_*. No se pierde ningun match potencial.
# Default: 1,500,000 filas (~0.5-0.8 GB de KB materializada por particion).
# Configurable via variable de entorno KB_MAX_ROWS_PER_PARTITION.
KB_MAX_ROWS_PER_PARTITION: int = int(os.environ.get("KB_MAX_ROWS_PER_PARTITION", "1500000"))


# =============================================================================
# COLUMNAS PARA RESOLUCION (leidas desde Silver DuckDB)
# =============================================================================

# Columnas que se leen del Silver DuckDB para la resolucion.
# nombre_act se obtiene via JOIN con denue_actividades_economicas.
COLS_NEEDED_FOR_MATCHING = [
    "id",
    "clee",
    "clee_master",
    "nom_estab",
    "raz_social",
    "codigo_act",
    "nombre_act",  # via JOIN con catalogo actividades
    "latitud",
    "longitud",
    "period",
    "telefono",
    "correoelec",
    "www",
    "fecha_alta_str",
    "per_ocu",
    "tipo_asent",
    "nomb_asent",
    "nom_cencom",
    "cod_postal",
]

# SQL query para leer datos del Silver DuckDB con JOINs a catalogos.
# Obtiene nombre_act via JOIN; cast de enums a VARCHAR para Polars.
SILVER_READ_QUERY = """
    SELECT
        e.id,
        e.clee,
        e.clee_master,
        e.nom_estab,
        e.raz_social,
        e.codigo_act,
        COALESCE(a.nombre_act, '') AS nombre_act,
        e.latitud,
        e.longitud,
        CAST(e.period AS VARCHAR) AS period,
        e.telefono,
        e.correoelec,
        e.www,
        e.fecha_alta_str,
        CAST(e.per_ocu AS VARCHAR) AS per_ocu,
        CAST(e.tipo_asent AS VARCHAR) AS tipo_asent,
        e.nomb_asent,
        e.nom_cencom,
        e.cod_postal
    FROM establecimientos e
    LEFT JOIN denue_actividades_economicas a ON e.codigo_act = a.codigo_act
"""

# Columnas de texto para sanitizar (None -> "")
TEXT_COLS_TO_SANITIZE = [
    "nom_estab",
    "raz_social",
    "nombre_act",
    "fecha_alta_str",
    "per_ocu",
    "tipo_asent",
    "nomb_asent",
    "nom_cencom",
    "cod_postal",
]
