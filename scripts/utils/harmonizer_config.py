import polars as pl

# --- CONFIGURACIÓN DE MEGA-REGIONES ---
MEGA_REGIONS = {
    "ZM_VALLE_MEXICO": [
        "CIUDAD_DE_MÉXICO", 
        "ESTADO_DE_MÉXICO",  # Actualizado para coincidir con la nueva normalización
        "DISTRITO_FEDERAL"   # Se mantiene por compatibilidad con datos históricos
    ],
    "LA_LAGUNA": ["COAHUILA_DE_ZARAGOZA", "DURANGO"],
    "PUEBLA_TLAXCALA": ["PUEBLA", "TLAXCALA"],
    "VALLARTA": ["JALISCO", "NAYARIT"]
}

# --- ESQUEMAS ---
# Esquema alineado con el Processor
SILVER_SCHEMA = {
    'id': pl.Utf8, 'clee': pl.Utf8, 'nom_estab': pl.Utf8, 
    'raz_social': pl.Utf8, 'codigo_act': pl.Utf8, 'nombre_act': pl.Utf8, 
    'per_ocu': pl.Utf8, 'tipo_vial': pl.Utf8, 'nom_vial': pl.Utf8, 
    'tipo_v_e_1': pl.Utf8, 'nom_v_e_1': pl.Utf8, 'tipo_v_e_2': pl.Utf8, 
    'nom_v_e_2': pl.Utf8, 'tipo_v_e_3': pl.Utf8, 'nom_v_e_3': pl.Utf8, 
    'numero_ext': pl.Utf8, 'letra_ext': pl.Utf8, 'edificio': pl.Utf8, 
    'edificio_e': pl.Utf8, 'numero_int': pl.Utf8, 'letra_int': pl.Utf8, 
    'tipo_asent': pl.Utf8, 'nomb_asent': pl.Utf8, 'tipocencom': pl.Utf8, 
    'nom_cencom': pl.Utf8, 'num_local': pl.Utf8, 'cod_postal': pl.Utf8, 
    'cve_ent': pl.Utf8, 'entidad': pl.Utf8, 'cve_mun': pl.Utf8, 
    'municipio': pl.Utf8, 'cve_loc': pl.Utf8, 'localidad': pl.Utf8, 
    'ageb': pl.Utf8, 'manzana': pl.Utf8, 
    'telefono': pl.Utf8, 'correoelec': pl.Utf8, 'www': pl.Utf8, 
    'tipounieco': pl.Utf8, 
    'latitud': pl.Float64, 'longitud': pl.Float64, 
    'fecha_alta': pl.Utf8, 'period': pl.Utf8, 'row_id': pl.Utf8
}

# Columnas mínimas necesarias para la resolución
COLS_NEEDED_FOR_MATCHING = [
    'row_id', 'clee', 'nom_estab', 'raz_social', 'nombre_act', 
    'latitud', 'longitud', 'period', 'telefono', 'correoelec', 'www',
    'entidad', 'municipio'
]

# Columnas de texto para sanitizar (None -> "")
TEXT_COLS_TO_SANITIZE = ['nom_estab', 'raz_social', 'nombre_act', 'clee', 'telefono', 'correoelec', 'www']