"""
Domain-type catalogs seed loader (tipo_vial / tipo_asent / tipounieco).

Seeds the `denue_tipos_vialidad`, `denue_tipos_asentamiento` and
`denue_tipos_unidad_economica` catalog tables from the Python enums in
`src_etl/db/enums.py` (single source of truth, same pattern as
`estratos.py` for per_ocu).

Surrogate keys: cada catalogo tiene `id INTEGER PRIMARY KEY` y el label
como UNIQUE. `establecimientos` referencia estos ids con FOREIGN KEYs
REALES (decision del usuario 2026-07-17: relaciones fisicas, no joins por
string). Los ids son DETERMINISTAS: posicion del valor en su enum
(1-based). Por eso los enums son append-only — reordenar o borrar valores
cambiaria ids ya referenciados por millones de filas.

Como los catalogos son referenciados por FKs, el reseed NO puede hacer
DELETE ciego: si el contenido ya coincide con los enums se salta; si
difiere y hay filas referenciandolo, DuckDB lo bloquea (correcto: un
cambio de dominio con datos vivos exige migracion consciente).

Seeds en parquet (mismo patron que geografia/SCIAN): las filas viven en
`data/seeds/seed_tipos_*.parquet` y los loaders cargan DESDE el parquet,
que es la FUENTE DE VERDAD del contenido. Los enums + DESCRIPCIONES_* solo
generan el archivo la primera vez (bootstrap) y aportan las referencias
semanticas que el codigo usa por nombre; un parquet existente NUNCA se
sobreescribe. Asi, un valor de dominio nuevo se agrega en un unico lugar
(el parquet -> la tabla) sin tocar codigo, y la validacion del ingest lo
lee de la BD.

Two entry points, same rows:
  - `load_seeds(conn, seeds_dir, force)`: native DuckDB (ingest bootstrap).
  - `load_seeds_sqlalchemy(connection)`: SQLAlchemy connection (Alembic).
"""

import logging
from pathlib import Path

import polars as pl
from sqlalchemy import Connection, text

from src_etl.db.enums import TipoAsentamiento, TipoUnidadEconomica, TipoVial

logger = logging.getLogger(__name__)

# (tabla, columna label, enum fuente, nombre para stats/logs)
TABLES = [
    ("denue_tipos_vialidad", "tipo_vial", TipoVial, "road_types"),
    ("denue_tipos_asentamiento", "tipo_asent", TipoAsentamiento, "settlement_types"),
    ("denue_tipos_unidad_economica", "tipounieco", TipoUnidadEconomica, "economic_unit_types"),
]
"""Catalogos de dominio que este loader administra, en orden de carga."""

TABLE_DDL = {
    "denue_tipos_vialidad": (
        "CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
        "tipo_vial VARCHAR NOT NULL UNIQUE, descripcion VARCHAR NOT NULL)"
    ),
    "denue_tipos_asentamiento": (
        "CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
        "tipo_asent VARCHAR NOT NULL UNIQUE, descripcion VARCHAR NOT NULL)"
    ),
    "denue_tipos_unidad_economica": (
        "CREATE TABLE {table} (id INTEGER PRIMARY KEY, "
        "tipounieco VARCHAR NOT NULL UNIQUE, descripcion VARCHAR NOT NULL)"
    ),
}
"""DDL por tabla (mismo esquema que los modelos ORM de models.py)."""

DESCRIPCIONES_VIALIDAD: dict[TipoVial, str] = {
    TipoVial.NINGUNO: "Sin tipo de vialidad especificado",
    TipoVial.SIN_REFERENCIA: "Domicilio sin referencia de vialidad",
    TipoVial.ANDADOR: "Via peatonal interior de conjuntos habitacionales o zonas centricas",
    TipoVial.AMPLIACION: "Extension del trazo de una vialidad existente",
    TipoVial.AVENIDA: "Vialidad amplia de circulacion primaria",
    TipoVial.BOULEVARD: "Avenida amplia con camellon central arbolado o ajardinado",
    TipoVial.CALLE: "Vialidad urbana basica de circulacion local",
    TipoVial.CALLEJON: "Via estrecha entre edificaciones, de paso local",
    TipoVial.CALZADA: "Vialidad amplia que conectaba la ciudad con poblados vecinos",
    TipoVial.CAMINO: "Via rural de transito vehicular, generalmente sin pavimentar",
    TipoVial.CARRETERA: "Via interurbana pavimentada de largo recorrido",
    TipoVial.CERRADA: "Calle con un solo acceso, sin salida",
    TipoVial.CIRCUITO: "Vialidad de trazo cerrado que rodea una zona o conjunto",
    TipoVial.CIRCUNVALACION: "Vialidad que rodea el perimetro de una ciudad o zona",
    TipoVial.CONTINUACION: "Tramo que continua una vialidad existente bajo otro nombre",
    TipoVial.CORREDOR: "Via urbana con vocacion comercial o de transporte definida",
    TipoVial.DIAGONAL: "Vialidad de trazo oblicuo respecto a la reticula urbana",
    TipoVial.EJE_VIAL: "Vialidad primaria de circulacion continua en reticula metropolitana",
    TipoVial.PASAJE: "Via de paso estrecha, frecuentemente peatonal o comercial",
    TipoVial.PEATONAL: "Via de uso exclusivo para peatones",
    TipoVial.PERIFERICO: "Anillo vial que bordea la mancha urbana",
    TipoVial.PLAZA: "Espacio publico abierto usado como referencia de domicilio",
    TipoVial.PRIVADA: "Calle de acceso restringido a residentes",
    TipoVial.PROLONGACION: "Extension de una vialidad que conserva su nombre",
    TipoVial.RETORNO: "Via corta que permite regresar a la vialidad principal",
    TipoVial.TERRACERIA: "Camino de tierra sin recubrimiento",
    TipoVial.VIADUCTO: "Vialidad elevada o deprimida de circulacion continua",
    TipoVial.AUTOPISTA: "Carretera de altas especificaciones con accesos controlados",
    TipoVial.BRECHA: "Camino rustico abierto para paso de vehiculos o personas",
    TipoVial.VEREDA: "Senda estrecha para paso de peatones o animales",
    TipoVial.CALLE_DE_RODAJE: "Via interna de aeropuerto para rodaje de aeronaves",
    TipoVial.PUENTE: "Estructura vial que cruza sobre un obstaculo natural o urbano",
    TipoVial.TUNEL: "Via subterranea de paso vehicular",
    TipoVial.PASO_A_DESNIVEL: "Cruce vial a distinto nivel de la vialidad principal",
    TipoVial.GLORIETA: "Interseccion circular de distribucion del transito",
    TipoVial.RAMPA: "Via inclinada de acceso o incorporacion entre vialidades",
    TipoVial.LIBRAMIENTO: "Via que rodea una poblacion para evitar atravesarla",
    TipoVial.COSTERA: "Vialidad paralela al litoral",
    TipoVial.OTRA: "Tipo de vialidad no incluido en el catalogo INEGI",
}
"""Seed generado: significado de cada tipo de vialidad INEGI (no existe
seed oficial descargable; redactado a partir de la terminologia urbana de
los catalogos INEGI). Debe cubrir TODOS los valores de TipoVial."""

DESCRIPCIONES_ASENTAMIENTO: dict[TipoAsentamiento, str] = {
    TipoAsentamiento.NINGUNO: "Sin tipo de asentamiento especificado",
    TipoAsentamiento.AEROPUERTO: "Instalacion aeroportuaria y su zona de servicios",
    TipoAsentamiento.AMPLIACION: "Extension de un asentamiento existente que conserva su nombre",
    TipoAsentamiento.BARRIO: "Asentamiento tradicional de origen historico dentro de una localidad",
    TipoAsentamiento.CANTON: "Caserio rural pequeno (denominacion regional)",
    TipoAsentamiento.CIUDAD: "Localidad urbana de gran tamano",
    TipoAsentamiento.CIUDAD_INDUSTRIAL: "Zona urbana planificada para uso industrial",
    TipoAsentamiento.COLONIA: "Asentamiento urbano con traza definida; unidad basica de la ciudad mexicana",
    TipoAsentamiento.CONDOMINIO: "Conjunto habitacional en regimen de propiedad condominal",
    TipoAsentamiento.CONJUNTO_HABITACIONAL: "Agrupacion planificada de viviendas construidas como unidad",
    TipoAsentamiento.CORREDOR_INDUSTRIAL: "Franja territorial destinada a instalaciones industriales",
    TipoAsentamiento.COTO: "Fraccionamiento cerrado con acceso controlado (denominacion regional)",
    TipoAsentamiento.CUARTEL: "Division territorial de origen militar o administrativo",
    TipoAsentamiento.EJIDO: "Nucleo agrario de propiedad social",
    TipoAsentamiento.EXHACIENDA: "Asentamiento sobre el casco de una antigua hacienda",
    TipoAsentamiento.FRACCION: "Porcion de un predio rustico subdividido",
    TipoAsentamiento.FRACCIONAMIENTO: "Desarrollo habitacional planificado con lotificacion autorizada",
    TipoAsentamiento.GRANJA: "Explotacion agropecuaria de pequena escala con vivienda",
    TipoAsentamiento.HACIENDA: "Finca rural de gran extension de origen colonial",
    TipoAsentamiento.INGENIO: "Asentamiento asociado a un ingenio azucarero",
    TipoAsentamiento.MANZANA: "Superficie delimitada por vialidades dentro de una localidad",
    TipoAsentamiento.PARAJE: "Lugar despoblado identificado como referencia rural",
    TipoAsentamiento.PARQUE_INDUSTRIAL: "Superficie planificada con infraestructura para industria",
    TipoAsentamiento.PRIVADA: "Conjunto habitacional de acceso restringido",
    TipoAsentamiento.PROLONGACION: "Extension de un asentamiento a lo largo de una vialidad",
    TipoAsentamiento.PUEBLO: "Localidad tradicional; nucleo historico o cabecera",
    TipoAsentamiento.PUERTO: "Asentamiento asociado a instalaciones portuarias",
    TipoAsentamiento.RANCHO: "Explotacion rural con vivienda aislada",
    TipoAsentamiento.RANCHERIA: "Caserio rural disperso, menor que un pueblo",
    TipoAsentamiento.REGION: "Demarcacion territorial amplia usada como referencia",
    TipoAsentamiento.RESIDENCIAL: "Desarrollo habitacional de perfil urbano medio o alto",
    TipoAsentamiento.RINCONADA: "Asentamiento pequeno en un repliegue del terreno o de la traza",
    TipoAsentamiento.SECCION: "Subdivision administrativa de un asentamiento mayor",
    TipoAsentamiento.SECTOR: "Division territorial urbana de caracter administrativo",
    TipoAsentamiento.SUPERMANZANA: "Agrupacion de manzanas planificada como unidad",
    TipoAsentamiento.UNIDAD: "Conjunto habitacional o administrativo construido como unidad",
    TipoAsentamiento.UNIDAD_HABITACIONAL: "Conjunto de vivienda multifamiliar, tipicamente de interes social",
    TipoAsentamiento.VILLA: "Localidad con rango historico entre pueblo y ciudad",
    TipoAsentamiento.ZONA_FEDERAL: "Franja de jurisdiccion federal (costas, cauces, vias)",
    TipoAsentamiento.ZONA_INDUSTRIAL: "Area urbana destinada predominantemente a la industria",
    TipoAsentamiento.ZONA_MILITAR: "Instalacion del Ejercito y su zona habitacional",
    TipoAsentamiento.ZONA_NAVAL: "Instalacion de la Armada y su zona habitacional",
    TipoAsentamiento.LOCALIDAD: "Lugar habitado reconocido con nombre y categoria propios",
    TipoAsentamiento.PARQUE: "Espacio verde publico usado como referencia de asentamiento",
    TipoAsentamiento.OTRA: "Tipo de asentamiento no incluido en el catalogo INEGI",
}
"""Seed generado: significado de cada tipo de asentamiento humano INEGI
(no existe seed oficial descargable). Debe cubrir TODOS los valores de
TipoAsentamiento."""


def build_rows() -> dict[str, list[dict]]:
    """
    Generate catalog rows from the enums (never duplicated by hand).

    Returns:
        Dict tabla -> lista de filas (dicts columna -> valor), ids 1-based
        en orden del enum.
    """
    descripciones = {
        "denue_tipos_vialidad": DESCRIPCIONES_VIALIDAD,
        "denue_tipos_asentamiento": DESCRIPCIONES_ASENTAMIENTO,
        "denue_tipos_unidad_economica": {m: m.get_descripcion() for m in TipoUnidadEconomica},
    }
    rows: dict[str, list[dict]] = {}
    for table, label_col, enum_cls, _display in TABLES:
        rows[table] = [
            {"id": i, label_col: member.value, "descripcion": descripciones[table][member]}
            for i, member in enumerate(enum_cls, start=1)
        ]
    return rows


def _rows_match(existing: list[tuple], expected: list[dict]) -> bool:
    """True si el contenido actual de la tabla ya es el esperado."""
    expected_tuples = [tuple(r.values()) for r in expected]
    return sorted(existing) == sorted(expected_tuples)


def _missing_appended_rows(existing: list[tuple], expected: list[dict]) -> list[dict] | None:
    """
    Filas por INSERTAR si el drift es una extension append-only del enum.

    Los enums son append-only y los catalogos estan referenciados por FKs
    con datos vivos: cuando INEGI agrega un valor nuevo (p.ej. tipo_asent
    'OTRO' en el corte 2026-05), el reseed por DELETE+INSERT fallaria
    porque DuckDB bloquea el DELETE de filas referenciadas. Si todo lo
    existente sigue siendo valido y solo hay filas nuevas al final, basta
    insertar las faltantes.

    Returns:
        Lista de filas faltantes si existing es subconjunto estricto de
        expected; None si el drift NO es append-only (requiere el camino
        de reseed completo / migracion consciente).
    """
    existing_set = set(existing)
    expected_tuples = {tuple(r.values()) for r in expected}
    if not existing_set <= expected_tuples:
        return None
    return [r for r in expected if tuple(r.values()) not in existing_set]


SEED_FILES = {table: f"seed_{table.removeprefix('denue_')}.parquet" for table, *_ in TABLES}
"""Archivo parquet por catalogo en data/seeds (patron de geografia/SCIAN):
seed_tipos_vialidad.parquet, seed_tipos_asentamiento.parquet,
seed_tipos_unidad_economica.parquet."""


def _default_seeds_dir() -> Path:
    """data/seeds del proyecto (settings.SEEDS_DIR); import lazy para no
    acoplar el modulo a la config en import-time."""
    from src_etl.core.config import get_settings

    return Path(get_settings().SEEDS_DIR)


def ensure_seed_parquets(seeds_dir: Path | None = None) -> dict[str, Path]:
    """
    Garantiza que exista cada `seed_tipos_*.parquet`; NO sobreescribe.

    El parquet es la FUENTE DE VERDAD del contenido del catalogo: si ya
    existe se respeta tal cual, de modo que agregar un valor de dominio
    nuevo (INEGI evoluciona sus etiquetas entre cortes) se hace en un solo
    lugar — el parquet — sin tocar codigo Python. Los enums solo sirven
    para el bootstrap inicial (cuando el archivo no existe) y para las
    referencias semanticas que el codigo necesita por nombre (p.ej.
    `TipoUnidadEconomica.FIJO` en `Establecimiento.is_fixed_establishment`).

    Returns:
        Dict tabla -> Path del parquet listo para cargar.
    """
    seeds_dir = seeds_dir or _default_seeds_dir()
    seeds_dir.mkdir(parents=True, exist_ok=True)
    rows_by_table = build_rows()
    paths: dict[str, Path] = {}

    for table, _label, _enum, display_name in TABLES:
        path = seeds_dir / SEED_FILES[table]
        if not path.exists():
            generated = pl.DataFrame(rows_by_table[table])
            generated.write_parquet(path)
            logger.info(f"   Seed generado desde enums: {path.name} ({len(generated)} filas)")
        paths[table] = path
    return paths


def _seed_rows(seeds_dir: Path | None, table: str) -> list[dict]:
    """Filas del catalogo leidas del parquet (garantizado al dia)."""
    paths = ensure_seed_parquets(seeds_dir)
    return pl.read_parquet(paths[table]).to_dicts()


def load_seeds(conn, seeds_dir: Path | None = None, force: bool = False) -> dict[str, int]:
    """
    Load domain catalogs via a native DuckDB connection (ingest bootstrap).

    Carga DESDE los parquet de data/seeds (regenerados desde los enums si
    faltan o difieren). Idempotente y seguro con FKs entrantes: crea tablas
    faltantes, salta las que ya coinciden y solo reseedea (delete + insert)
    cuando el contenido difiere (si hay filas referenciando, DuckDB bloquea
    el DELETE — un cambio de dominio con datos vivos exige migracion).

    Args:
        conn: DuckDB connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        force: If True, drop and recreate the tables first.

    Returns:
        dict: Row count per catalog (display name -> rows).
    """
    stats = {}

    for table, _label, _enum, display_name in TABLES:
        if force:
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

        if not conn.execute(
            f"SELECT 1 FROM information_schema.tables WHERE table_name = '{table}'"
        ).fetchone():
            conn.execute(TABLE_DDL[table].format(table=table))

        rows = _seed_rows(seeds_dir, table)
        cols = list(rows[0].keys())
        placeholders = ", ".join(["?"] * len(cols))
        insert_sql = f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({placeholders})"
        existing = conn.execute(f"SELECT {', '.join(cols)} FROM {table}").fetchall()
        if not _rows_match(existing, rows):
            missing = _missing_appended_rows(existing, rows)
            if missing is not None:
                conn.executemany(insert_sql, [[r[c] for c in cols] for r in missing])
                logger.info(f"   Appended {len(missing):,} new {display_name}")
            else:
                conn.execute(f"DELETE FROM {table}")
                conn.executemany(insert_sql, [[r[c] for c in cols] for r in rows])
                logger.info(f"   Loaded {len(rows):,} {display_name}")
        stats[display_name] = len(rows)

    return stats


def load_seeds_sqlalchemy(
    connection: Connection,
    seeds_dir: Path | None = None,
    truncate: bool = True,
) -> dict[str, int]:
    """
    Load domain catalogs via SQLAlchemy connection (for Alembic migrations).

    Carga DESDE los parquet de data/seeds (regenerados desde los enums si
    faltan o difieren). Creates missing tables (keeps older replay points
    valid, same pattern as estratos); reseeds only when the content differs
    (safe with incoming FKs).

    Args:
        connection: SQLAlchemy connection.
        seeds_dir: Directorio de seeds (default: settings.SEEDS_DIR).
        truncate: If True, delete existing rows before reseeding a table
            whose content differs.

    Returns:
        dict: Row count per catalog (display name -> rows).
    """
    stats = {}

    for table, _label, _enum, display_name in TABLES:
        table_exists = (
            connection.execute(
                text("SELECT COUNT(*) FROM information_schema.tables WHERE table_name = :table"),
                {"table": table},
            ).scalar()
            > 0
        )
        if not table_exists:
            connection.execute(text(TABLE_DDL[table].format(table=table)))

        rows = _seed_rows(seeds_dir, table)
        cols = list(rows[0].keys())
        params = ", ".join(f":{c}" for c in cols)
        insert_sql = text(f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({params})")
        existing = connection.execute(text(f"SELECT {', '.join(cols)} FROM {table}")).fetchall()
        if not _rows_match([tuple(r) for r in existing], rows):
            missing = _missing_appended_rows([tuple(r) for r in existing], rows)
            if missing is not None:
                connection.execute(insert_sql, missing)
                print(f"   Appended {len(missing):,} new {display_name}")
            else:
                if truncate:
                    connection.execute(text(f"DELETE FROM {table}"))
                connection.execute(insert_sql, rows)
                print(f"   Loaded {len(rows):,} {display_name}")
        stats[display_name] = len(rows)

    return stats
