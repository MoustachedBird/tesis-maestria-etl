"""
Configuracion y umbrales de la resolucion CLEE (entity resolution).

Reglas de este archivo (ver todo.md, "Reglas de trabajo"):
  - Variables de CALIBRACION y recursos se leen del entorno (.env) con
    default aqui: se ajustan sin tocar codigo. Cada una esta documentada
    tambien en .env.example.
  - CONSTANTES estructurales (acopladas al esquema o a datos ya
    materializados en Silver) NO son variables de entorno: cambiarlas
    exige migracion/recomputo; su docstring explica por que.

Sobre los umbrales de matching: valores mas bajos = mas matches (mas
falsos positivos); mas altos = menos matches (mas falsos negativos).
"""

import os

# =============================================================================
# RADIO ESPACIAL (calibracion, .env)
# =============================================================================

SPATIAL_RADIUS_M: int = int(os.environ.get("SPATIAL_RADIUS_M", "500"))
"""Radio maximo (metros) para buscar candidatos en la Knowledge Base.

Aplica a AMBOS generadores de candidatos (BallTree del metodo radio y
blocking H3): solo negocios dentro de este radio se evaluan en la cascada
de matching. 500m cubre mudanzas dentro del mismo barrio/colonia.

OJO: acoplado a H3_GRID_DISK_K -- si se sube por encima de la cobertura
del blocking H3, el guard al final del archivo lo detiene (habria que
subir H3_GRID_DISK_K a la par y re-verificar paridad).
"""

# =============================================================================
# CELDAS H3 (constantes estructurales -- NO .env)
#
# El metodo H3 NO sustituye al de radio: es un metodo COMPLEMENTARIO que
# llena su propia columna (clee_propagado_h3) para compararse lado a lado
# con el baseline BallTree (clee_propagado_radio). Ver todo.md Fase 4.
# =============================================================================

H3_BLOCKING_RES = 9
"""Resolucion H3 de blocking (res 9 ~ arista 174m): celda de `h3_cell_9`.

Es la celda con la que el metodo H3 genera candidatos (grid_disk).
Constante estructural, NO variable de entorno: esta materializada en la
columna `h3_cell_9` de Silver (~77M filas). Cambiarla exige migracion +
re-backfill con --force.
"""

H3_PARTITION_RES = 5
"""Resolucion H3 de particionado grueso (res 5 ~ arista 8.5km): celda de
`h3_cell_5`, derivada como parent(h3_cell_9).

HOY NINGUN codigo la consume: esta reservada para el particionado de KB
por celda gruesa (Fase 4 opcional, todo.md) que podaria particiones de KB
sin solape espacial con el target. Se precomputa porque su costo marginal
es ~0 (misma pasada del UPDATE de backfill, sin indice, columna derivada).
Constante estructural, NO .env.
"""

H3_GRID_DISK_K = 3
"""Anillos de grid_disk alrededor de la celda del target (candidatos H3).

k=3 en res 9 (arista ~174m) cubre >=500m en el peor caso (target en el
borde de su celda), garantizando que ningun candidato dentro de
SPATIAL_RADIUS_M se pierda antes del post-filtro haversine exacto
(paridad verificada contra BallTree: 0 diferencias en el test sintetico).

Acoplada a SPATIAL_RADIUS_M y H3_BLOCKING_RES -- por eso NO es .env: si
cambia el radio hay que recalcular k y re-verificar paridad. El guard al
final del archivo protege la cobertura.
"""

# =============================================================================
# UMBRALES DE LA CASCADA DE MATCHING (calibracion, .env)
# =============================================================================

ACTIVITY_GATE_THRESH: int = int(os.environ.get("ACTIVITY_GATE_THRESH", "60"))
"""GATE OBLIGATORIO: umbral minimo de similitud fuzzy en nombre_act
(actividad SCIAN). Si no se supera, el candidato se descarta sin evaluar
otros campos. 60 = permisivo con variaciones de redaccion.
"""

BUSINESS_NAME_MATCH_THRESH: int = int(os.environ.get("BUSINESS_NAME_MATCH_THRESH", "75"))
"""Umbral de fuzzy (token_sort_ratio) en nom_estab para MATCH DIRECTO por
nombre comercial: si se supera, se declara match sin otros campos.
"""

LEGAL_NAME_MATCH_THRESH: int = int(os.environ.get("LEGAL_NAME_MATCH_THRESH", "80"))
"""Umbral de fuzzy (token_sort_ratio) en raz_social para MATCH DIRECTO por
razon social. Mas estricto que el nombre comercial porque razones sociales
pueden repetirse entre negocios distintos de un mismo dueno.
"""

MIN_COMBINED_SIGNALS: int = int(os.environ.get("MIN_COMBINED_SIGNALS", "2"))
"""MATCH COMBINADO: numero de senales debiles coincidentes para declarar
match cuando ningun campo individual supera su umbral directo.

Senales que se cuentan: nombre comercial parcial (> PARTIAL_NAME_THRESH),
telefono exacto (7+ digitos), email exacto (5+ chars), web exacta
(5+ chars), codigo postal igual.
"""

PARTIAL_NAME_THRESH: int = int(os.environ.get("PARTIAL_NAME_THRESH", "60"))
"""Umbral de fuzzy en nom_estab para contar como "senal parcial" en el
match combinado. Mas bajo que BUSINESS_NAME_MATCH_THRESH porque aqui no
basta por si sola.
"""

# =============================================================================
# LONGITUDES MINIMAS PARA CONTACTO VALIDO (constantes de validez de dato)
#
# No son calibracion de matching sino la definicion de cuando un campo de
# contacto es utilizable como senal; por eso viven aqui y no en .env.
# =============================================================================

MIN_TEL_LENGTH = 7
"""Largo minimo del telefono limpio (solo digitos) para contar como senal:
menos de 7 digitos no identifica una linea telefonica real en MX."""

MIN_EMAIL_LENGTH = 5
"""Largo minimo del email normalizado para contar como senal (descarta
valores basura tipo "-", "N/A")."""

MIN_WEB_LENGTH = 5
"""Largo minimo de la web normalizada para contar como senal (descarta
valores basura)."""

# =============================================================================
# PARALELISMO Y RAM (recursos, .env)
# =============================================================================

MATCH_WORKERS: int | None = int(os.environ.get("MATCH_WORKERS", "8"))
"""Numero de workers de multiprocessing para el loop de matching en la
propagacion. 1 desactiva el paralelismo (modo debug secuencial).
Regla practica: cores fisicos - 3 (ver .env.example)."""

MATCH_PARALLEL_THRESHOLD: int = int(os.environ.get("MATCH_PARALLEL_THRESHOLD", "50000"))
"""Minimo de filas pendientes para activar multiprocessing: por debajo,
el overhead de fork no compensa y se usa el loop secuencial."""

KB_MAX_ROWS_PER_PARTITION: int = int(os.environ.get("KB_MAX_ROWS_PER_PARTITION", "1500000"))
"""Maximo de filas por particion de la KB de referencia (knob principal de
RAM del matching). Si la KB excede este umbral, se divide en particiones
procesadas secuencialmente (sin perder matches). Cada worker puede
terminar copiando (COW) las paginas de la particion compartida: menor =
menos RAM pico, mayor = menos particiones (mas rapido)."""

# =============================================================================
# GUARD: cobertura del blocking H3 vs radio espacial
# =============================================================================

_H3_RING_COVERAGE_M = 174
"""Cobertura aproximada (metros) que aporta cada anillo de grid_disk en
res 9 (arista promedio de la celda). Solo se usa en el guard de abajo."""

if SPATIAL_RADIUS_M > H3_GRID_DISK_K * _H3_RING_COVERAGE_M:
    raise ValueError(
        f"SPATIAL_RADIUS_M={SPATIAL_RADIUS_M}m excede la cobertura garantizada "
        f"del blocking H3 (H3_GRID_DISK_K={H3_GRID_DISK_K} anillos x "
        f"~{_H3_RING_COVERAGE_M}m = {H3_GRID_DISK_K * _H3_RING_COVERAGE_M}m). "
        "Sube H3_GRID_DISK_K en clee_resolution_config.py y re-verifica la "
        "paridad H3 vs BallTree antes de correr."
    )

# =============================================================================
# COLUMNAS PARA RESOLUCION (leidas desde Silver DuckDB)
# =============================================================================

COLS_NEEDED_FOR_MATCHING = [
    "id",
    "clee",
    "clee_propagado_radio",
    "clee_propagado_h3",
    "h3_cell_9",
    "h3_cell_5",
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
"""Columnas que se leen del Silver DuckDB para la resolucion.
`nombre_act` se obtiene via JOIN con denue_actividades_economicas."""

SILVER_READ_QUERY = """
    SELECT
        e.id,
        e.clee,
        e.clee_propagado_radio,
        e.clee_propagado_h3,
        e.h3_cell_9,
        e.h3_cell_5,
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
"""SQL para leer datos del Silver DuckDB con JOINs a catalogos.
Obtiene nombre_act via JOIN; cast de enums a VARCHAR para Polars."""

TEXT_COLS_TO_SANITIZE = [
    "nom_estab",
    "raz_social",
    "nombre_act",
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
"""Columnas de texto a sanitizar (None -> "") antes del matching.

IMPORTANTE: telefono, correoelec y www DEBEN estar aqui para que
clean_contact_columns() genere tel_clean/email_clean/www_clean con ""
en vez de None. Si faltan, columnas dispersas (especialmente www) causan
errores de inferencia de esquema al reconstruir DataFrames desde dicts."""
