"""
Carga y validacion de los seeds parquet: UN solo camino para todos.

Los cinco catalogos (geografia, SCIAN, dominios, estratos, delitos SESNSP)
hacen exactamente lo mismo: resolver la ruta del parquet, leerlo, validarlo
contra las restricciones de su tabla y sembrarlo. Antes cada loader tenia su
propia copia, con semanticas que habian divergido en silencio (uno releia el
archivo crudo en vez de las filas validadas; otro solo sembraba si la tabla
estaba vacia). Aqui vive la version unica.

Los parquet de `data/seeds` son la fuente de verdad de los catalogos, asi que
un archivo mal formado (id repetido, etiqueta duplicada, columna obligatoria
en NULL) es un error de datos que debe detenerse EN LA MIGRACION, con un
mensaje que diga archivo, columna y valores ofensores — no convertirse en un
error criptico de constraint a media carga, ni peor: desaparecer en silencio
porque la siembra usa INSERT OR IGNORE.

Las restricciones NO se declaran aqui: se leen del modelo ORM
(`Base.metadata`), que es la definicion canonica del esquema. Asi esta
validacion no puede desincronizarse de la tabla que valida.
"""

from pathlib import Path

import polars as pl
from sqlalchemy import UniqueConstraint, delete, insert, select


def _table(table_name: str):
    """Objeto Table del metadata del ORM para la tabla dada."""
    from src.shared.db.base import get_table
    from src.shared.db.models import Establecimiento  # noqa: F401  (puebla el metadata)

    return get_table(table_name)


def _unique_key_sets(table) -> list[tuple[str, ...]]:
    """Conjuntos de columnas que deben ser unicos: PK, UNIQUE de tabla y
    columnas marcadas unique=True."""
    keys: list[tuple[str, ...]] = []
    pk = tuple(c.name for c in table.primary_key.columns)
    if pk:
        keys.append(pk)
    for con in table.constraints:
        if isinstance(con, UniqueConstraint):
            cols = tuple(c.name for c in con.columns)
            if cols and cols not in keys:
                keys.append(cols)
    for col in table.columns:
        if col.unique and (col.name,) not in keys:
            keys.append((col.name,))
    return keys


def validate_seed(df: pl.DataFrame, table_name: str, source: str) -> pl.DataFrame:
    """
    Valida un seed contra las restricciones de su tabla; devuelve el df.

    Comprueba, en este orden: que existan las columnas obligatorias, que no
    haya NULLs donde la tabla exige NOT NULL, y que cada clave unica (PK,
    UNIQUE de tabla y columnas unique=True) no tenga repetidos.

    Args:
        df: Filas leidas del parquet.
        table_name: Tabla destino (para leer sus restricciones del ORM).
        source: Ruta/nombre del archivo, para el mensaje de error.

    Returns:
        El mismo DataFrame, si pasa todas las comprobaciones.

    Raises:
        ValueError: Con archivo, columna(s) y valores ofensores concretos.
    """
    table = _table(table_name)
    model_cols = {c.name for c in table.columns}

    faltantes = sorted(
        c.name
        for c in table.columns
        if not c.nullable and c.name not in df.columns and c.default is None
    )
    if faltantes:
        raise ValueError(
            f"❌ Seed invalido {source}: faltan columnas obligatorias de "
            f"{table_name}: {faltantes}"
        )

    if df.height == 0:
        raise ValueError(f"❌ Seed invalido {source}: no tiene filas")

    for col in table.columns:
        if col.nullable or col.name not in df.columns:
            continue
        nulos = df.filter(pl.col(col.name).is_null()).height
        if nulos:
            raise ValueError(
                f"❌ Seed invalido {source}: {nulos} fila(s) con "
                f"{table_name}.{col.name} en NULL (la tabla lo exige NOT NULL)"
            )

    for key in _unique_key_sets(table):
        cols = [c for c in key if c in df.columns]
        if len(cols) != len(key):
            continue
        dup = (
            df.group_by(cols)
            .len()
            .filter(pl.col("len") > 1)
            .sort(cols)
        )
        if dup.height:
            ejemplos = dup.head(5).select(cols).rows()
            raise ValueError(
                f"❌ Seed invalido {source}: {dup.height} valor(es) repetido(s) "
                f"en {table_name}({', '.join(cols)}), que la tabla exige UNIQUE. "
                f"Ejemplos: {ejemplos}"
            )

    extra = sorted(set(df.columns) - model_cols)
    if extra:
        raise ValueError(
            f"❌ Seed invalido {source}: columnas que no existen en "
            f"{table_name}: {extra}"
        )

    return df


# ---------------------------------------------------------------------------
# Resolucion de rutas
# ---------------------------------------------------------------------------


def default_seeds_dir() -> Path:
    """
    `data/seeds` del proyecto (settings.SEEDS_DIR).

    Import lazy de la config para no acoplar el modulo en import-time.
    """
    from src.shared.config import get_settings

    return Path(get_settings().SEEDS_DIR)


def seed_path(file_name: str, seeds_dir: Path | None = None) -> Path:
    """
    Ruta de un seed; falla claro si no esta versionado.

    Args:
        file_name: Nombre del parquet dentro del directorio de seeds.
        seeds_dir: Directorio (default: settings.SEEDS_DIR).

    Raises:
        FileNotFoundError: Con la ruta completa, si el archivo no existe.
    """
    path = (seeds_dir or default_seeds_dir()) / file_name
    if not path.exists():
        raise FileNotFoundError(f"Seed requerido no encontrado: {path}")
    return path


def read_seed(
    table_name: str,
    file_name: str,
    seeds_dir: Path | None = None,
    transform=None,
) -> pl.DataFrame:
    """
    Lee un seed parquet y lo valida contra las restricciones de su tabla.

    Es el UNICO punto por donde un seed entra al sistema: garantiza que lo
    que se valida es exactamente lo que despues se inserta.

    Args:
        table_name: Tabla destino (de ahi salen las restricciones).
        file_name: Nombre del parquet.
        seeds_dir: Directorio (default: settings.SEEDS_DIR).
        transform: Callable opcional df -> df aplicado ANTES de validar
            (p.ej. derivar la jerarquia SCIAN del codigo).

    Returns:
        DataFrame validado, listo para sembrar.
    """
    path = seed_path(file_name, seeds_dir)
    df = pl.read_parquet(path)
    if transform is not None:
        df = transform(df)
    return validate_seed(df, table_name, str(path))


# ---------------------------------------------------------------------------
# Siembra
# ---------------------------------------------------------------------------


def seed_duckdb(conn, table_name: str, df: pl.DataFrame) -> int:
    """
    Siembra via conexion DuckDB nativa; idempotente y append-only.

    `INSERT OR IGNORE` por PK: inserta solo lo que falta y nunca dropea ni
    trunca (los catalogos estan referenciados por FKs de las tablas de
    hechos, que bloquearian un DELETE).

    Returns:
        Numero de filas efectivamente insertadas.
    """
    cols = df.columns
    placeholders = ", ".join(["?"] * len(cols))
    return conn.executemany(
        f"INSERT OR IGNORE INTO {table_name} ({', '.join(cols)}) VALUES ({placeholders})",
        df.rows(),
    ).fetchone()[0]


def seed_sqlalchemy(
    connection, table_name: str, df: pl.DataFrame, truncate: bool = True
) -> int:
    """
    Siembra via conexion SQLAlchemy (migraciones Alembic).

    Las sentencias se construyen desde el objeto `Table` del modelo, no
    como texto: el schema, el entrecomillado y el bindeo de parametros los
    resuelve SQLAlchemy a partir de la misma definicion canonica que crea
    la tabla, asi que no pueden divergir de ella.

    Solo reescribe cuando el contenido de la tabla difiere del parquet, de
    modo que re-aplicar la migracion no toca filas que ya estan bien.

    Args:
        connection: SQLAlchemy connection.
        table_name: Tabla destino.
        df: Filas ya validadas.
        truncate: Si True, vacia la tabla antes de reescribir en caso de
            diferencia.

    Returns:
        Numero de filas del catalogo.
    """
    table = _table(table_name)
    columns = [table.c[name] for name in df.columns]

    existing = connection.execute(select(*columns)).fetchall()
    if [tuple(row) for row in existing] != df.rows():
        if truncate:
            connection.execute(delete(table))
        connection.execute(insert(table), df.to_dicts())
        print(f"   Loaded {df.height:,} rows into {table_name}")
    return df.height
