"""
Fase 7 — Evaluacion de precision con CLEE enmascarado.

Mide empiricamente la precision/recall de la cascada de matching usando
el CLEE oficial de INEGI como ground truth: toma un periodo objetivo P
que SI trae CLEE oficial (2021+), lo ENMASCARA, lo resuelve con la
maquinaria de produccion contra los demas periodos oficiales como
referencia, y compara la asignacion contra la verdad.

Protocolo completo en todo.md (seccion "Protocolo de evaluacion",
2026-07-20). Independiente de las columnas clee_propagado_*: la verdad
es `clee` oficial, asi que puede correrse antes/despues de cualquier
re-resolucion. JAMAS escribe en el Silver real: `extract` lee read-only
a parquets y `evaluate` trabaja solo sobre esos parquets.

Uso:
    # 1. Extraer muestra (lectura read-only de Silver; NO correr mientras
    #    una resolucion este escribiendo en Silver)
    uv run python scripts/evaluate_masked_clee.py extract

    # 2. Evaluar (solo parquets + scratch, seguro en cualquier momento)
    uv run python scripts/evaluate_masked_clee.py evaluate

Salidas en data/eval/fase7_masked_clee/: results.parquet (fila por target
con su categoria) y metrics.json (resumen + segmentaciones).
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
import typer

# Ejecutable directo (uv run python scripts/...): mismo patron que
# reorganize_domain_ids.py para resolver los paquetes del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.etl.utils.clee_resolution_config import (  # noqa: E402
    SILVER_READ_QUERY,
    SPATIAL_RADIUS_M,
)
from src.etl.utils.entity_resolution import (  # noqa: E402
    assign_new_clees,
    build_single_period_kb,
    propagate_clees_radio,
    set_matching_logger,
)
from src.etl.utils.matching import clean_contact_columns  # noqa: E402
from src.shared.config import settings  # noqa: E402
from src.shared.db.query import fetch_df  # noqa: E402
from src.shared.db.session import get_engine  # noqa: E402
from src.shared.logging import get_stage_logger  # noqa: E402

logger = get_stage_logger(__name__, Path("./data/etl_fase7_eval.log"))

app = typer.Typer(help="Fase 7: evaluacion de precision con CLEE enmascarado")

EVAL_DIR = settings.DATA_DIR / "eval" / "fase7_masked_clee"
"""Directorio de trabajo de la evaluacion (parquets de muestra + salidas).
Fuera de la nomenclatura Bronze/Silver/Gold a proposito: NO es una etapa
del pipeline, es un artefacto de analisis ad-hoc, regenerable con
`extract` (no se commitea)."""

DEFAULT_TARGET_PERIOD = "2021-05-01"
"""Periodo objetivo por defecto: 05_2021, el mas viejo con CLEE oficial
al 100%. Evaluarlo contra referencias SOLO futuras es el analogo mas fiel
a produccion (la identidad se propaga del futuro al pasado)."""

DEFAULT_STATES = ["09", "14", "32"]
"""Estratos por defecto: 09 CDMX (denso), 14 Jalisco (medio/denso),
32 Zacatecas (rural). Cubren los regimenes de densidad del protocolo con
volumen que corre en minutos."""

CLEE_COVERAGE_MIN = 0.999
"""Cobertura minima de `clee` oficial para aceptar un periodo como
referencia (o como objetivo): filtra periodos pre-2021 donde INEGI no
asignaba CLEE."""

MASTER_COLUMN = "clee_propagado_radio"
"""Columna de trabajo que la maquinaria de produccion rellena durante la
evaluacion. Solo existe en los DataFrames del scratch — el Silver real
nunca se toca. Radio y H3 asignan identico por construccion (paridad
verificada), asi que evaluar con el generador radio basta para la cascada."""

EVAL_READ_QUERY = SILVER_READ_QUERY.replace(
    "SELECT\n        e.id,",
    "SELECT\n        e.id,\n        e.cve_ent,\n        e.cve_mun,",
    1,
)
"""SILVER_READ_QUERY + cve_ent/cve_mun: produccion no las necesita (el
matching no usa geografia administrativa), pero la evaluacion si, para
segmentar metricas por entidad (protocolo de todo.md)."""
assert "e.cve_ent," in EVAL_READ_QUERY, "SILVER_READ_QUERY cambio de formato; ajustar el replace"


def _period_coverage(conn) -> pl.DataFrame:
    """Cobertura de CLEE oficial por periodo (para elegir refs validas)."""
    return fetch_df(
        conn,
        """
        SELECT CAST(period AS VARCHAR) AS period,
               AVG(CASE WHEN clee IS NOT NULL AND clee != '' THEN 1.0 ELSE 0.0 END) AS cobertura
        FROM silver.establecimientos GROUP BY 1 ORDER BY 1
        """,
    )


@app.command()
def extract(
    target_period: str = typer.Option(DEFAULT_TARGET_PERIOD, help="Periodo objetivo (YYYY-MM-DD)"),
    states: list[str] = typer.Option(DEFAULT_STATES, "--state", help="cve_ent de la muestra"),
):
    """
    Extrae la muestra a parquets (lectura READ-ONLY de Silver).

    Guarda target.parquet + ref_{period}.parquet por cada periodo de
    referencia valido (cobertura CLEE >= CLEE_COVERAGE_MIN y period >
    target). No correr mientras una resolucion escribe en Silver.
    """
    EVAL_DIR.mkdir(parents=True, exist_ok=True)
    # Solo lectura: la muestra sale de Silver sin tocarla. Aun asi, no
    # correrlo mientras una resolucion escribe.
    with get_engine().connect() as conn:
        coverage = _period_coverage(conn)
        logger.info(f"Cobertura CLEE por periodo:\n{coverage}")

        target_cov = coverage.filter(pl.col("period") == target_period)
        if target_cov.height == 0 or target_cov["cobertura"][0] < CLEE_COVERAGE_MIN:
            raise typer.BadParameter(
                f"El periodo objetivo {target_period} no tiene cobertura CLEE oficial "
                f">= {CLEE_COVERAGE_MIN} (necesaria como ground truth)."
            )

        ref_periods = coverage.filter(
            (pl.col("cobertura") >= CLEE_COVERAGE_MIN) & (pl.col("period") > target_period)
        )["period"].to_list()
        if not ref_periods:
            raise typer.BadParameter(f"Sin periodos de referencia futuros a {target_period}.")

        base = (
            EVAL_READ_QUERY
            + "\n    WHERE e.period = CAST(:period AS DATE) AND e.cve_ent = ANY(:states)"
        )

        df_target = fetch_df(conn, base, {"period": target_period, "states": list(states)})
        df_target.write_parquet(EVAL_DIR / "target.parquet")
        logger.info(f"target {target_period}: {df_target.height:,} filas -> target.parquet")

        for rp in ref_periods:
            df_ref = fetch_df(conn, base, {"period": rp, "states": list(states)})
            df_ref.write_parquet(EVAL_DIR / f"ref_{rp}.parquet")
            logger.info(f"ref {rp}: {df_ref.height:,} filas -> ref_{rp}.parquet")

        manifest = {
            "target_period": target_period,
            "states": states,
            "ref_periods": ref_periods,
            "extracted_at": datetime.now().isoformat(),
            "target_rows": df_target.height,
        }
        (EVAL_DIR / "manifest.json").write_text(json.dumps(manifest, indent=2))
        logger.info(f"Muestra lista en {EVAL_DIR}")


def _load_masked(path: Path) -> tuple[pl.DataFrame, pl.DataFrame]:
    """
    Carga un parquet de la muestra y enmascara su identidad.

    Devuelve (df enmascarado, verdad). La verdad es (id, clee_verdadero);
    el df queda con MASTER_COLUMN, clee y resolved_by_ref en NULL — la
    maquinaria lo ve como un periodo sin resolver.
    """
    df = clean_contact_columns(pl.read_parquet(path))
    truth = df.select("id", pl.col("clee").alias("clee_verdadero"))
    df = df.with_columns(
        pl.lit(None, dtype=pl.Utf8).alias(MASTER_COLUMN),
        pl.lit(None, dtype=pl.Utf8).alias("clee"),
        pl.lit(None, dtype=pl.Utf8).alias("resolved_by_ref"),
    )
    return df, truth


def _propagate_and_attribute(df_target: pl.DataFrame, kb: pl.DataFrame, label: str) -> pl.DataFrame:
    """
    Propaga una KB al target y atribuye `resolved_by_ref` = label a lo
    recien resuelto.

    Atribucion por columnas, NUNCA por posicion: la propagacion devuelve
    las filas reordenadas (resueltas primero), asi que una mascara
    posicional pre/post quedaria desalineada. "Recien resuelta por esta
    ref" = ya tiene identidad pero sin ref atribuida.
    """
    df_target = propagate_clees_radio(df_target, kb, SPATIAL_RADIUS_M)
    df_target = df_target.with_columns(
        pl.when(pl.col("resolved_by_ref").is_null() & pl.col(MASTER_COLUMN).is_not_null())
        .then(pl.lit(label))
        .otherwise(pl.col("resolved_by_ref"))
        .alias("resolved_by_ref")
    )
    nuevos = (df_target["resolved_by_ref"] == label).sum()
    pendientes = df_target[MASTER_COLUMN].is_null().sum()
    logger.info(f"  ref {label}: +{nuevos:,} resueltas, {pendientes:,} pendientes")
    return df_target


def classify_results(
    assigned: pl.DataFrame,
    truth_col: str,
    known_clees: set[str],
) -> pl.DataFrame:
    """
    Particiona los resultados en las categorias del protocolo.

    Args:
        assigned: DataFrame con `truth_col` (CLEE verdadero) y
            MASTER_COLUMN (asignacion; NEW_* si no matcheo).
        truth_col: Columna con el CLEE oficial enmascarado.
        known_clees: Union de CLEEs presentes en las referencias.

    Returns:
        DataFrame con columnas `evaluable` (bool) y `categoria`:
        - evaluables (verdad existe en alguna referencia):
          correcto | falso_match | falso_nuevo
        - no-evaluables: nuevo_correcto (NEW_*) | match_espurio
    """
    is_new = pl.col(MASTER_COLUMN).str.starts_with("NEW_")
    evaluable = pl.col(truth_col).is_in(sorted(known_clees))
    return assigned.with_columns(evaluable.alias("evaluable")).with_columns(
        pl.when(pl.col("evaluable"))
        .then(
            pl.when(pl.col(MASTER_COLUMN) == pl.col(truth_col))
            .then(pl.lit("correcto"))
            .when(is_new)
            .then(pl.lit("falso_nuevo"))
            .otherwise(pl.lit("falso_match"))
        )
        .otherwise(
            pl.when(is_new).then(pl.lit("nuevo_correcto")).otherwise(pl.lit("match_espurio"))
        )
        .alias("categoria")
    )


def compute_metrics(results: pl.DataFrame) -> dict:
    """
    Metricas del protocolo a partir del DataFrame clasificado.

    precision = correctos / asignados-no-NEW (evaluables + espurios);
    recall = correctos / evaluables; f1 = armonica; tasa_espurios =
    match_espurio / no-evaluables.
    """
    counts = dict(results.group_by("categoria").len().iter_rows())
    correctos = counts.get("correcto", 0)
    falsos_match = counts.get("falso_match", 0)
    falsos_nuevo = counts.get("falso_nuevo", 0)
    espurios = counts.get("match_espurio", 0)
    nuevos_ok = counts.get("nuevo_correcto", 0)

    evaluables = correctos + falsos_match + falsos_nuevo
    no_evaluables = espurios + nuevos_ok
    asignados_no_new = correctos + falsos_match + espurios

    precision = correctos / asignados_no_new if asignados_no_new else 0.0
    recall = correctos / evaluables if evaluables else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0

    return {
        "total": results.height,
        "evaluables": evaluables,
        "no_evaluables": no_evaluables,
        "correctos": correctos,
        "falsos_match": falsos_match,
        "falsos_nuevo": falsos_nuevo,
        "match_espurio": espurios,
        "nuevo_correcto": nuevos_ok,
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
        "tasa_espurios": round(espurios / no_evaluables, 6) if no_evaluables else 0.0,
    }


def _segment_metrics(results: pl.DataFrame) -> dict:
    """Metricas segmentadas: por entidad, sector SCIAN (2 dig.) y por
    periodo de referencia que resolvio cada fila (distancia temporal)."""
    segments: dict[str, dict] = {}
    with_sector = results.with_columns(pl.col("codigo_act").str.slice(0, 2).alias("sector"))
    for col in ["cve_ent", "sector", "resolved_by_ref"]:
        segments[col] = {
            str(key): compute_metrics(group)
            for (key,), group in sorted(with_sector.group_by(col), key=lambda kv: str(kv[0][0]))
        }
    return segments


def _chain_metrics(results: pl.DataFrame) -> dict:
    """
    Metricas del experimento de dos saltos (categorias chain-aware).

    Los correctos incluyen identidades sinteticas heredadas del intermedio
    que apuntan al negocio verdadero (`correcto_new_heredado`); los
    asignados incluyen las identidades NEW_CHAIN (son identidades
    sustantivas del intermedio, no "sin match").
    """
    counts = dict(results.group_by("categoria").len().iter_rows())
    categorias = [
        "correcto",
        "correcto_new_heredado",
        "falso_match_heredado",
        "falso_match",
        "falso_nuevo",
        "nuevo_correcto",
        "match_espurio",
    ]
    correctos = counts.get("correcto", 0) + counts.get("correcto_new_heredado", 0)
    falsos = (
        counts.get("falso_match", 0)
        + counts.get("falso_match_heredado", 0)
        + counts.get("match_espurio", 0)
    )
    evaluables = results.filter(pl.col("evaluable")).height
    asignados = correctos + falsos
    precision = correctos / asignados if asignados else 0.0
    recall = correctos / evaluables if evaluables else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    return {
        "total": results.height,
        "evaluables": evaluables,
        "categorias": {k: counts.get(k, 0) for k in categorias},
        "precision": round(precision, 6),
        "recall": round(recall, 6),
        "f1": round(f1, 6),
    }


@app.command()
def evaluate(
    ref_order: str = typer.Option(
        "nearest",
        "--ref-order",
        help="Orden de referencias: 'nearest' (mas cercana al target "
        "primero, replica produccion) o 'newest' (orden legado, comparacion)",
    ),
):
    """
    Corre la evaluacion sobre la muestra extraida (solo parquets).

    Enmascara el CLEE del target, lo resuelve contra cada referencia en el
    orden pedido (--ref-order; 'nearest' replica la ventana de produccion
    desde el hallazgo de Fase 7), asigna NEW_* a lo no resuelto y
    clasifica contra la verdad. Escribe results/metrics en EVAL_DIR; el
    sufijo del archivo es el orden ('' = newest legado, '_nearest' =
    produccion) para poder compararlos lado a lado.
    """
    if ref_order not in ("newest", "nearest"):
        raise typer.BadParameter("--ref-order debe ser 'newest' o 'nearest'")
    manifest = json.loads((EVAL_DIR / "manifest.json").read_text())
    set_matching_logger(logger)

    df_target, truth = _load_masked(EVAL_DIR / "target.parquet")
    logger.info(
        f"Evaluando {df_target.height:,} filas de {manifest['target_period']} "
        f"contra {len(manifest['ref_periods'])} referencias (orden: {ref_order})..."
    )

    known_clees: set[str] = set()
    # Todas las refs son futuras al target, asi que orden ascendente
    # equivale a "mas cercana temporalmente primero".
    refs_ordenadas = sorted(manifest["ref_periods"], reverse=(ref_order == "newest"))
    for rp in refs_ordenadas:
        df_ref = clean_contact_columns(pl.read_parquet(EVAL_DIR / f"ref_{rp}.parquet"))
        # Identidad de la KB = CLEE OFICIAL de la referencia (ground truth)
        kb = build_single_period_kb(df_ref, master_column="clee")
        known_clees.update(kb["clee_id"].to_list())
        del df_ref
        df_target = _propagate_and_attribute(df_target, kb, rp)
        del kb
        if df_target[MASTER_COLUMN].is_null().sum() == 0:
            break

    df_target = assign_new_clees(df_target, region_prefix="EVAL", master_column=MASTER_COLUMN)

    results = classify_results(
        df_target.join(truth, on="id").with_columns(
            pl.col("resolved_by_ref").fill_null("sin_match")
        ),
        truth_col="clee_verdadero",
        known_clees=known_clees,
    ).select(
        "id",
        "clee_verdadero",
        MASTER_COLUMN,
        "evaluable",
        "categoria",
        "resolved_by_ref",
        "codigo_act",
        "cve_ent",
        "cve_mun",
    )
    sufijo = "" if ref_order == "newest" else f"_{ref_order}"
    results.write_parquet(EVAL_DIR / f"results{sufijo}.parquet")

    metrics = {
        "manifest": manifest,
        "ref_order": ref_order,
        "evaluated_at": datetime.now().isoformat(),
        "global": compute_metrics(results),
        "segments": _segment_metrics(results),
    }
    (EVAL_DIR / f"metrics{sufijo}.json").write_text(json.dumps(metrics, indent=2))

    g = metrics["global"]
    logger.info(
        f"✅ Evaluacion completa: precision={g['precision']:.4f} "
        f"recall={g['recall']:.4f} f1={g['f1']:.4f} "
        f"(evaluables={g['evaluables']:,}, espurios={g['match_espurio']:,} "
        f"de {g['no_evaluables']:,} no-evaluables)"
    )
    logger.info(
        f"Salidas: {EVAL_DIR / f'results{sufijo}.parquet'} y {EVAL_DIR / f'metrics{sufijo}.json'}"
    )


@app.command()
def chain(
    intermediate: str = typer.Option(
        "2021-11-01",
        "--intermediate",
        help="Periodo intermedio I (debe estar en la muestra extraida): se "
        "resuelve enmascarado y el target solo llega a el via encadenamiento",
    ),
):
    """
    Experimento de dos saltos: mide el error transitivo del encadenamiento.

    Paso 1: enmascara el intermedio I y lo resuelve nearest-first contra
    las referencias oficiales POSTERIORES a I (sus NEW_* llevan prefijo
    CHAIN). Paso 2: resuelve el target usando I-RESUELTO como primera
    referencia (identidad = columna propagada, como haria produccion con
    nearest-first) y despues las mismas referencias oficiales; el target
    NUNCA ve el clee oficial de I directamente.

    Un match del target a una fila de I cuenta como correcto si esa fila
    de I pertenece (segun su clee oficial) al mismo negocio — aunque la
    identidad heredada sea NEW_CHAIN (correcto_new_heredado). Si el target
    matcheo la fila correcta de I pero I la habia resuelto mal, es
    falso_match_heredado (error del paso 1, no del 2). Comparar contra
    metrics_nearest.json (un salto directo, mismo universo evaluable).

    Escribe results_chain.parquet y metrics_chain.json.
    """
    manifest = json.loads((EVAL_DIR / "manifest.json").read_text())
    if intermediate not in manifest["ref_periods"]:
        raise typer.BadParameter(
            f"{intermediate} no esta en la muestra extraida ({manifest['ref_periods']})"
        )
    set_matching_logger(logger)

    refs_post = sorted(rp for rp in manifest["ref_periods"] if rp > intermediate)
    if not refs_post:
        raise typer.BadParameter(f"Sin referencias posteriores a {intermediate}.")

    # ---- Paso 1: resolver el intermedio enmascarado (nearest-first) ----
    df_i, truth_i = _load_masked(EVAL_DIR / f"ref_{intermediate}.parquet")
    logger.info(
        f"[chain 1/2] Resolviendo intermedio {intermediate} enmascarado "
        f"({df_i.height:,} filas) contra {len(refs_post)} refs oficiales..."
    )
    known_oficiales: set[str] = set()
    for rp in refs_post:
        df_ref = clean_contact_columns(pl.read_parquet(EVAL_DIR / f"ref_{rp}.parquet"))
        kb = build_single_period_kb(df_ref, master_column="clee")
        known_oficiales.update(kb["clee_id"].to_list())
        del df_ref
        df_i = _propagate_and_attribute(df_i, kb, rp)
        del kb
        if df_i[MASTER_COLUMN].is_null().sum() == 0:
            break
    df_i = assign_new_clees(df_i, region_prefix="CHAIN", master_column=MASTER_COLUMN)

    results_i = classify_results(
        df_i.join(truth_i, on="id").with_columns(pl.col("resolved_by_ref").fill_null("sin_match")),
        truth_col="clee_verdadero",
        known_clees=known_oficiales,
    )
    metrics_i = compute_metrics(results_i)
    logger.info(
        f"[chain 1/2] Intermedio resuelto: precision={metrics_i['precision']:.4f} "
        f"recall={metrics_i['recall']:.4f}"
    )

    # Identidad asignada en I -> clees oficiales reales de las filas de I
    # que la portan. Si el target matchea esa identidad y su clee verdadero
    # esta en la lista, el encadenamiento apunto al mismo negocio.
    map_i = (
        results_i.select(pl.col(MASTER_COLUMN).alias("__id_i"), "clee_verdadero")
        .group_by("__id_i")
        .agg(pl.col("clee_verdadero").unique().alias("clees_reales_i"))
    )

    # ---- Paso 2: target via I-resuelto, luego refs oficiales ----
    df_t, truth_t = _load_masked(EVAL_DIR / "target.parquet")
    via_label = f"via_{intermediate}"
    logger.info(
        f"[chain 2/2] Resolviendo target {manifest['target_period']} "
        f"({df_t.height:,} filas): primero {via_label}, luego refs oficiales..."
    )
    kb_i = build_single_period_kb(df_i, master_column=MASTER_COLUMN)
    del df_i
    df_t = _propagate_and_attribute(df_t, kb_i, via_label)
    del kb_i
    for rp in refs_post:
        if df_t[MASTER_COLUMN].is_null().sum() == 0:
            break
        df_ref = clean_contact_columns(pl.read_parquet(EVAL_DIR / f"ref_{rp}.parquet"))
        kb = build_single_period_kb(df_ref, master_column="clee")
        del df_ref
        df_t = _propagate_and_attribute(df_t, kb, rp)
        del kb
    df_t = assign_new_clees(df_t, region_prefix="EVAL", master_column=MASTER_COLUMN)

    # ---- Clasificacion chain-aware ----
    # Universo evaluable: clees oficiales de I + refs posteriores = las 7
    # referencias de la muestra (mismo universo que la corrida nearest).
    universo = known_oficiales | set(truth_i["clee_verdadero"].to_list())

    assigned = (
        df_t.join(truth_t, on="id")
        .with_columns(pl.col("resolved_by_ref").fill_null("sin_match"))
        .join(map_i, left_on=MASTER_COLUMN, right_on="__id_i", how="left")
    )

    sin_identidad = pl.col(MASTER_COLUMN).str.starts_with("NEW_EVAL")
    es_new_chain = pl.col(MASTER_COLUMN).str.starts_with("NEW_CHAIN")
    es_via_i = pl.col("resolved_by_ref") == via_label
    directo_ok = pl.col(MASTER_COLUMN) == pl.col("clee_verdadero")
    mismo_negocio_i = (
        pl.col("clees_reales_i").list.contains(pl.col("clee_verdadero")).fill_null(False)
    )
    evaluable = pl.col("clee_verdadero").is_in(sorted(universo))

    results = (
        assigned.with_columns(evaluable.alias("evaluable"))
        .with_columns(
            pl.when(pl.col("evaluable"))
            .then(
                pl.when(sin_identidad)
                .then(pl.lit("falso_nuevo"))
                .when(directo_ok)
                .then(pl.lit("correcto"))
                .when(es_new_chain & mismo_negocio_i)
                .then(pl.lit("correcto_new_heredado"))
                .when(es_via_i & mismo_negocio_i)
                .then(pl.lit("falso_match_heredado"))
                .otherwise(pl.lit("falso_match"))
            )
            .otherwise(
                pl.when(sin_identidad)
                .then(pl.lit("nuevo_correcto"))
                .otherwise(pl.lit("match_espurio"))
            )
            .alias("categoria")
        )
        .select(
            "id",
            "clee_verdadero",
            MASTER_COLUMN,
            "evaluable",
            "categoria",
            "resolved_by_ref",
            "codigo_act",
            "cve_ent",
            "cve_mun",
        )
    )
    results.write_parquet(EVAL_DIR / "results_chain.parquet")

    por_ref = {
        str(key): _chain_metrics(group)
        for (key,), group in sorted(
            results.group_by("resolved_by_ref"), key=lambda kv: str(kv[0][0])
        )
    }
    metrics = {
        "manifest": manifest,
        "intermediate": intermediate,
        "evaluated_at": datetime.now().isoformat(),
        "paso1_intermedio": metrics_i,
        "paso2_target": {"global": _chain_metrics(results), "por_ref": por_ref},
    }
    (EVAL_DIR / "metrics_chain.json").write_text(json.dumps(metrics, indent=2))

    g = metrics["paso2_target"]["global"]
    logger.info(
        f"✅ Chain completo: precision={g['precision']:.4f} recall={g['recall']:.4f} "
        f"f1={g['f1']:.4f} (correcto_new_heredado="
        f"{g['categorias']['correcto_new_heredado']:,}, falso_match_heredado="
        f"{g['categorias']['falso_match_heredado']:,})"
    )
    logger.info(f"Salidas: {EVAL_DIR / 'results_chain.parquet'} y {EVAL_DIR / 'metrics_chain.json'}")


if __name__ == "__main__":
    app()
