"""
Metricas de exito de la propagacion CLEE sobre una base ya resuelta.

Complementa `scripts/evaluate_masked_clee.py`: aquel mide PRECISION contra
ground truth sobre una muestra enmascarada; este mide COBERTURA y salud
estructural de las tablas de resolucion tal como quedaron en el
warehouse real, sobre las 77M de filas y sin ground truth.

Las tres preguntas que responde:

1. **Tasa de vinculacion** (el "porcentaje de exito"): que fraccion de las
   filas sin CLEE oficial (cortes 2015-2020) terminaron con una identidad
   REAL de INEGI en vez de una sintetica `NEW_*`. Se reporta por periodo
   y agregada.
2. **Cadenas de identidad**: cuantos periodos cubre cada identidad. Una
   identidad de un solo periodo no aporta nada al analisis de
   supervivencia (la unidad de analisis de la tesis); la distribucion de
   la longitud de cadena es la medida directa de cuanta historia se
   reconstruyo.
3. **Colisiones intra-periodo**: una identidad que aparece dos veces en
   el MISMO corte es imposible por construccion (un establecimiento
   aparece una vez por foto) — mide errores duros de la cascada sin
   necesidad de ground truth.

Siempre abre la base en READ-ONLY: es un script de analisis, jamas
escribe en Silver.

Uso:
    uv run python scripts/clee_propagation_stats.py            # Silver real
    docker compose run --rm tools python scripts/clee_propagation_stats.py
    uv run python scripts/clee_propagation_stats.py --method radio

Salida: tabla en el log + JSON en data/eval/propagation_stats/.
"""

import json
import sys
from datetime import datetime
from pathlib import Path

import polars as pl
import typer
from sqlalchemy import text
from sqlalchemy.engine import Connection

# Ejecutable directo (uv run python scripts/...): mismo patron que
# evaluate_masked_clee.py para resolver los paquetes del proyecto.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.etl.transformers.clee_resolver import CLEEResolver
from src.etl.utils.clee_resolution_config import RESOLVED_COLUMN  # noqa: E402
from src.shared.config import settings  # noqa: E402
from src.shared.db.query import fetch_df  # noqa: E402
from src.shared.db.session import get_engine  # noqa: E402
from src.shared.logging import get_stage_logger  # noqa: E402

logger = get_stage_logger(__name__, Path("./data/etl_propagation_stats.log"))

app = typer.Typer(help="Metricas de cobertura/salud de la resolucion CLEE por metodo")

STATS_DIR = settings.DATA_DIR / "eval" / "propagation_stats"
"""Directorio de salida de los JSON de metricas. Vive bajo data/eval/ (no
Bronze/Silver/Gold) porque es un artefacto de analisis regenerable, igual
que la evaluacion de Fase 7."""

SYNTHETIC_PREFIX = "NEW_"
"""Prefijo de las identidades sinteticas que asigna `assign_new_clees()`
(`NEW_{prefijo_region}-{uuid12}`). NO es configurable por .env: esta
acoplado al formato materializado en `clee_resuelto` y a
`entidades_economicas.es_sintetica`, que lo usa con el mismo literal."""

DEFAULT_METHODS = list(CLEEResolver.METHODS)
"""Metodos a analizar por defecto, del registro del resolver (radio, h3, y
cualquiera que se registre despues). Se derivan del registro para que
agregar un metodo no requiera tocar este script."""


def metodo_source(method: str) -> str:
    """Fuente de lectura de un metodo: el hecho unido a su identidad.

    La identidad vive en `silver.clee_<metodo>` y el `clee` de INEGI en
    `silver.establecimientos`, asi que toda metrica que cruce los dos
    necesita el JOIN. Se entrega como subconsulta para que las funciones de
    metricas sigan recibiendo "una tabla" y su SQL no cambie.

    El JOIN lleva `period` ademas de `id`: es lo que lo hace partition-wise.

    Args:
        method: metodo registrado en `CLEEResolver.METHODS`.

    Returns:
        str: subconsulta aliaseada, usable como `FROM {...}`.
    """
    tabla = CLEEResolver.METHODS[method]["table"]
    return f"""(
        SELECT e.id, e.period, e.clee, r.{RESOLVED_COLUMN}
        FROM silver.establecimientos e
        LEFT JOIN silver.{tabla} r ON r.id = e.id AND r.period = e.period
    ) AS src"""


def comparacion_source(metodo_a: str, metodo_b: str) -> str:
    """Fuente para comparar dos metodos fila a fila.

    Trae la identidad de ambos para el mismo `(id, period)`, aliaseadas
    `identidad_a` / `identidad_b`.

    Args:
        metodo_a: primer metodo a comparar.
        metodo_b: segundo metodo.

    Returns:
        str: subconsulta aliaseada.
    """
    tabla_a = CLEEResolver.METHODS[metodo_a]["table"]
    tabla_b = CLEEResolver.METHODS[metodo_b]["table"]
    return f"""(
        SELECT e.id, e.period,
               a.{RESOLVED_COLUMN} AS identidad_a,
               b.{RESOLVED_COLUMN} AS identidad_b
        FROM silver.establecimientos e
        LEFT JOIN silver.{tabla_a} a ON a.id = e.id AND a.period = e.period
        LEFT JOIN silver.{tabla_b} b ON b.id = e.id AND b.period = e.period
    ) AS src"""


def _real_identity_predicate(column: str) -> str:
    """Predicado SQL de 'identidad real' (asignada y no sintetica)."""
    return f"{column} IS NOT NULL AND NOT starts_with({column}, '{SYNTHETIC_PREFIX}')"


def open_source() -> Connection:
    """Conexion de lectura a Silver.

    Antes aceptaba una ruta: un archivo del warehouse o un parquet con un
    volcado. Con los datos en un servidor eso dejo de tener sentido --hay
    una sola Silver y es la que dice la configuracion. Para analizar un
    volcado suelto, Polars lee el parquet directo sin pasar por aqui.

    Returns:
        Connection: conexion abierta; el llamador la cierra.
    """
    return get_engine().connect()


def compute_period_metrics(
    conn: Connection,
    column: str,
    table: str,
) -> pl.DataFrame:
    """
    Cobertura de la columna de identidad periodo por periodo.

    Args:
        conn: conexion de lectura.
        column: columna de identidad en `table`.
        table: fuente de lectura; normalmente `metodo_source(metodo)`.

    Returns:
        DataFrame con una fila por periodo:
        - `total`: filas del corte.
        - `con_clee_oficial`: filas que traen CLEE de INEGI (los cortes
          2021+ vienen al 100%; los viejos, en 0).
        - `asignadas`: filas con identidad no nula (debe ser == total tras
          una resolucion completa; menos indica corrida incompleta).
        - `vinculadas`: filas con identidad REAL (no `NEW_*`).
        - `sinteticas`: filas con identidad `NEW_*`.
        - `tasa_vinculacion`: vinculadas / total.
        - `tasa_cobertura`: asignadas / total.
    """
    df = fetch_df(
        conn,
        f"""
        SELECT
            CAST(period AS VARCHAR) AS period,
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE clee IS NOT NULL AND clee != '') AS con_clee_oficial,
            COUNT({column}) AS asignadas,
            COUNT(*) FILTER (WHERE {_real_identity_predicate(column)}) AS vinculadas,
            COUNT(*) FILTER (WHERE starts_with({column}, '{SYNTHETIC_PREFIX}')) AS sinteticas
        FROM {table}
        WHERE period IS NOT NULL
        GROUP BY 1
        ORDER BY 1
    """,
    )

    return df.with_columns(
        (pl.col("vinculadas") / pl.col("total")).alias("tasa_vinculacion"),
        (pl.col("asignadas") / pl.col("total")).alias("tasa_cobertura"),
    )


def compute_identity_metrics(
    conn: Connection,
    column: str,
    table: str,
) -> dict:
    """
    Metricas por IDENTIDAD: nacimientos, cadenas y colisiones.

    Una sola agregacion por identidad (`GROUP BY column`) alimenta las
    tres familias de metricas — es la operacion cara del script (77M filas
    en produccion), asi que se materializa una vez en tabla temporal y se
    consulta varias veces.

    Definiciones:
    - **Nacimiento** de una identidad sintetica = el periodo MAS NUEVO en
      que aparece. La propagacion va futuro -> pasado, asi que `NEW_*` se
      crea en el corte mas reciente donde el negocio no encontro pareja y
      de ahi se hereda hacia atras. Comparar nacimientos por periodo
      contra los "nuevos" de `data/etl_clee_resolution_{metodo}.log` es la
      verificacion cruzada de esta lectura.
    - **Cadena** = numero de periodos distintos que cubre la identidad.
      Longitud 1 = observacion aislada (no aporta supervivencia).
    - **Colision** = la identidad aparece 2+ veces dentro de un mismo
      periodo (imposible por construccion: un establecimiento aparece una
      vez por foto). Se detecta sin un segundo GROUP BY caro: si
      `filas > periodos_distintos`, hay repeticion intra-periodo.

    Args:
        conn: conexion de lectura (la temp table vive fuera
            de la base).
        column: Columna de identidad.
        table: tabla de hechos, calificada con su schema.

    Returns:
        Dict con `identidades` (conteos globales), `cadenas`
        (distribucion de longitud), `colisiones` (agregado + desglose
        `por_periodo`) y `nacimientos_sinteticos` (identidades sinteticas
        nacidas por periodo).
    """
    tmp = "_ident_stats"
    # Una temporal con el agregado por identidad: lo consultan cuatro
    # resumenes distintos y recalcular el GROUP BY sobre 82.9M filas cuatro
    # veces no tiene sentido. `DROP ... IF EXISTS` antes porque PostgreSQL
    # no tiene `CREATE OR REPLACE` para tablas.
    conn.execute(text(f"DROP TABLE IF EXISTS {tmp}"))
    conn.execute(
        text(f"""
        CREATE TEMP TABLE {tmp} AS
        SELECT
            {column} AS identidad,
            starts_with({column}, '{SYNTHETIC_PREFIX}') AS sintetica,
            COUNT(*) AS filas,
            COUNT(DISTINCT period) AS periodos,
            CAST(MAX(period) AS VARCHAR) AS nacimiento,
            CAST(MIN(period) AS VARCHAR) AS mas_antiguo
        FROM {table}
        WHERE {column} IS NOT NULL
        GROUP BY 1, 2
    """)
    )

    totales = fetch_df(
        conn,
        f"""
        SELECT
            COUNT(*) AS identidades,
            COUNT(*) FILTER (WHERE sintetica) AS sinteticas,
            COUNT(*) FILTER (WHERE NOT sintetica) AS reales,
            COUNT(*) FILTER (WHERE periodos > 1) AS longitudinales,
            SUM(filas) AS filas,
            AVG(periodos) AS periodos_promedio
        FROM {tmp}
    """,
    ).to_dicts()[0]

    cadenas = fetch_df(
        conn,
        f"""
        SELECT periodos, sintetica, COUNT(*) AS identidades, SUM(filas) AS filas
        FROM {tmp} GROUP BY 1, 2 ORDER BY 1, 2
    """,
    )

    colisiones = fetch_df(
        conn,
        f"""
        SELECT
            COUNT(*) FILTER (WHERE filas > periodos) AS identidades_con_colision,
            COALESCE(SUM(filas - periodos), 0) AS filas_excedentes
        FROM {tmp}
    """,
    ).to_dicts()[0]

    nacimientos = fetch_df(
        conn,
        f"""
        SELECT nacimiento AS period, COUNT(*) AS identidades_nacidas
        FROM {tmp} WHERE sintetica GROUP BY 1 ORDER BY 1
    """,
    )

    conn.execute(text(f"DROP TABLE IF EXISTS {tmp}"))

    # Desglose por corte: en que periodos se concentran las colisiones
    # (los agregados de arriba dicen cuantas hay, no donde).
    colisiones_periodo = fetch_df(
        conn,
        f"""
        WITH dup AS (
            SELECT period, {column} AS identidad, COUNT(*) AS filas
            FROM {table}
            WHERE {column} IS NOT NULL AND period IS NOT NULL
            GROUP BY 1, 2
            HAVING COUNT(*) > 1
        )
        SELECT
            CAST(period AS VARCHAR) AS period,
            COUNT(*) AS identidades_con_colision,
            SUM(filas) AS filas_involucradas,
            SUM(filas - 1) AS filas_excedentes
        FROM dup GROUP BY 1 ORDER BY 1
    """,
    )

    identidades = int(totales["identidades"])
    return {
        "identidades": {
            "total": identidades,
            "reales": int(totales["reales"]),
            "sinteticas": int(totales["sinteticas"]),
            "longitudinales": int(totales["longitudinales"]),
            "tasa_longitudinal": (
                int(totales["longitudinales"]) / identidades if identidades else 0.0
            ),
            "periodos_promedio": float(totales["periodos_promedio"] or 0.0),
        },
        "cadenas": cadenas.to_dicts(),
        "colisiones": {
            "identidades_con_colision": int(colisiones["identidades_con_colision"]),
            "filas_excedentes": int(colisiones["filas_excedentes"]),
            "tasa_identidades_con_colision": (
                int(colisiones["identidades_con_colision"]) / identidades if identidades else 0.0
            ),
            "por_periodo": colisiones_periodo.to_dicts(),
        },
        "nacimientos_sinteticos": nacimientos.to_dicts(),
    }


def compute_agreement(
    conn: Connection,
    col_a: str,
    col_b: str,
    table: str,
) -> dict:
    """
    Acuerdo fila a fila entre dos columnas de identidad (radio vs h3).

    Radio y H3 comparten cascada y radio efectivo: solo cambia el
    generador de candidatos, asi que deberian asignar identico salvo por
    las identidades sinteticas, que llevan prefijo distinto por metodo y
    uuid aleatorio (nunca coinciden literalmente aunque el veredicto
    "sin match" sea el mismo).

    Returns:
        Dict con el conteo de cada categoria y `tasa_acuerdo_semantico`
        = (identicas + ambas sinteticas) / total, la unica comparacion
        justa entre metodos.
    """
    pred_a, pred_b = _real_identity_predicate(col_a), _real_identity_predicate(col_b)
    row = fetch_df(
        conn,
        f"""
        SELECT
            COUNT(*) AS total,
            COUNT(*) FILTER (WHERE {col_a} IS NOT DISTINCT FROM {col_b}) AS identicas,
            COUNT(*) FILTER (
                WHERE starts_with({col_a}, '{SYNTHETIC_PREFIX}')
                  AND starts_with({col_b}, '{SYNTHETIC_PREFIX}')
                  AND {col_a} != {col_b}
            ) AS ambas_sinteticas,
            COUNT(*) FILTER (WHERE ({pred_a}) AND ({pred_b}) AND {col_a} != {col_b})
                AS real_distinta,
            COUNT(*) FILTER (
                WHERE (({pred_a}) AND starts_with({col_b}, '{SYNTHETIC_PREFIX}'))
                   OR (({pred_b}) AND starts_with({col_a}, '{SYNTHETIC_PREFIX}'))
            ) AS real_vs_sintetica
        FROM {table}
    """,
    ).to_dicts()[0]

    total = int(row["total"])
    acuerdo = int(row["identicas"]) + int(row["ambas_sinteticas"])
    return {
        "columnas": [col_a, col_b],
        "total": total,
        "identicas": int(row["identicas"]),
        "ambas_sinteticas": int(row["ambas_sinteticas"]),
        "real_distinta": int(row["real_distinta"]),
        "real_vs_sintetica": int(row["real_vs_sintetica"]),
        "tasa_acuerdo_semantico": acuerdo / total if total else 0.0,
    }


def summarize_periods(periods: pl.DataFrame) -> dict:
    """
    Agrega las metricas por periodo en los totales que se reportan.

    Separa los dos regimenes, porque mezclarlos infla el numero: los
    cortes con CLEE oficial se resuelven por copia directa (100% de exito
    trivial) y solo los cortes SIN CLEE ponen a prueba la propagacion.
    `tasa_vinculacion_propagados` es LA metrica de exito del script.
    """
    if periods.is_empty():
        return {
            "total": 0,
            "propagados": {"periodos": 0, "total": 0, "vinculadas": 0, "sinteticas": 0},
            "oficiales": {"periodos": 0, "total": 0},
            "tasa_vinculacion_propagados": 0.0,
            "tasa_cobertura_global": 0.0,
        }

    oficiales = periods.filter(pl.col("con_clee_oficial") == pl.col("total"))
    propagados = periods.filter(pl.col("con_clee_oficial") != pl.col("total"))

    prop_total = int(propagados["total"].sum())
    prop_vinc = int(propagados["vinculadas"].sum())
    return {
        "total": int(periods["total"].sum()),
        "propagados": {
            "periodos": propagados.height,
            "total": prop_total,
            "vinculadas": prop_vinc,
            "sinteticas": int(propagados["sinteticas"].sum()),
        },
        "oficiales": {
            "periodos": oficiales.height,
            "total": int(oficiales["total"].sum()),
        },
        "tasa_vinculacion_propagados": prop_vinc / prop_total if prop_total else 0.0,
        "tasa_cobertura_global": (
            int(periods["asignadas"].sum()) / int(periods["total"].sum())
            if int(periods["total"].sum())
            else 0.0
        ),
    }


def _log_period_table(
    column: str,
    periods: pl.DataFrame,
    nacimientos: dict[str, int],
    colisiones: dict[str, int],
) -> None:
    """Imprime la tabla por periodo en el log (formato legible en terminal)."""
    logger.info(f"--- {column}: cobertura por periodo ---")
    logger.info(
        f"{'periodo':<12}{'filas':>12}{'vinculadas':>13}{'sinteticas':>12}"
        f"{'nacidas':>10}{'colision':>11}{'% exito':>9}"
    )
    for r in periods.iter_rows(named=True):
        oficial = "*" if r["con_clee_oficial"] == r["total"] else " "
        logger.info(
            f"{r['period']:<11}{oficial}{r['total']:>12,}{r['vinculadas']:>13,}"
            f"{r['sinteticas']:>12,}{nacimientos.get(r['period'], 0):>10,}"
            f"{colisiones.get(r['period'], 0):>11,}{r['tasa_vinculacion'] * 100:>8.2f}%"
        )
    logger.info("   (* = corte con CLEE oficial: resolucion deterministica)")


@app.command()
def run(
    methods: list[str] = typer.Option(
        DEFAULT_METHODS,
        "--method",
        help="Metodo(s) de resolucion a analizar (radio, h3). Repetible.",
    ),
    identity_metrics: bool = typer.Option(
        True,
        "--identity-metrics/--no-identity-metrics",
        help="Calcular cadenas/colisiones/nacimientos (GROUP BY por "
        "identidad: es la parte cara sobre 77M filas).",
    ),
    agreement: bool = typer.Option(
        True,
        "--agreement/--no-agreement",
        help="Comparar los dos primeros metodos fila a fila (radio vs h3).",
    ),
    out: Path = typer.Option(None, "--out", help="JSON de salida. Default: data/eval/..."),
):
    """
    Calcula las metricas de exito de propagacion y las escribe a JSON.

    Solo lectura. A diferencia de antes, correrlo durante una resolucion ya
    no es un problema de bloqueo --PostgreSQL admite lectores concurrentes--
    aunque las cifras saldran de un estado a medio resolver.
    """
    conn = open_source()
    try:
        desconocidos = [m for m in methods if m not in CLEEResolver.METHODS]
        if desconocidos:
            raise typer.BadParameter(
                f"Metodos desconocidos: {desconocidos}. Opciones: {list(CLEEResolver.METHODS)}"
            )

        reporte: dict = {
            "db": settings.POSTGRES_DB,
            "computed_at": datetime.now().isoformat(),
            "metodos": {},
        }

        for method in methods:
            column, source = RESOLVED_COLUMN, metodo_source(method)
            logger.info(f"Analizando {method}...")
            periods = compute_period_metrics(conn, column, source)
            resumen = summarize_periods(periods)

            bloque: dict = {
                "por_periodo": periods.to_dicts(),
                "resumen": resumen,
            }
            nacimientos: dict[str, int] = {}
            colisiones_periodo: dict[str, int] = {}
            if identity_metrics:
                ident = compute_identity_metrics(conn, column, source)
                bloque.update(ident)
                nacimientos = {
                    n["period"]: n["identidades_nacidas"] for n in ident["nacimientos_sinteticos"]
                }
                colisiones_periodo = {
                    c["period"]: c["filas_excedentes"] for c in ident["colisiones"]["por_periodo"]
                }
            reporte["metodos"][method] = bloque

            _log_period_table(method, periods, nacimientos, colisiones_periodo)
            p = resumen["propagados"]
            logger.info(
                f"   EXITO DE PROPAGACION ({method}): "
                f"{resumen['tasa_vinculacion_propagados'] * 100:.2f}% "
                f"({p['vinculadas']:,} de {p['total']:,} filas sin CLEE oficial "
                f"quedaron con identidad real de INEGI; {p['sinteticas']:,} sinteticas)"
            )
            if identity_metrics:
                i = bloque["identidades"]
                c = bloque["colisiones"]
                logger.info(
                    f"   Identidades: {i['total']:,} ({i['reales']:,} reales, "
                    f"{i['sinteticas']:,} sinteticas); {i['tasa_longitudinal'] * 100:.2f}% "
                    f"cubren 2+ periodos (promedio {i['periodos_promedio']:.2f})"
                )
                logger.info(
                    f"   Colisiones intra-periodo: {c['identidades_con_colision']:,} "
                    f"identidades ({c['tasa_identidades_con_colision'] * 100:.4f}%), "
                    f"{c['filas_excedentes']:,} filas excedentes"
                )

        if agreement and len(methods) >= 2:
            ac = compute_agreement(
                conn, "identidad_a", "identidad_b", comparacion_source(methods[0], methods[1])
            )
            reporte["acuerdo"] = ac
            logger.info(
                f"Acuerdo {methods[0]} vs {methods[1]}: "
                f"{ac['tasa_acuerdo_semantico'] * 100:.4f}% semantico "
                f"({ac['identicas']:,} identicas + {ac['ambas_sinteticas']:,} ambas "
                f"sinteticas de {ac['total']:,}); {ac['real_distinta']:,} con CLEE real "
                f"distinto, {ac['real_vs_sintetica']:,} real-vs-sintetica"
            )
    finally:
        conn.close()

    STATS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = out or STATS_DIR / f"stats_{settings.POSTGRES_DB}.json"
    out_path.write_text(json.dumps(reporte, indent=2, default=str))
    logger.info(f"✅ Metricas escritas en {out_path}")


if __name__ == "__main__":
    app()
