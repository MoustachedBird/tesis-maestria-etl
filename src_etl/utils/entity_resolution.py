import polars as pl

from src_etl.utils.matching import calculate_match_score, find_spatial_neighbors, generate_temp_clee


def get_region_prefix(state_list: list[str]) -> str:
    """Genera un prefijo corto unico para namespacing de IDs temporales."""
    if len(state_list) > 1:
        combined = "".join(sorted([s[:3] for s in state_list]))
        return f"REG_{abs(hash(combined)) % 10000:04d}"
    else:
        state = state_list[0]
        clean_state = state.replace(" ", "").upper()
        return f"{clean_state[:3]}_{abs(hash(state)) % 1000:03d}"


def initialize_knowledge_base(
    df_base: pl.DataFrame, region_prefix: str
) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Crea la KB inicial con historial completo.

    Cada registro del periodo base se convierte en una entidad de la KB.
    Los registros con CLEE oficial lo usan como master_clee; los demas
    reciben un CLEE temporal generado.

    Returns:
        Tupla (mapping_df, kb_df) donde mapping_df tiene [row_id, master_clee]
        y kb_df es la Knowledge Base agregada por master_clee.
    """
    # Aseguramos homogeneidad de tipos string y limpieza de nulos
    df_resolved = df_base.with_columns(
        [
            pl.col("entidad").cast(pl.Utf8).fill_null(""),
            pl.col("municipio").cast(pl.Utf8).fill_null(""),
            pl.col("localidad").cast(pl.Utf8).fill_null(""),
            pl.col("fecha_alta_str").cast(pl.Utf8).fill_null(""),
            pl.col("source_paths").cast(pl.Utf8).fill_null(""),
            pl.col("per_ocu").cast(pl.Utf8).fill_null(""),
            pl.col("tipo_asent").cast(pl.Utf8).fill_null(""),
            pl.col("nomb_asent").cast(pl.Utf8).fill_null(""),
            pl.col("tipocencom").cast(pl.Utf8).fill_null(""),
            pl.col("nom_cencom").cast(pl.Utf8).fill_null(""),
            pl.col("cod_postal").cast(pl.Utf8).fill_null(""),
        ]
    ).with_columns(
        pl.when((pl.col("clee").is_not_null()) & (pl.col("clee") != ""))
        .then(pl.col("clee"))
        .otherwise(
            pl.col("row_id").map_elements(
                lambda x: f"NEW_{region_prefix}-{generate_temp_clee()}", return_dtype=pl.Utf8
            )
        )
        .alias("master_clee")
    )

    kb = df_resolved.group_by("master_clee").agg(
        [
            pl.col("latitud").first(),
            pl.col("longitud").first(),
            pl.col("period").alias("periods"),
            pl.col("nom_estab").alias("names"),
            pl.col("raz_social").alias("razones"),
            pl.col("nombre_act").alias("acts"),
            pl.col("entidad").alias("entidades"),
            pl.col("municipio").alias("municipios"),
            pl.col("localidad").alias("localidades"),
            pl.col("fecha_alta_str").alias("fechas_alta"),
            pl.col("source_paths").alias("source_paths"),
            pl.col("per_ocu").alias("estratos_personal"),
            pl.col("tipo_asent").alias("tipos_asentamiento"),
            pl.col("nomb_asent").alias("asentamientos"),
            pl.col("tipocencom").alias("tipos_centro_comercial"),
            pl.col("nom_cencom").alias("centros_comerciales"),
            pl.col("cod_postal").alias("codigos_postales"),
            pl.col("tel_clean").fill_null("").alias("tels"),
            pl.col("email_clean").fill_null("").alias("emails"),
            pl.col("www_clean").fill_null("").alias("wwws"),
        ]
    )

    return df_resolved.select(["row_id", "master_clee"]), kb


def resolve_period_logic(
    df_target: pl.DataFrame,
    kb: pl.DataFrame,
    region_prefix: str,
    spatial_tol: int,
) -> pl.DataFrame:
    """
    Nucleo algoritmico de resolucion con cascada logica.

    Para cada registro del periodo objetivo:
    1. Si tiene CLEE oficial -> match determinista
    2. Si no tiene CLEE -> buscar vecinos espaciales en KB (BallTree)
       y evaluar cascada: gate SCIAN -> razon social -> nombre -> combinado

    Los umbrales de matching se importan desde clee_resolution_config.py
    y se aplican dentro de calculate_match_score().

    Args:
        df_target: DataFrame del periodo a resolver
        kb: Knowledge Base acumulada
        region_prefix: Prefijo para generar CLEEs temporales
        spatial_tol: Radio de busqueda espacial en metros

    Returns:
        DataFrame con todos los registros del periodo + columna master_clee
    """

    # A. Determinista: Enlace directo por CLEE oficial
    df_with_clee = df_target.filter((pl.col("clee").is_not_null()) & (pl.col("clee") != ""))
    df_res_A = df_with_clee.with_columns(pl.col("clee").alias("master_clee"))

    # B. Probabilistico: Negocios sin CLEE o nuevos registros
    df_missing = df_target.filter((pl.col("clee").is_null()) | (pl.col("clee") == ""))

    new_links = []

    if df_missing.height > 0 and kb.height > 0:
        # 1. Filtro Espacial Inicial (Radio de tolerancia en metros)
        indices, _ = find_spatial_neighbors(kb, df_missing, spatial_tol)

        kb_ids = kb["master_clee"].to_list()

        # Pre-cargamos las listas historicas de la KB para acceso rapido
        kb_lists = {
            "names": kb["names"].to_list(),
            "razones": kb["razones"].to_list(),
            "acts": kb["acts"].to_list(),
            "tels": kb["tels"].to_list(),
            "emails": kb["emails"].to_list(),
            "wwws": kb["wwws"].to_list(),
            "cps": kb["codigos_postales"].to_list(),
            "plazas": kb["centros_comerciales"].to_list(),
        }

        target_rows = df_missing.to_dicts()

        for idx, row in enumerate(target_rows):
            neighbors = indices[idx]
            best_clee = None
            # Sanitizacion preventiva
            safe_row = {k: (v if v is not None else "") for k, v in row.items()}

            if len(neighbors) > 0:
                for n_idx in neighbors:
                    # Ejecutamos la comparacion jerarquica
                    is_match, strategy = calculate_match_score(
                        safe_row,
                        kb_lists["names"][n_idx],
                        kb_lists["razones"][n_idx],
                        kb_lists["acts"][n_idx],
                        kb_lists["tels"][n_idx],
                        kb_lists["emails"][n_idx],
                        kb_lists["wwws"][n_idx],
                        kb_lists["cps"][n_idx],
                        kb_lists["plazas"][n_idx],
                    )

                    if is_match:
                        best_clee = kb_ids[n_idx]
                        # Si el match es por identidad fuerte, detenemos la busqueda
                        if strategy in ["NAME_MATCH", "RAZON_MATCH"]:
                            break

            # Si no hubo match con vecinos, generamos un CLEE temporal nuevo
            final_clee = best_clee if best_clee else f"NEW_{region_prefix}-{generate_temp_clee()}"

            new_item = row.copy()
            new_item["master_clee"] = final_clee
            new_links.append(new_item)

        # Reconstruccion del DataFrame con los resultados del Algoritmo B
        df_res_B = pl.DataFrame(new_links).select(df_res_A.columns)
    else:
        # Caso donde no hay KB previa o todos los registros son nuevos
        df_res_B = df_missing.with_columns(
            pl.col("row_id")
            .map_elements(
                lambda x: f"NEW_{region_prefix}-{generate_temp_clee()}", return_dtype=pl.Utf8
            )
            .alias("master_clee")
        ).select(df_res_A.columns)

    # Union de resultados Deterministas y Probabilisticos
    return df_res_A.vstack(df_res_B)


def update_kb_logic(kb: pl.DataFrame, new_resolved_data: pl.DataFrame) -> pl.DataFrame:
    """
    Actualiza la KB acumulando historiales de todas las variables.

    Cada campo se mantiene como lista, permitiendo rastrear la evolucion
    del negocio a traves del tiempo (nombres, actividades, contactos, etc.).
    """
    kb_updates = (
        new_resolved_data.with_columns(
            [
                pl.col("entidad").cast(pl.Utf8).fill_null(""),
                pl.col("municipio").cast(pl.Utf8).fill_null(""),
                pl.col("localidad").cast(pl.Utf8).fill_null(""),
                pl.col("fecha_alta_str").cast(pl.Utf8).fill_null(""),
                pl.col("source_paths").cast(pl.Utf8).fill_null(""),
                pl.col("per_ocu").cast(pl.Utf8).fill_null(""),
                pl.col("tipo_asent").cast(pl.Utf8).fill_null(""),
                pl.col("nomb_asent").cast(pl.Utf8).fill_null(""),
                pl.col("tipocencom").cast(pl.Utf8).fill_null(""),
                pl.col("nom_cencom").cast(pl.Utf8).fill_null(""),
                pl.col("cod_postal").cast(pl.Utf8).fill_null(""),
            ]
        )
        .group_by("master_clee")
        .agg(
            [
                pl.col("period").alias("periods"),
                pl.col("nom_estab").alias("names"),
                pl.col("raz_social").alias("razones"),
                pl.col("nombre_act").alias("acts"),
                pl.col("entidad").alias("entidades"),
                pl.col("municipio").alias("municipios"),
                pl.col("localidad").alias("localidades"),
                pl.col("fecha_alta_str").alias("fechas_alta"),
                pl.col("source_paths").alias("source_paths"),
                pl.col("per_ocu").alias("estratos_personal"),
                pl.col("tipo_asent").alias("tipos_asentamiento"),
                pl.col("nomb_asent").alias("asentamientos"),
                pl.col("tipocencom").alias("tipos_centro_comercial"),
                pl.col("nom_cencom").alias("centros_comerciales"),
                pl.col("cod_postal").alias("codigos_postales"),
                pl.col("tel_clean").fill_null("").alias("tels"),
                pl.col("email_clean").fill_null("").alias("emails"),
                pl.col("www_clean").fill_null("").alias("wwws"),
                pl.col("latitud").first().alias("latitud"),
                pl.col("longitud").first().alias("longitud"),
            ]
        )
    )

    kb = kb.join(kb_updates, on="master_clee", how="full", suffix="_new")

    def merge_lists(col_name):
        return pl.concat_list(
            [pl.col(col_name).fill_null([]), pl.col(f"{col_name}_new").fill_null([])]
        ).list.eval(pl.element().flatten().unique())

    return kb.select(
        [
            pl.col("master_clee"),
            pl.coalesce(["latitud", "latitud_new"]).alias("latitud"),
            pl.coalesce(["longitud", "longitud_new"]).alias("longitud"),
            merge_lists("periods").list.sort().alias("periods"),
            merge_lists("names").alias("names"),
            merge_lists("razones").alias("razones"),
            merge_lists("acts").alias("acts"),
            merge_lists("entidades").alias("entidades"),
            merge_lists("municipios").alias("municipios"),
            merge_lists("localidades").alias("localidades"),
            merge_lists("fechas_alta").alias("fechas_alta"),
            merge_lists("source_paths").alias("source_paths"),
            merge_lists("estratos_personal").alias("estratos_personal"),
            merge_lists("tipos_asentamiento").alias("tipos_asentamiento"),
            merge_lists("asentamientos").alias("asentamientos"),
            merge_lists("tipos_centro_comercial").alias("tipos_centro_comercial"),
            merge_lists("centros_comerciales").alias("centros_comerciales"),
            merge_lists("codigos_postales").alias("codigos_postales"),
            merge_lists("tels").alias("tels"),
            merge_lists("emails").alias("emails"),
            merge_lists("wwws").alias("wwws"),
        ]
    )


def consolidate_global_catalog_lazy(lazy_frames: list[pl.LazyFrame]) -> pl.LazyFrame:
    """
    Consolidacion final de snapshots nacionales con todas las variables.

    Recibe LazyFrames de cada snapshot regional/nacional y los fusiona
    en un unico catalogo maestro agrupado por master_clee.
    """
    full_concat = pl.concat(lazy_frames)

    def merge_lists_final(col_name):
        return pl.col(col_name).list.eval(pl.element().flatten().unique())

    return full_concat.group_by("master_clee").agg(
        [
            pl.col("latitud").first(),
            pl.col("longitud").first(),
            merge_lists_final("periods").list.sort().alias("periods"),
            merge_lists_final("names"),
            merge_lists_final("razones"),
            merge_lists_final("acts"),
            merge_lists_final("entidades"),
            merge_lists_final("municipios"),
            merge_lists_final("localidades"),
            merge_lists_final("fechas_alta"),
            merge_lists_final("source_paths"),
            merge_lists_final("estratos_personal"),
            merge_lists_final("tipos_asentamiento"),
            merge_lists_final("asentamientos"),
            merge_lists_final("tipos_centro_comercial"),
            merge_lists_final("centros_comerciales"),
            merge_lists_final("codigos_postales"),
            merge_lists_final("tels"),
            merge_lists_final("emails"),
            merge_lists_final("wwws"),
        ]
    )
