"""
Constants and configurations for DENUE data normalization.
"""

# MASTER SCHEMA: Todas las columnas que deben existir en el Warehouse final.
MASTER_COLUMNS = [
    'id', 'clee', 'nom_estab', 'raz_social', 'codigo_act', 'nombre_act', 'per_ocu',
    'tipo_vial', 'nom_vial', 'tipo_v_e_1', 'nom_v_e_1', 'tipo_v_e_2', 
    'nom_v_e_2', 'tipo_v_e_3', 'nom_v_e_3', 'numero_ext', 'letra_ext', 
    'edificio', 'edificio_e', 'numero_int', 'letra_int', 'tipo_asent', 
    'nomb_asent', 'tipocencom', 'nom_cencom', 'num_local', 'cod_postal', 
    'cve_ent', 'entidad', 'cve_mun', 'municipio', 'cve_loc', 'localidad', 
    'ageb', 'manzana', 'telefono', 'correoelec', 'www', 'tipounieco', 
    'latitud', 'longitud', 'fecha_alta'
]

# Columnas que realmente se guardarán en la tabla 'establecimientos'
DB_FACT_COLUMNS = [
    'id',  # <--- Cambiado de 'row_id' a 'id' para coincidir con el procesador
    'clee', 'nom_estab', 'raz_social', 'codigo_act', 
    'id_tipo_vial',      # FK a denue_tipos_vialidad
    'id_tipo_asent',     # FK a denue_tipos_asentamiento
    'id_tipo_unidad',    # FK a denue_tipos_unidad
    'id_estrato',        # FK a denue_estratos_personal
    'nom_vial', 'tipo_v_e_1', 'nom_v_e_1', 'tipo_v_e_2', 'nom_v_e_2', 
    'tipo_v_e_3', 'nom_v_e_3', 'numero_ext', 'letra_ext', 'edificio', 
    'edificio_e', 'numero_int', 'letra_int', 'nomb_asent', 'nom_cencom', 
    'num_local', 'cod_postal', 'cve_ent', 'cve_mun', 'cve_loc', 'ageb', 
    'manzana', 'telefono', 'correoelec', 'www', 'latitud', 'longitud', 
    'fecha_alta', 'period'
]

# REQUIRED COLUMNS: Columnas indispensables para que el registro sea procesado.
REQUIRED_COLUMNS = ['id', 'codigo_act', 'entidad', 'municipio']

# COLUMN ALIASES: Mapeo de nombres variables del INEGI a nombres estandarizados.
COLUMN_ALIASES = {
    # Entidad
    'nom_ent': 'entidad',
    'entidad_federativa': 'entidad',
    'ent': 'entidad',
    'desc_ent': 'entidad',
    'nombre_ent': 'entidad',
    'nombre_entidad': 'entidad',
    'cve_ent': 'entidad', 
    
    # Municipio
    'nom_mun': 'municipio',
    'municipio_del_domicilio': 'municipio',
    'mun': 'municipio',
    'desc_mun': 'municipio',
    'nombre_mun': 'municipio',
    'nombre_municipio': 'municipio',
    'municipio_delegacion': 'municipio',
    'municipio_o_delegacion': 'municipio',
    'cve_mun': 'municipio',

    # Localidad
    'nom_loc': 'localidad',
    'loc': 'localidad',
    
    # CP
    'cod_post': 'cod_postal',
    'cp': 'cod_postal',
    'codigo_postal': 'cod_postal',
    
    # Fecha
    'fecha_alta_base': 'fecha_alta'
}

# ENTITY VALUE NORMALIZATION: Corrección de nombres de estados para uniformidad.
ENTITY_VALUE_NORMALIZATION = {
    "DISTRITO FEDERAL": "CIUDAD DE MÉXICO",
    "DISTRITO_FEDERAL": "CIUDAD DE MÉXICO",
    "DF": "CIUDAD DE MÉXICO",
    "COAHUILA DE ZARAGOZA": "COAHUILA DE ZARAGOZA", 
    "MICHOACÁN DE OCAMPO": "MICHOACÁN DE OCAMPO",
    "VERACRUZ DE IGNACIO DE LA LLAVE": "VERACRUZ DE IGNACIO DE LA LLAVE",
    "MÉXICO": "ESTADO DE MÉXICO",
    "MEXICO": "ESTADO DE MÉXICO",
    "EDO. DE MÉXICO": "ESTADO DE MÉXICO",
}