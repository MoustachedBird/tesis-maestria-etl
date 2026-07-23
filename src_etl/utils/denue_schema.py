"""
Constants and configurations for DENUE data normalization.
"""

# MASTER SCHEMA: Todas las columnas que deben existir en el Warehouse final.
MASTER_COLUMNS = [
    "id",
    "clee",
    "nom_estab",
    "raz_social",
    "codigo_act",
    "nombre_act",
    "per_ocu",
    "tipo_vial",
    "nom_vial",
    "tipo_v_e_1",
    "nom_v_e_1",
    "tipo_v_e_2",
    "nom_v_e_2",
    "tipo_v_e_3",
    "nom_v_e_3",
    "numero_ext",
    "letra_ext",
    "edificio",
    "edificio_e",
    "numero_int",
    "letra_int",
    "tipo_asent",
    "nomb_asent",
    "tipocencom",
    "nom_cencom",
    "num_local",
    "cod_postal",
    "cve_ent",
    "entidad",
    "cve_mun",
    "municipio",
    "cve_loc",
    "localidad",
    "ageb",
    "manzana",
    "telefono",
    "correoelec",
    "www",
    "tipounieco",
    "latitud",
    "longitud",
    "fecha_alta_str",
    "period",
]

# Columnas que realmente se guardarán en la tabla 'establecimientos'
DB_FACT_COLUMNS = [
    "id",  # <--- Cambiado de 'row_id' a 'id' para coincidir con el procesador
    "clee",
    "clee_propagado_radio",
    "nom_estab",
    "raz_social",
    "codigo_act",
    "tipo_vial",  # dominio: catalogo denue_tipos_vialidad
    "tipo_asent",  # dominio: catalogo denue_tipos_asentamiento
    "tipounieco",  # dominio: catalogo denue_tipos_unidad_economica
    "per_ocu",  # dominio: catalogo denue_estratos_personal
    "nom_vial",
    "tipo_v_e_1",
    "nom_v_e_1",
    "tipo_v_e_2",
    "nom_v_e_2",
    "tipo_v_e_3",
    "nom_v_e_3",
    "numero_ext",
    "letra_ext",
    "edificio",
    "edificio_e",
    "numero_int",
    "letra_int",
    "nomb_asent",
    "nom_cencom",
    "num_local",
    "cod_postal",
    "cve_ent",
    "cve_mun",
    "cve_loc",
    "ageb",
    "manzana",
    "telefono",
    "correoelec",
    "www",
    "latitud",
    "longitud",
    "fecha_alta_str",
    "fecha_alta_date",
    "period",
]

# REQUIRED COLUMNS: Columnas indispensables para que el registro sea procesado.
REQUIRED_COLUMNS = ["codigo_act", "entidad", "municipio"]

# COLUMN ALIASES: Mapeo de nombres variables del INEGI a nombres estandarizados.
COLUMN_ALIASES = {
    # Entidad
    "nom_ent": "entidad",
    "entidad_federativa": "entidad",
    "ent": "entidad",
    "desc_ent": "entidad",
    "nombre_ent": "entidad",
    "nombre_entidad": "entidad",
    "cve_ent": "entidad",
    "clave entidad": "cve_ent",  # Histórico 2015 (código numérico)
    # Municipio
    "nom_mun": "municipio",
    "municipio_del_domicilio": "municipio",
    "mun": "municipio",
    "desc_mun": "municipio",
    "nombre_mun": "municipio",
    "nombre_municipio": "municipio",
    "municipio_delegacion": "municipio",
    "municipio_o_delegacion": "municipio",
    "cve_mun": "municipio",
    "clave municipio": "cve_mun",  # Histórico 2015 (código numérico)
    # Localidad
    "nom_loc": "localidad",
    "loc": "localidad",
    "clave localidad": "cve_loc",  # Histórico 2015 (código numérico)
    "cve_loc": "localidad",
    # Actividad Económica (SCIAN)
    "código de la clase de actividad scian": "codigo_act",  # Histórico 2015
    "nombre de clase de la actividad": "nombre_act",  # Histórico 2015
    # Establecimiento y Razón Social
    "nombre de la unidad económica": "nom_estab",  # Histórico 2015
    "razón social": "raz_social",  # Histórico 2015
    # Estrato y Ocupación
    "descripcion estrato personal ocupado": "per_ocu",  # Histórico 2015
    # Vialidad y Domicilio
    "tipo de vialidad": "tipo_vial",  # Histórico 2015
    "nombre de la vialidad": "nom_vial",  # Histórico 2015
    "tipo de asentamiento humano": "tipo_asent",  # Histórico 2015
    "nombre de asentamiento humano": "nomb_asent",  # Histórico 2015
    "área geoestadística básica": "ageb",  # Histórico 2015
    "número exterior o kilómetro": "numero_ext",  # Histórico 2015
    "número interior": "numero_int",  # Histórico 2015
    "tipo centro comercial": "tipocencom",  # Histórico 2015
    "número de local": "num_local",  # Histórico 2015
    # CP
    "cod_post": "cod_postal",
    "cp": "cod_postal",
    "codigo_postal": "cod_postal",
    "código postal": "cod_postal",  # Histórico 2015
    # Fecha e Integración
    "fecha_alta": "fecha_alta_str",  # Renombrar columna CSV estándar
    "fecha_alta_base": "fecha_alta_str",
    "fecha de incorporación al denue": "fecha_alta_str",  # Histórico 2015
    # Vialidades cruzadas (entre vialidad) - Histórico 2015
    "tipo de entre vialidad 1": "tipo_v_e_1",
    "nombre de entre vialidad 1": "nom_v_e_1",
    "tipo de entre vialidad 2": "tipo_v_e_2",
    "nombre de entre vialidad 2": "nom_v_e_2",
    "tipo de entre vialidad 3": "tipo_v_e_3",
    "nombre de entre vialidad 3": "nom_v_e_3",
    # Edificio y letras - Histórico 2015
    "letra exterior": "letra_ext",
    "edificio piso": "edificio_e",
    "letra interior": "letra_int",
    # Centro comercial - Histórico 2015
    "corredor industrial, centro comercial o mercado público": "nom_cencom",
    # Contacto - Histórico 2015
    "número de teléfono": "telefono",
    "correo electrónico": "correoelec",
    "sitio en internet": "www",
    # Tipo establecimiento - Histórico 2015
    "tipo de establecimiento": "tipounieco",
    # Nombre de entidad federativa - Histórico 2015
    "entidad federativa": "entidad",
}

# ENTITY VALUE NORMALIZATION: Corrección de nombres de estados para uniformidad.
# Mapa {valor observado: valor canonico} que se aplica a la columna
# `entidad` en processor._clean_raw_data (NO esta indexado por columna:
# ojo al recorrerlo, sus claves son valores del dato).
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

DOMAIN_VALUE_ALIASES: dict[str, dict[str, str]] = {
    "tipo_asent": {
        # INEGI usa "OTRO" desde el corte 2026-05 para lo que el catalogo
        # historico llama "OTRA (ESPECIFICAR)": mismo concepto, etiqueta
        # distinta. Se unifica al canonico en vez de duplicar la fila del
        # catalogo (evita dos ids para un solo dominio).
        "OTRO": "OTRA (ESPECIFICAR)",
    },
}
"""Sinonimos de valores de dominio por columna: {columna: {alias: canonico}}.

Aplicado en la limpieza (`processor._clean_raw_data`) ANTES de la
validacion contra la BD, para que un cambio de etiqueta de INEGI entre
cortes no exija una fila nueva de catalogo ni un id nuevo."""

DOMAIN_ID_COLUMNS = {
    "tipo_vial": ("tipo_vial_id", "denue_tipos_vialidad", "tipo_vial"),
    "tipo_asent": ("tipo_asent_id", "denue_tipos_asentamiento", "tipo_asent"),
    "tipounieco": ("tipounieco_id", "denue_tipos_unidad_economica", "tipounieco"),
    "per_ocu": ("per_ocu_id", "denue_estratos_personal", "per_ocu"),
}
"""Mapeo de columna-label del CSV -> (columna id en establecimientos,
tabla de catalogo, columna label en el catalogo).

Los CSV de INEGI traen labels; la tabla de hechos guarda FKs enteros
REALES a los catalogos. El writer usa este mapeo para el JOIN de
conversion label -> id en el INSERT, y el script de reorganizacion lo usa
para reconstruir la tabla historica."""
