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

Salidas en data/curated/fase7_eval/: results.parquet (fila por target con
su categoria) y metrics.json (resumen + segmentaciones).
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

from src_etl.core.config import settings  # noqa: E402
from src_etl.core.logging import get_stage_logger  # noqa: E402
from src_etl.utils.clee_resolution_config import (  # noqa: E402
    SILVER_READ_QUERY,
    SPATIAL_RADIUS_M,
)
from src_etl.utils.entity_resolution import (  # noqa: E402
    assign_new_clees,
    build_single_period_kb,
    propagate_clees_radio,
    set_matching_logger,
)
from src_etl.utils.matching import clean_contact_columns  # noqa: E402
from src_etl.utils.warehouse import connect_configured  # noqa: E402

logger = get_stage_logger(__name__, Path("./data/etl_fase7_eval.log"))

app = typer.Typer(help="Fase 7: evaluacion de precision con CLEE enmascarado")

EVAL_DIR = settings.DATA_DIR / "curated" / "fase7_eval"
"""Directorio de trabajo de la evaluacion (parquets de muestra + salidas).
Vive en data/curated/ (gitignored, regenerable con `extract`)."""

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


def _period_coverage(conn) -> pl.DataFrame:
    """Cobertura de CLEE oficial por periodo (para elegir refs validas)."""
    return conn.execute("""
        SELECT CAST(period AS VARCHAR) AS period,
               AVG(CASE WHEN clee IS NOT NULL AND clee != '' THEN 1.0 ELSE 0.0 END) AS cobertura
        FROM establecimientos GROUP BY 1 ORDER BY 1
    """).pl()


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
    conn = connect_configured(settings.SILVER_DUCKDB_PATH, read_only=True)
    try:
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

        placeholders = ", ".join(["?"] * len(states))
        base = SILVER_READ_QUERY + f"\n    WHERE e.period = ? AND e.cve_ent IN ({placeholders})"

        df_target = conn.execute(base, [target_period, *states]).pl()
        df_target.write_parquet(EVAL_DIR / "target.parquet")
        logger.info(f"target {target_period}: {df_target.height:,} filas -> target.parquet")

        for rp in ref_periods:
            df_ref = conn.execute(base, [rp, *states]).pl()
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
    finally:
        conn.close()


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


@app.command()
def evaluate():
    """
    Corre la evaluacion sobre la muestra extraida (solo parquets).

    Enmascara el CLEE del target, lo resuelve contra cada referencia
    (mas nueva primero, misma ventana que produccion), asigna NEW_* a lo
    no resuelto y clasifica contra la verdad. Escribe results.parquet y
    metrics.json en EVAL_DIR.
    """
    manifest = json.loads((EVAL_DIR / "manifest.json").read_text())
    set_matching_logger(logger)

    df_target = clean_contact_columns(pl.read_parquet(EVAL_DIR / "target.parquet"))
    truth = df_target.select("id", pl.col("clee").alias("clee_verdadero"))
    # Mascara: la maquinaria ve el periodo como no resuelto
    df_target = df_target.with_columns(
        pl.lit(None, dtype=pl.Utf8).alias(MASTER_COLUMN),
        pl.lit(None, dtype=pl.Utf8).alias("clee"),
        pl.lit(None, dtype=pl.Utf8).alias("resolved_by_ref"),
    )
    logger.info(
        f"Evaluando {df_target.height:,} filas de {manifest['target_period']} "
        f"contra {len(manifest['ref_periods'])} referencias..."
    )

    known_clees: set[str] = set()
    for rp in sorted(manifest["ref_periods"], reverse=True):
        df_ref = clean_contact_columns(pl.read_parquet(EVAL_DIR / f"ref_{rp}.parquet"))
        # Identidad de la KB = CLEE OFICIAL de la referencia (ground truth)
        kb = build_single_period_kb(df_ref, master_column="clee")
        known_clees.update(kb["clee_id"].to_list())
        del df_ref

        antes = df_target[MASTER_COLUMN].is_null()
        df_target = propagate_clees_radio(df_target, kb, SPATIAL_RADIUS_M)
        nuevos = antes & df_target[MASTER_COLUMN].is_not_null()
        df_target = df_target.with_columns(
            pl.when(pl.Series(nuevos))
            .then(pl.lit(rp))
            .otherwise(pl.col("resolved_by_ref"))
            .alias("resolved_by_ref")
        )
        pendientes = df_target[MASTER_COLUMN].is_null().sum()
        logger.info(f"  ref {rp}: +{int(nuevos.sum()):,} resueltas, {pendientes:,} pendientes")
        del kb
        if pendientes == 0:
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
    results.write_parquet(EVAL_DIR / "results.parquet")

    metrics = {
        "manifest": manifest,
        "evaluated_at": datetime.now().isoformat(),
        "global": compute_metrics(results),
        "segments": _segment_metrics(results),
    }
    (EVAL_DIR / "metrics.json").write_text(json.dumps(metrics, indent=2))

    g = metrics["global"]
    logger.info(
        f"✅ Evaluacion completa: precision={g['precision']:.4f} "
        f"recall={g['recall']:.4f} f1={g['f1']:.4f} "
        f"(evaluables={g['evaluables']:,}, espurios={g['match_espurio']:,} "
        f"de {g['no_evaluables']:,} no-evaluables)"
    )
    logger.info(f"Salidas: {EVAL_DIR / 'results.parquet'} y {EVAL_DIR / 'metrics.json'}")


if __name__ == "__main__":
    app()
