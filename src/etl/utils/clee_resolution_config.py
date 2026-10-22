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
# escribe su propia tabla (silver.clee_h3) para compararse lado a lado con
# el baseline BallTree (silver.clee_radio).
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

HOY NINGUN codigo la consume: es el habilitador precomputado del
particionado de KB por celda gruesa (Fase 4 opcional, todo.md; Fase 5 lo
reutilizaria como llave de particion espacial en joins). Como funcionara la
poda: en vez de partir la KB en slices arbitrarios (que obligan a evaluar
TODOS los targets contra TODAS las particiones), la KB se particionaria
por h3_cell_5; dado que SPATIAL_RADIUS_M (500m) << arista res-5 (~8.5km),
todo candidato de un target vive en su celda res-5 o en sus vecinas
inmediatas (grid_disk k=1), asi que las particiones fuera de ese disco se
saltan sin perder matches (menos filas escaneadas Y menos RAM pico). La
derivacion parent(h3_cell_9) es la que garantiza que ninguna celda de
blocking res-9 quede repartida entre dos particiones res-5 (la celda
res-5 directa del punto filtraria ~6.5% de filas a otra particion,
medido). Se precomputa porque su costo marginal es ~0 (misma pasada del
UPDATE de backfill, sin indice, columna derivada de h3_cell_9 — si la
optimizacion se descarta, la columna se puede borrar sin perder nada).
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
# COLUMNAS PARA RESOLUCION (leidas desde Silver)
# =============================================================================

COLS_NEEDED_FOR_MATCHING = [
    "id",
    "clee",
    "clee_resuelto",
    "origen",
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
"""Columnas que se leen de Silver para la resolucion.
`nombre_act` se obtiene via JOIN con denue_actividades_economicas."""

RESOLVED_COLUMN = "clee_resuelto"
"""Nombre de la columna de identidad resuelta, en la base y en los DataFrames.

Igual para todo metodo: lo que distingue a radio de h3 es la TABLA donde
viven sus filas (`silver.clee_radio` / `silver.clee_h3`), no el nombre de la
columna. Asi el motor de matching no necesita saber que metodo lo invoco.
"""

ORIGEN_COLUMN = "origen"
"""Columna que dice como se asigno la identidad (valores de `OrigenClee`)."""


def silver_read_query(clee_table: str | None) -> str:
    """SQL para leer un corte de Silver con su identidad ya resuelta.

    Resuelve los dominios (FKs enteros) a sus labels con el MISMO nombre de
    columna de siempre, asi que el pipeline de matching
    (COLS_NEEDED_FOR_MATCHING, sanitizacion, KB) no cambia. Catalogos de
    decenas de filas: el costo del JOIN es ~0.

    La identidad llega por LEFT JOIN contra la tabla del metodo y no como
    columna de `establecimientos`: la tabla de hechos ya no la guarda (ver
    `docs/separacion_ingest_clee.md`). El JOIN incluye `period` para que sea
    partition-wise, y es LEFT porque un corte sin resolver todavia no tiene
    filas ahi.

    `clee_table=None` lee sin identidad: las dos columnas salen en NULL y no
    se paga el JOIN. Lo usa la evaluacion con CLEEs enmascarados, que
    resuelve en memoria contra su propia verdad y nunca lee la resolucion
    materializada.

    Args:
        clee_table: tabla de resolucion del metodo activo, sin calificar
            (`clee_radio` o `clee_h3`), o None para no unir.

    Returns:
        str: SELECT sin filtro; el llamador agrega su `WHERE e.period = ...`.
    """
    if clee_table is None:
        identidad = (
            f"CAST(NULL AS VARCHAR) AS {RESOLVED_COLUMN}, "
            f"CAST(NULL AS VARCHAR) AS {ORIGEN_COLUMN}"
        )
        join = ""
    else:
        identidad = f"r.{RESOLVED_COLUMN} AS {RESOLVED_COLUMN}, r.{ORIGEN_COLUMN} AS {ORIGEN_COLUMN}"
        join = f"\n    LEFT JOIN {clee_table} r ON r.id = e.id AND r.period = e.period"

    return f"""
    SELECT
        e.id,
        e.clee,
        {identidad},
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
        ep.per_ocu AS per_ocu,
        ta.tipo_asent AS tipo_asent,
        e.nomb_asent,
        e.nom_cencom,
        e.cod_postal
    FROM establecimientos e{join}
    LEFT JOIN denue_actividades_economicas a ON e.codigo_act = a.codigo_act
    LEFT JOIN denue_estratos_personal ep ON e.per_ocu_id = ep.id
    LEFT JOIN denue_tipos_asentamiento ta ON e.tipo_asent_id = ta.id
"""


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
