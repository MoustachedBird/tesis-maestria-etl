import polars as pl

# =============================================================================
# UMBRALES DE ENTITY RESOLUTION (Facil de calibrar)
#
# Estos parametros controlan la logica de matching entre registros de
# diferentes periodos. Ajustar con cuidado: valores mas bajos = mas matches
# (mas falsos positivos), valores mas altos = menos matches (mas falsos negativos).
# =============================================================================

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


# =============================================================================
# ESQUEMAS Y COLUMNAS
# =============================================================================

# Esquema alineado con el Processor
SILVER_SCHEMA = {
    "id": pl.Utf8,
    "clee": pl.Utf8,
    "nom_estab": pl.Utf8,
    "raz_social": pl.Utf8,
    "codigo_act": pl.Utf8,
    "nombre_act": pl.Utf8,
    "per_ocu": pl.Utf8,
    "tipo_vial": pl.Utf8,
    "nom_vial": pl.Utf8,
    "tipo_v_e_1": pl.Utf8,
    "nom_v_e_1": pl.Utf8,
    "tipo_v_e_2": pl.Utf8,
    "nom_v_e_2": pl.Utf8,
    "tipo_v_e_3": pl.Utf8,
    "nom_v_e_3": pl.Utf8,
    "numero_ext": pl.Utf8,
    "letra_ext": pl.Utf8,
    "edificio": pl.Utf8,
    "edificio_e": pl.Utf8,
    "numero_int": pl.Utf8,
    "letra_int": pl.Utf8,
    "tipo_asent": pl.Utf8,
    "nomb_asent": pl.Utf8,
    "tipocencom": pl.Utf8,
    "nom_cencom": pl.Utf8,
    "num_local": pl.Utf8,
    "cod_postal": pl.Utf8,
    "cve_ent": pl.Utf8,
    "entidad": pl.Utf8,
    "cve_mun": pl.Utf8,
    "municipio": pl.Utf8,
    "cve_loc": pl.Utf8,
    "localidad": pl.Utf8,
    "ageb": pl.Utf8,
    "manzana": pl.Utf8,
    "telefono": pl.Utf8,
    "correoelec": pl.Utf8,
    "www": pl.Utf8,
    "tipounieco": pl.Utf8,
    "latitud": pl.Float64,
    "longitud": pl.Float64,
    "fecha_alta_str": pl.Utf8,
    "period": pl.Utf8,
    "row_id": pl.Utf8,
}

# Columnas minimas necesarias para la resolucion
COLS_NEEDED_FOR_MATCHING = [
    "row_id",
    "clee",
    "nom_estab",
    "raz_social",
    "nombre_act",
    "latitud",
    "longitud",
    "period",
    "telefono",
    "correoelec",
    "www",
    "entidad",
    "municipio",
    "localidad",
    "fecha_alta_str",
    "source_paths",
    "per_ocu",
    "tipo_asent",
    "nomb_asent",
    "tipocencom",
    "nom_cencom",
    "cod_postal",
]

# Columnas de texto para sanitizar (None -> "")
TEXT_COLS_TO_SANITIZE = [
    "nom_estab",
    "raz_social",
    "nombre_act",
    "entidad",
    "municipio",
    "localidad",
    "fecha_alta_str",
    "per_ocu",
    "tipo_asent",
    "nomb_asent",
    "tipocencom",
    "nom_cencom",
    "cod_postal",
]
