"""Tests for the SESNSP loader/transformer (crime incidence ingestion)."""

from pathlib import Path

import duckdb
import polars as pl
import pytest

from src_etl.loaders.sesnsp import (
    SEED_DELITOS_FILE,
    ensure_tables,
    load_delitos_catalog,
    sync_delitos_seed,
    sync_municipios_from_seed,
)
from src_etl.transformers.sesnsp import (
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


@pytest.mark.unit
class TestDelitosSeed:
    """sync_delitos_seed(): asignacion de ids append-only."""

    def test_first_sync_creates_seed_sorted(self, tmp_path):
        catalog = sync_delitos_seed(_sample_delitos(), tmp_path)

        assert catalog.height == 2
        assert catalog["id"].to_list() == [1, 2]
        # Orden determinista por clave natural: "El patrimonio" < "La vida..."
        assert catalog.row(0)[1] == "El patrimonio"
        assert (tmp_path / SEED_DELITOS_FILE).exists()

    def test_resync_is_idempotent(self, tmp_path):
        sync_delitos_seed(_sample_delitos(), tmp_path)
        catalog = sync_delitos_seed(_sample_delitos(), tmp_path)
        assert catalog.height == 2

    def test_new_delito_appends_without_renumbering(self, tmp_path):
        first = sync_delitos_seed(_sample_delitos(), tmp_path)
        nuevo = pl.DataFrame(
            {
                "bien_juridico": ["La libertad personal"],
                "tipo": ["Secuestro"],
                "subtipo": ["Secuestro"],
                "modalidad": ["Secuestro extorsivo"],
            }
        )
        catalog = sync_delitos_seed(nuevo, tmp_path)

        assert catalog.height == 3
        # Ids existentes intactos, el nuevo al final con max+1
        assert catalog.head(2).equals(first)
        assert catalog.row(2)[0] == 3


@pytest.mark.unit
class TestLoadDelitosCatalog:
    """load_delitos_catalog(): carga append-only del seed a la tabla."""

    def test_loads_and_is_idempotent(self, tmp_path):
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        ensure_tables(conn)
        sync_delitos_seed(_sample_delitos(), tmp_path)

        assert load_delitos_catalog(conn, tmp_path) == 2
        assert load_delitos_catalog(conn, tmp_path) == 0

    def test_missing_seed_raises(self, tmp_path):
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        ensure_tables(conn)
        with pytest.raises(FileNotFoundError):
            load_delitos_catalog(conn, tmp_path)


@pytest.mark.unit
class TestSyncMunicipios:
    """sync_municipios_from_seed(): inserta solo lo faltante."""

    def test_inserts_missing_rows_only(self, tmp_path):
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        pl.DataFrame(
            {
                "cve_ent": ["01", "09"],
                "cve_mun": ["001", "998"],
                "municipio": ["AGUASCALIENTES", "NO ESPECIFICADO"],
            }
        ).write_parquet(tmp_path / "seed_municipios.parquet")
        conn.execute("INSERT INTO denue_entidades VALUES ('09', 'Ciudad de México')")

        assert sync_municipios_from_seed(conn, tmp_path) == 1
        assert sync_municipios_from_seed(conn, tmp_path) == 0


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
        """Ejecuta la carga completa sobre una BD en memoria."""
        conn = duckdb.connect()
        _bootstrap_geography(conn)
        ensure_tables(conn)
        csv_path = tmp_path / "corte.csv"
        _write_sample_csv(csv_path, encoding=encoding)

        processor = SesnspProcessor.__new__(SesnspProcessor)
        processor.seeds_dir = tmp_path
        new_delitos, rows = processor._load_csv(conn, csv_path)
        assert new_delitos == 2
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
        csv_path = tmp_path / "corte.csv"
        _write_sample_csv(csv_path)

        processor = SesnspProcessor.__new__(SesnspProcessor)
        processor.seeds_dir = tmp_path
        processor._load_csv(conn, csv_path)
        _, rows = processor._load_csv(conn, csv_path)  # segundo corte, mismo archivo

        assert rows == 24  # sin duplicados: rebuild, no append
