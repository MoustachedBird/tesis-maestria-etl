"""Validacion de la Fase 3 contra un PostgreSQL real.

Todo lo que la Fase 3 construyo --particiones, COPY, staging, indices-- se
verifico compilando DDL y corriendo el suite, pero **nunca toco un
servidor**. Este script es lo que cierra esa brecha. No es un test de
pytest a proposito: los 2 bloques marcados `postgres` siguen escritos
contra DuckDB y migrarlos es trabajo aparte; esto valida el codigo nuevo
hoy, sin esperar a esa migracion.

Corre sobre la base de pruebas (`TEST_POSTGRES_URL`, o `<POSTGRES_DB>_test`),
nunca sobre la real, y **recrea el schema `silver` desde cero** en cada
corrida.

    uv run python -m scripts.smoke_fase3
"""

from __future__ import annotations

from datetime import date

import polars as pl
from sqlalchemy import create_engine, text
from tests.conftest import get_test_postgres_url

from src.etl.db.indexes import (
    drop_indexes,
    ensure_indexes,
    existing_indexes,
    expected_indexes,
    missing_indexes,
)
from src.shared.db.base import Base, tables_for
from src.shared.db.copy import copy_dataframe
from src.shared.db.models import silver  # noqa: F401  (puebla el metadata)
from src.shared.db.partitions import (
    detach_partition,
    drop_partition,
    existing_partitions,
    partition_name,
)
from src.shared.db.staging import StagingValidationError, staging_partition

#: Cortes de juguete. Dos, para que el test de nombres de indice por
#: particion tenga algo contra que chocar si la cota de 63 estuviera mal.
PERIODS = [date(2024, 11, 1), date(2025, 3, 1)]

#: Filas por corte. Chico a proposito: esto valida semantica, no volumen.
ROWS = 1_000

_fallos: list[str] = []


def check(condicion: bool, descripcion: str) -> None:
    """Registra el resultado de una comprobacion sin abortar la corrida.

    Se sigue adelante tras un fallo para que una sola pasada diga *todo* lo
    que esta mal, en vez de obligar a un ciclo de arreglar-y-reintentar.

    Args:
        condicion: resultado de la comprobacion.
        descripcion: que se estaba comprobando.
    """
    print(f"  {'OK  ' if condicion else 'FALLA'}  {descripcion}")
    if not condicion:
        _fallos.append(descripcion)


def muestra(period: date, rows: int) -> pl.DataFrame:
    """DataFrame de prueba con los casos que importan.

    Incluye a proposito un NULL y una cadena vacia en la misma columna: es
    la hipotesis central de `copy.py` y la unica que no se podia verificar
    sin servidor.

    Args:
        period: corte al que pertenecen las filas.
        rows: cuantas filas generar.

    Returns:
        pl.DataFrame: filas listas para COPY.
    """
    nombres: list[str | None] = [f"Negocio {i}" for i in range(rows)]
    nombres[0] = None  # NULL
    nombres[1] = ""  # cadena vacia -- debe sobrevivir distinta de NULL
    return pl.DataFrame(
        {
            "id": [f"{period.isoformat()}-{i:06d}" for i in range(rows)],
            "period": [period] * rows,
            "nom_estab": nombres,
            "latitud": [19.4 + i / 1e6 for i in range(rows)],
            "longitud": [-99.1 - i / 1e6 for i in range(rows)],
        }
    )


def recrear_schema(engine) -> None:
    """Deja `silver` vacio y con el esquema del modelo.

    Args:
        engine: engine sobre la base de pruebas.
    """
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA IF EXISTS silver CASCADE"))
        conn.execute(text("CREATE SCHEMA silver"))
    Base.metadata.create_all(engine, tables=tables_for("silver"))


def probar_particionado(engine) -> None:
    """La tabla padre nace particionada y con la PK compuesta."""
    print("\n[1] Particionado")
    with engine.connect() as conn:
        relkind = conn.execute(
            text(
                "SELECT relkind FROM pg_class c JOIN pg_namespace n "
                "ON n.oid = c.relnamespace "
                "WHERE n.nspname='silver' AND c.relname='establecimientos'"
            )
        ).scalar_one()
        check(relkind == "p", "establecimientos es tabla particionada (relkind='p')")

        pk = conn.execute(
            text(
                "SELECT a.attname FROM pg_index i "
                "JOIN pg_attribute a ON a.attrelid=i.indrelid "
                "AND a.attnum = ANY(i.indkey) "
                "WHERE i.indrelid='silver.establecimientos'::regclass AND i.indisprimary "
                "ORDER BY a.attname"
            )
        ).scalars().all()
        check(pk == ["id", "period"], f"PK compuesta (id, period); se obtuvo {pk}")


def probar_carga(engine) -> None:
    """El camino feliz: staging -> COPY -> ATTACH, por cada corte."""
    print("\n[2] staging + COPY + ATTACH (camino feliz)")
    for period in PERIODS:
        df = muestra(period, ROWS)
        with engine.begin() as conn:
            with staging_partition(conn, period) as target:
                escritas = copy_dataframe(conn, df, target.table, target.schema)
            check(escritas == ROWS, f"{period}: COPY escribio {ROWS} filas")

    with engine.connect() as conn:
        parts = existing_partitions(conn)
        esperadas = sorted(partition_name(p) for p in PERIODS)
        check(parts == esperadas, f"particiones enganchadas: {parts}")

        total = conn.execute(
            text("SELECT COUNT(*) FROM silver.establecimientos")
        ).scalar_one()
        check(total == ROWS * len(PERIODS), f"la tabla padre ve {total} filas")

        # El pruning: una consulta por periodo debe tocar una sola particion.
        plan = conn.execute(
            text(
                "EXPLAIN SELECT COUNT(*) FROM silver.establecimientos "
                f"WHERE period = DATE '{PERIODS[0].isoformat()}'"
            )
        ).scalars().all()
        tocadas = [ln for ln in plan if "establecimientos_" in ln]
        check(len(tocadas) == 1, f"pruning: la consulta toca 1 particion ({len(tocadas)})")


def probar_nulls(engine) -> None:
    """NULL y cadena vacia sobreviven al COPY como valores distintos."""
    print("\n[3] NULL contra cadena vacia (la hipotesis de copy.py)")
    with engine.connect() as conn:
        nulos = conn.execute(
            text("SELECT COUNT(*) FROM silver.establecimientos WHERE nom_estab IS NULL")
        ).scalar_one()
        vacios = conn.execute(
            text("SELECT COUNT(*) FROM silver.establecimientos WHERE nom_estab = ''")
        ).scalar_one()
        check(nulos == len(PERIODS), f"NULLs preservados: {nulos} (uno por corte)")
        check(vacios == len(PERIODS), f"cadenas vacias preservadas: {vacios} (una por corte)")


def probar_rollback(engine) -> None:
    """Si la carga falla a la mitad, Silver no cambia."""
    print("\n[4] Fallo a media carga (el motivo de existir de staging.py)")
    antes = _contar(engine)
    period = date(2026, 1, 1)

    class Explota(RuntimeError):
        """Simula el OOM de §1.2."""

    try:
        with engine.begin() as conn:
            with staging_partition(conn, period) as target:
                copy_dataframe(conn, muestra(period, 10), target.table, target.schema)
                raise Explota("OOM simulado a media carga")
    except Explota:
        pass

    check(_contar(engine) == antes, "las filas de Silver no cambiaron")
    with engine.connect() as conn:
        check(
            partition_name(period) not in existing_partitions(conn),
            "no quedo particion a medias",
        )

    # Y la validacion: un corte cuyas filas no son del periodo se rechaza
    # ANTES de tocar la tabla padre.
    try:
        with engine.begin() as conn:
            with staging_partition(conn, period) as target:
                copy_dataframe(
                    conn, muestra(date(2030, 7, 1), 5), target.table, target.schema
                )
        check(False, "la validacion rechaza filas de otro periodo")
    except StagingValidationError:
        check(True, "la validacion rechaza filas de otro periodo")

    check(_contar(engine) == antes, "Silver sigue intacta tras el rechazo")


def probar_indices(engine) -> None:
    """drop/ensure, el contrato de suspended_indexes, y el camino concurrente."""
    print("\n[5] Indices sobre pg_indexes")
    with engine.begin() as conn:
        presentes = existing_indexes(conn)
        check(
            set(expected_indexes()) <= presentes,
            f"create_all dejo los {len(expected_indexes())} indices declarados",
        )
        soltados = drop_indexes(conn)
        check(len(soltados) == len(expected_indexes()), f"drop solto {len(soltados)}")
        check(len(missing_indexes(conn)) == len(expected_indexes()), "quedan todos faltantes")
        creados = ensure_indexes(conn)
        check(len(creados) == len(expected_indexes()), f"ensure recreo {len(creados)}")
        check(missing_indexes(conn) == [], "no falta ninguno tras ensure")

    # CONCURRENTLY no corre dentro de una transaccion.
    print("    -- camino concurrently=True (ON ONLY + attach por particion)")
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        drop_indexes(conn)
        creados = ensure_indexes(conn, concurrently=True)
        check(len(creados) == len(expected_indexes()), f"concurrently recreo {len(creados)}")
        check(missing_indexes(conn) == [], "no falta ninguno tras ensure concurrente")
        invalidos = conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname='silver' AND NOT i.indisvalid"
            )
        ).scalar_one()
        check(invalidos == 0, f"ningun indice quedo invalido ({invalidos})")


def probar_ciclo_particion(engine) -> None:
    """detach conserva los datos; drop se los lleva."""
    print("\n[6] Ciclo de vida de una particion")
    period = PERIODS[0]
    antes = _contar(engine)
    with engine.begin() as conn:
        detach_partition(conn, period)
    check(_contar(engine) == antes - ROWS, "detach saca las filas de la tabla padre")

    with engine.connect() as conn:
        sueltas = conn.execute(
            text(f"SELECT COUNT(*) FROM silver.{partition_name(period)}")
        ).scalar_one()
    check(sueltas == ROWS, "la tabla desenganchada conserva sus filas")

    with engine.begin() as conn:
        drop_partition(conn, period)
    print("    (particion eliminada)")


def _contar(engine) -> int:
    """Filas en la tabla de hechos.

    Args:
        engine: engine sobre la base de pruebas.

    Returns:
        int: total de filas.
    """
    with engine.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM silver.establecimientos")).scalar_one()


def main() -> int:
    """Corre todas las comprobaciones y reporta.

    Returns:
        int: 0 si todo paso, 1 si algo fallo.
    """
    url = get_test_postgres_url()
    print(f"Base de pruebas: {url.rsplit('@', 1)[-1]}")  # sin credenciales
    engine = create_engine(url)

    recrear_schema(engine)
    probar_particionado(engine)
    probar_carga(engine)
    probar_nulls(engine)
    probar_rollback(engine)
    probar_indices(engine)
    probar_ciclo_particion(engine)
    engine.dispose()

    print()
    if _fallos:
        print(f"FALLARON {len(_fallos)} comprobaciones:")
        for f in _fallos:
            print(f"  - {f}")
        return 1
    print("Todas las comprobaciones pasaron. La Fase 3 corre contra Postgres.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
