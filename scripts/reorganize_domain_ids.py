"""
Script ONE-TIME: reorganiza `establecimientos` al esquema relacional por ids.

Reconstruye la tabla de hechos del Silver convirtiendo las columnas-label
de dominio (tipo_vial, tipo_asent, tipounieco, per_ocu) en FOREIGN KEYs
enteros reales (*_id) hacia los catalogos denue_tipos_* /
denue_estratos_personal, y anhade el FK de codigo_act al catalogo SCIAN
(DDL canonico: `loaders.denue.ESTABLECIMIENTOS_DDL`).

POR QUE UN SCRIPT Y NO UNA MIGRACION (limitaciones DuckDB verificadas):
  - ALTER TABLE ... ADD CONSTRAINT no existe: los FKs solo nacen con la
    tabla -> hay que reconstruirla.
  - ALTER DROP COLUMN esta bloqueado por los indices implicitos de los FKs
    de geografia (columnas posteriores) -> ni siquiera se pueden tirar las
    columnas-label in situ.
  - La copia de ~77M filas (~22GB) merece transacciones por periodo,
    progreso/ETA y verificacion de paridad antes del swap -- no una
    transaccion gigante de Alembic.

Flujo: precondiciones -> crear establecimientos_new (DDL final) -> INSERT
por periodo con JOIN label->id -> verificar paridad -> swap (drop+rename)
-> recrear indices de usuario (traducidos a *_id) -> CHECKPOINT.

Idempotente: si la tabla ya esta reorganizada, sale sin tocar nada; una
corrida fallida deja el original intacto (solo borra establecimientos_new).

Requisitos previos: `make db-migrate` (catalogos con ids sembrados,
revision c3a26327aec3). Correr con: `make reorganize-domain-ids`.
"""

import re
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src_etl.core.config import get_settings  # noqa: E402
from src_etl.core.logging import get_stage_logger  # noqa: E402
from src_etl.loaders.denue import ESTABLECIMIENTOS_DDL  # noqa: E402
from src_etl.utils.denue_schema import DOMAIN_ID_COLUMNS  # noqa: E402
from src_etl.utils.warehouse import connect_configured  # noqa: E402

logger = get_stage_logger(__name__, Path("./data/etl_reorganize_ids.log"))

OLD_TABLE = "establecimientos"
NEW_TABLE = "establecimientos_new"

_LABEL_COLS = list(DOMAIN_ID_COLUMNS)  # tipo_vial, tipo_asent, tipounieco, per_ocu

# Indices capturados del esquema viejo pueden referenciar columnas-label;
# al recrearlos sobre la tabla nueva se traducen a su columna *_id.
_INDEX_TRANSLATE = re.compile(r"\b(tipo_vial|tipo_asent|tipounieco|per_ocu)\b(?!_id)")


def _columns(conn, table: str) -> list[str]:
    """Columnas de una tabla en orden de definicion."""
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]


def _check_preconditions(conn, db_path: Path) -> bool:
    """Valida estado de la base y espacio en disco.

    Returns:
        True si hay trabajo por hacer; False si ya esta reorganizada.
    """
    tables = {r[0] for r in conn.execute("SHOW TABLES").fetchall()}
    if OLD_TABLE not in tables:
        raise SystemExit(f"❌ No existe la tabla {OLD_TABLE} en {db_path}")

    for _label, (_id_col, cat_table, _cat_label) in DOMAIN_ID_COLUMNS.items():
        if cat_table not in tables:
            raise SystemExit(
                f"❌ Falta el catalogo {cat_table}. Corre primero: make db-migrate"
            )
        if "id" not in _columns(conn, cat_table):
            raise SystemExit(
                f"❌ {cat_table} no tiene surrogate id. Corre primero: make db-migrate"
            )

    old_cols = _columns(conn, OLD_TABLE)
    if not any(c in old_cols for c in _LABEL_COLS):
        logger.info("La tabla ya esta reorganizada (sin columnas-label). Nada que hacer.")
        return False

    # Espacio: la reconstruccion duplica la tabla temporalmente.
    db_size = db_path.stat().st_size
    free = shutil.disk_usage(db_path.parent).free
    if free < int(db_size * 1.3):
        raise SystemExit(
            f"❌ Espacio insuficiente: base {db_size / 1e9:.1f}GB, "
            f"libre {free / 1e9:.1f}GB (se requiere ~1.3x la base)."
        )
    return True


def _capture_user_indexes(conn) -> list[tuple[str, str]]:
    """(nombre, sql traducido a *_id) de los indices de usuario del viejo."""
    rows = conn.execute(
        "SELECT index_name, sql FROM duckdb_indexes() "
        f"WHERE table_name = '{OLD_TABLE}' AND sql IS NOT NULL"
    ).fetchall()
    return [(name, _INDEX_TRANSLATE.sub(r"\1_id", sql)) for name, sql in rows]


def _build_select(conn) -> tuple[list[str], str, str]:
    """Construye el SELECT de copia con la conversion label -> id.

    Returns:
        (columnas destino, expresiones SELECT, clausulas JOIN)
    """
    old_cols = set(_columns(conn, OLD_TABLE))
    id_by_label = {label: cfg for label, cfg in DOMAIN_ID_COLUMNS.items()}
    id_cols = {cfg[0]: (label, cfg[1], cfg[2]) for label, cfg in id_by_label.items()}

    target_cols, select_exprs, joins = [], [], []
    for col in _columns(conn, NEW_TABLE):
        target_cols.append(col)
        if col in id_cols:
            label, cat_table, cat_label = id_cols[col]
            alias = f"cat_{label}"
            joins.append(f"LEFT JOIN {cat_table} {alias} ON e.{label} = {alias}.{cat_label}")
            select_exprs.append(f"{alias}.id AS {col}")
        elif col in old_cols:
            select_exprs.append(f"e.{col}")
        else:
            logger.warning(f"   Columna {col} no existe en la tabla vieja -> NULL")
            select_exprs.append(f"NULL AS {col}")

    return target_cols, ", ".join(select_exprs), " ".join(joins)


def _copy_rows(conn) -> int:
    """Copia por periodo (transacciones acotadas, progreso y ETA)."""
    periods = [
        r[0]
        for r in conn.execute(
            f"SELECT DISTINCT period FROM {OLD_TABLE} ORDER BY period"
        ).fetchall()
    ]
    total = conn.execute(f"SELECT COUNT(*) FROM {OLD_TABLE}").fetchone()[0]
    target_cols, select_sql, joins_sql = _build_select(conn)

    logger.info(f"Copiando {total:,} filas en {len(periods)} periodos...")
    done, t0 = 0, time.perf_counter()
    for i, period in enumerate(periods, start=1):
        t_p = time.perf_counter()
        conn.execute(
            f"INSERT INTO {NEW_TABLE} ({', '.join(target_cols)}) "
            f"SELECT {select_sql} FROM {OLD_TABLE} e {joins_sql} WHERE e.period = ?",
            [period],
        )
        n = conn.execute(
            f"SELECT COUNT(*) FROM {NEW_TABLE} WHERE period = ?", [period]
        ).fetchone()[0]
        done += n
        elapsed = time.perf_counter() - t0
        rate = done / elapsed if elapsed > 0 else 0
        eta_min = (total - done) / rate / 60 if rate > 0 else 0
        logger.info(
            f"   [{i}/{len(periods)}] {period}: {n:,} filas en "
            f"{time.perf_counter() - t_p:.1f}s | global {done:,}/{total:,} "
            f"({100 * done / total:.1f}%) ETA ~{eta_min:.0f} min"
        )
    return total


def _verify_parity(conn) -> None:
    """Paridad old vs new; aborta (sin tocar el original) si algo no cuadra."""
    n_old = conn.execute(f"SELECT COUNT(*) FROM {OLD_TABLE}").fetchone()[0]
    n_new = conn.execute(f"SELECT COUNT(*) FROM {NEW_TABLE}").fetchone()[0]
    if n_old != n_new:
        raise SystemExit(f"❌ Paridad de filas fallo: old={n_old:,} new={n_new:,}")

    for label, (id_col, _cat, _cl) in DOMAIN_ID_COLUMNS.items():
        with_label = conn.execute(
            f"SELECT COUNT(*) FROM {OLD_TABLE} WHERE {label} IS NOT NULL"
        ).fetchone()[0]
        with_id = conn.execute(
            f"SELECT COUNT(*) FROM {NEW_TABLE} WHERE {id_col} IS NOT NULL"
        ).fetchone()[0]
        if with_label != with_id:
            raise SystemExit(
                f"❌ Paridad de {label} fallo: labels={with_label:,} ids={with_id:,} "
                "(hay labels fuera de catalogo?)"
            )
        logger.info(f"   Paridad {label} -> {id_col}: {with_id:,} OK")
    logger.info(f"   Paridad total: {n_new:,} filas OK")


def main() -> None:
    """Ejecuta la reorganizacion completa (ver docstring del modulo)."""
    settings = get_settings()
    db_path = Path(settings.SILVER_DUCKDB_PATH)
    logger.info(f"Silver: {db_path} ({db_path.stat().st_size / 1e9:.1f}GB)")

    conn = connect_configured(db_path)
    try:
        if not _check_preconditions(conn, db_path):
            return

        indexes = _capture_user_indexes(conn)
        if indexes:
            logger.info(f"Respaldo de {len(indexes)} indice(s) de usuario (se recrean al final):")
            for _name, sql in indexes:
                logger.info(f"   {sql};")

        conn.execute(f"DROP TABLE IF EXISTS {NEW_TABLE}")
        conn.execute(ESTABLECIMIENTOS_DDL.format(table=NEW_TABLE))
        logger.info(f"{NEW_TABLE} creada con PK + 8 FKs reales.")

        t0 = time.perf_counter()
        total = _copy_rows(conn)
        logger.info(f"Copia completada: {total:,} filas en {(time.perf_counter() - t0) / 60:.1f} min")

        _verify_parity(conn)

        # Swap atomico a nivel de catalogo
        conn.execute(f"DROP TABLE {OLD_TABLE}")
        conn.execute(f"ALTER TABLE {NEW_TABLE} RENAME TO {OLD_TABLE}")
        logger.info("Swap completado: establecimientos ahora es la tabla relacional por ids.")

        for name, sql in indexes:
            t_i = time.perf_counter()
            conn.execute(sql)
            logger.info(f"   Indice {name} recreado en {time.perf_counter() - t_i:.1f}s")

        conn.execute("CHECKPOINT")
        logger.info(
            "Listo. Nota: el archivo .duckdb no encoge (los bloques liberados se "
            "reutilizan); para compactarlo fisicamente usar COPY FROM DATABASE a "
            "un archivo nuevo cuando convenga."
        )
    finally:
        conn.close()


if __name__ == "__main__":
    main()
