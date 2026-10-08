"""Tests for the SESNSP loader/transformer (crime incidence ingestion)."""

from pathlib import Path

import duckdb
import polars as pl
import pytest

from src.etl.loaders.sesnsp import (
    SEED_DELITOS_FILE,
    build_delitos_rows,
    delitos_seed_path,
    ensure_tables,
    validate_delitos_del_csv,
    validate_municipios_del_csv,
)
from src.etl.transformers.sesnsp import (
    SesnspProcessor,
    cut_sort_key,
    detect_csv_encoding,
)

# Header real del CSV consolidado del SESNSP (formato ancho)
CSV_HEADER = (
    "Año,Clave_Ent,Entidad,Cve. Municipio,Municipio,Bien jurídico afectado,"
    "Tipo de delito,Subtipo de delito,Modalidad,Enero,Febrero,Marzo,Abril,"
    "Mayo,Junio,Julio,Agosto,Septiembre,Octubre,Noviembre,Diciembre"
)


def _sample_delitos() -> pl.DataFrame:
    """Dos combinaciones distintas de la jerarquia del delito."""
    return pl.DataFrame(
        {
            "bien_juridico": ["La vida y la Integridad corporal", "El patrimonio"],
            "tipo": ["Homicidio", "Robo"],
            "subtipo": ["Homicidio doloso", "Robo a negocio"],
            "modalidad": ["Con arma de fuego", "Con violencia"],
        }
    )


def _write_sample_csv(path: Path, encoding: str = "latin-1") -> None:
    """CSV ancho minimo: 1 municipio x 2 delitos x 1 anio."""
    rows = [
        CSV_HEADER,
        "2020,1,Aguascalientes,1001,Aguascalientes,La vida y la Integridad corporal,"
        "Homicidio,Homicidio doloso,Con arma de fuego,2,0,1,0,0,0,0,0,0,0,0,3",
        "2020,1,Aguascalientes,1001,Aguascalientes,El patrimonio,"
        "Robo,Robo a negocio,Con violencia,5,4,0,0,0,0,0,0,0,0,0,1",
    ]
    path.write_text("\n".join(rows), encoding=encoding)


def _bootstrap_geography(conn: duckdb.DuckDBPyConnection) -> None:
    """Catalogos geograficos minimos para satisfacer las FKs."""
    conn.execute("CREATE TABLE denue_entidades (cve_ent VARCHAR PRIMARY KEY, entidad VARCHAR)")
    conn.execute(
        "CREATE TABLE denue_municipios (cve_ent VARCHAR, cve_mun VARCHAR, municipio VARCHAR, "
        "PRIMARY KEY (cve_ent, cve_mun))"
    )
    conn.execute("INSERT INTO denue_entidades VALUES ('01', 'Aguascalientes')")
    conn.execute("INSERT INTO denue_municipios VALUES ('01', '001', 'AGUASCALIENTES')")


def _seed_delitos(tmp_path: Path) -> Path:
    """Escribe un seed del catalogo con las 2 combinaciones de muestra."""
    catalogo = _sample_delitos().with_row_index("id", offset=1).with_columns(
        pl.col("id").cast(pl.Int32)
    )
    path = tmp_path / SEED_DELITOS_FILE
    catalogo.write_parquet(path)
    return path


def _tabla_delitos_sembrada(conn: duckdb.DuckDBPyConnection, tmp_path: Path) -> None:
    """Crea sesnsp_delitos y la siembra desde el seed, como la migracion."""
    from src.etl.loaders.sesnsp import SESNSP_DELITOS_DDL

    _seed_delitos(tmp_path)
    conn.execute(SESNSP_DELITOS_DDL)
    for row in build_delitos_rows(tmp_path):
        conn.execute(
            "INSERT INTO sesnsp_delitos VALUES (?, ?, ?, ?, ?)",
            [row["id"], row["bien_juridico"], row["tipo"], row["subtipo"], row["modalidad"]],
        )


@pytest.mark.unit
class TestDelitosSeedEsFuenteDeVerdad:
    """El catalogo sale del parquet; el transform ya no lo descubre."""

    def test_rows_come_from_the_parquet(self, tmp_path):
        _seed_delitos(tmp_path)
        rows = build_delitos_rows(tmp_path)
        assert [r["id"] for r in rows] == [1, 2]
        assert {r["tipo"] for r in rows} == {"Homicidio", "Robo"}

    def test_missing_seed_fails_loudly(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Seed requerido"):
            delitos_seed_path(tmp_path)

    def test_seed_malformado_se_detecta(self, tmp_path):
        """Un id repetido viola la PK del catalogo y debe detenerse."""
        catalogo = _sample_delitos().with_columns(pl.lit(1, dtype=pl.Int32).alias("id"))
        catalogo.select(["id", *_sample_delitos().columns]).write_parquet(
            tmp_path / SEED_DELITOS_FILE
        )
        with pytest.raises(ValueError, match="repetido"):
            build_delitos_rows(tmp_path)


@pytest.mark.unit
class TestLinkeoDelCorte:
    """Cada corte anual se valida contra los catalogos, como un negocio."""

    def test_delitos_del_catalogo_pasan(self, tmp_path):
        conn = duckdb.connect()
        _tabla_delitos_sembrada(conn, tmp_path)
        validate_delitos_del_csv(conn, _sample_delitos())  # no debe lanzar

    def test_delito_nuevo_se_reporta_para_darlo_de_alta(self, tmp_path):
        conn = duckdb.connect()
        _tabla_delitos_sembrada(conn, tmp_path)
        nuevo = pl.DataFrame(
            {
                "bien_juridico": ["La libertad personal"],
                "tipo": ["Secuestro"],
                "subtipo": ["Secuestro extorsivo"],
                "modalidad": ["Secuestro extorsivo"],
            }
        )
        with pytest.raises(ValueError, match="fuera del catalogo sesnsp_delitos"):
            validate_delitos_del_csv(conn, nuevo)

    def test_municipio_del_catalogo_pasa(self):
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        claves = pl.DataFrame({"cve_ent": ["01"], "cve_mun": ["001"]})
        validate_municipios_del_csv(conn, claves)  # no debe lanzar

    def test_municipio_ausente_se_reporta(self):
        """Incluye los sinteticos 998/999 si no estan en el seed geografico."""
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        claves = pl.DataFrame({"cve_ent": ["01"], "cve_mun": ["998"]})
        with pytest.raises(ValueError, match="fuera del catalogo denue_municipios"):
            validate_municipios_del_csv(conn, claves)


@pytest.mark.unit
class TestCutSortKey:
    """cut_sort_key(): el corte del NOMBRE manda sobre el mtime."""

    def _touch(self, path: Path) -> Path:
        """Crea un archivo vacio (el mtime queda al momento de creacion)."""
        path.write_text("")
        return path

    def test_newer_cut_in_name_beats_newer_mtime(self, tmp_path):
        import os
        import time

        manual = self._touch(tmp_path / "Municipal-Delitos - (2015-2025) dic25_corte_jun26.csv")
        mirror = self._touch(tmp_path / "IDM_NM_dic25.csv")
        # El espejo se descargo DESPUES (mtime mayor)...
        now = time.time()
        os.utime(manual, (now - 3600, now - 3600))
        os.utime(mirror, (now, now))
        # ...pero el corte jun26 del nombre manual gana
        assert max([manual, mirror], key=cut_sort_key) == manual

    def test_max_token_wins_within_name(self, tmp_path):
        # dic25 (fin de datos) + jun26 (corte): usa jun26
        path = self._touch(tmp_path / "algo_dic25_corte_jun26.csv")
        assert cut_sort_key(path)[0].isoformat() == "2026-06-01"

    def test_four_digit_year(self, tmp_path):
        path = self._touch(tmp_path / "Municipal-Delitos-2015-2025_jun2026.csv")
        assert cut_sort_key(path)[0].isoformat() == "2026-06-01"

    def test_no_token_falls_back_to_mtime(self, tmp_path):
        import os
        import time

        old = self._touch(tmp_path / "sin_token_a.csv")
        new = self._touch(tmp_path / "sin_token_b.csv")
        now = time.time()
        os.utime(old, (now - 3600, now - 3600))
        os.utime(new, (now, now))
        assert cut_sort_key(old)[0] == cut_sort_key(new)[0]  # ambos date.min
        assert max([old, new], key=cut_sort_key) == new

    def test_equal_cut_tiebreak_by_mtime(self, tmp_path):
        import os
        import time

        a = self._touch(tmp_path / "IDM_NM_jun26.csv")
        b = self._touch(tmp_path / "Municipal-Delitos_corte_jun26.csv")
        now = time.time()
        os.utime(a, (now, now))
        os.utime(b, (now - 3600, now - 3600))
        assert max([a, b], key=cut_sort_key) == a


@pytest.mark.unit
class TestDetectEncoding:
    """detect_csv_encoding(): utf-8 vs latin-1."""

    def test_latin1(self, tmp_path):
        path = tmp_path / "l1.csv"
        _write_sample_csv(path, encoding="latin-1")
        assert detect_csv_encoding(path) == "latin-1"

    def test_utf8(self, tmp_path):
        path = tmp_path / "u8.csv"
        _write_sample_csv(path, encoding="utf-8")
        assert detect_csv_encoding(path) == "utf-8"


@pytest.mark.unit
class TestLoadCsv:
    """SesnspProcessor._load_csv(): unpivot ancho -> largo con FKs."""

    def _run(self, tmp_path, encoding: str) -> duckdb.DuckDBPyConnection:
        """Ejecuta la carga completa sobre una BD en memoria.

        El catalogo de delitos se siembra antes desde el parquet, como hace
        `make db-migrate`: el transform ya no lo descubre del CSV.
        """
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        ensure_tables(conn)
        _tabla_delitos_sembrada(conn, tmp_path)
        csv_path = tmp_path / "corte.csv"
        _write_sample_csv(csv_path, encoding=encoding)

        processor = SesnspProcessor.__new__(SesnspProcessor)
        processor.seeds_dir = tmp_path
        new_delitos, rows = processor._load_csv(conn, csv_path)
        assert new_delitos == 0  # el catalogo no se modifica aqui
        assert rows == 24  # 2 filas anchas x 12 meses
        return conn

    def test_unpivot_values_and_dates(self, tmp_path):
        conn = self._run(tmp_path, "latin-1")

        assert conn.execute(
            """
            SELECT carpetas FROM sesnsp_incidencia_municipal i
            JOIN sesnsp_delitos d ON d.id = i.delito_id
            WHERE d.subtipo = 'Homicidio doloso' AND i.fecha = DATE '2020-01-01'
            """
        ).fetchone() == (2,)
        assert conn.execute(
            """
            SELECT carpetas FROM sesnsp_incidencia_municipal i
            JOIN sesnsp_delitos d ON d.id = i.delito_id
            WHERE d.subtipo = 'Robo a negocio' AND i.fecha = DATE '2020-12-01'
            """
        ).fetchone() == (1,)

    def test_geographic_keys_zero_padded(self, tmp_path):
        conn = self._run(tmp_path, "latin-1")
        assert conn.execute(
            "SELECT DISTINCT cve_ent, cve_mun FROM sesnsp_incidencia_municipal"
        ).fetchall() == [("01", "001")]

    def test_utf8_csv_loads_identically(self, tmp_path):
        conn = self._run(tmp_path, "utf-8")
        total = conn.execute("SELECT SUM(carpetas) FROM sesnsp_incidencia_municipal").fetchone()[0]
        assert total == 2 + 1 + 3 + 5 + 4 + 1

    def test_rebuild_replaces_previous_load(self, tmp_path):
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        ensure_tables(conn)
        _tabla_delitos_sembrada(conn, tmp_path)
        csv_path = tmp_path / "corte.csv"
        _write_sample_csv(csv_path)

        processor = SesnspProcessor.__new__(SesnspProcessor)
        processor.seeds_dir = tmp_path
        processor._load_csv(conn, csv_path)
        _, rows = processor._load_csv(conn, csv_path)  # segundo corte, mismo archivo

        assert rows == 24  # sin duplicados: rebuild, no append
