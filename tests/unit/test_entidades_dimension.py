"""Tests for the entidades_economicas dimension builder (N3)."""

import duckdb
import pytest

from src_etl.loaders.entidades import TABLE_NAME, build_entidades_economicas


def _make_observations(conn: duckdb.DuckDBPyConnection) -> None:
    """Tabla establecimientos minima (solo columnas que consume el builder).

    Escenario: un negocio con CLEE visto en 3 periodos (con mudanza de CP
    en el ultimo), uno sintetico NEW_* visto una vez, y una fila sin
    resolver (clee NULL) que debe quedar fuera.
    """
    conn.execute("""
        CREATE TABLE establecimientos (
            id VARCHAR, clee_propagado_radio VARCHAR, period DATE,
            nom_estab VARCHAR, raz_social VARCHAR, codigo_act VARCHAR,
            cve_ent VARCHAR, cve_mun VARCHAR, cod_postal VARCHAR,
            latitud DOUBLE, longitud DOUBLE, h3_cell_9 BIGINT,
            fecha_alta_date DATE
        )
    """)
    conn.execute("""
        INSERT INTO establecimientos VALUES
        ('a1', '14TAC123', DATE '2019-11-01', 'TACOS EL GUERO', 'GUERO SA',
         '722514', '14', '039', '44100', 20.66, -103.35, 617700169958293503,
         DATE '2010-07-01'),
        ('a2', '14TAC123', DATE '2021-05-01', 'TACOS EL GUERO', 'GUERO SA',
         '722514', '14', '039', '44100', 20.66, -103.35, 617700169958293503,
         DATE '2010-07-01'),
        ('a3', '14TAC123', DATE '2022-11-01', 'TACOS EL GUERO SA', 'GUERO SA',
         '722514', '14', '039', '44200', 20.67, -103.36, 617700169958293504,
         DATE '2009-01-01'),
        ('b1', 'NEW_14H3-abc123', DATE '2016-01-01', 'MISCELANEA LUPITA', NULL,
         '461110', '14', '120', '45500', 20.50, -103.20, 617700169958290000,
         DATE '2015-03-01'),
        ('c1', NULL, DATE '2016-01-01', 'SIN RESOLVER', NULL,
         '461110', '14', '120', '45500', 20.51, -103.21, 617700169958290001,
         DATE '2015-04-01')
    """)


@pytest.mark.unit
class TestBuildEntidadesEconomicas:
    """build_entidades_economicas(): agregacion observaciones -> dimension."""

    def _build(self) -> duckdb.DuckDBPyConnection:
        """Construye la dimension sobre el escenario minimo."""
        conn = duckdb.connect()
        _make_observations(conn)
        assert build_entidades_economicas(conn) == 2
        return conn

    def test_one_row_per_identity_and_null_excluded(self):
        conn = self._build()
        ids = [
            r[0]
            for r in conn.execute(
                f"SELECT clee_propagado_radio FROM {TABLE_NAME} ORDER BY 1"
            ).fetchall()
        ]
        assert ids == ["14TAC123", "NEW_14H3-abc123"]

    def test_lifespan_and_most_recent_attributes(self):
        conn = self._build()
        row = conn.execute(
            f"""SELECT primer_periodo, ultimo_periodo, n_periodos, cod_postal,
                       nom_estab, fecha_alta_date, es_sintetica
                FROM {TABLE_NAME} WHERE clee_propagado_radio = '14TAC123'"""
        ).fetchone()
        primer, ultimo, n, cp, nom, alta, sintetica = row
        assert (primer.isoformat(), ultimo.isoformat(), n) == ("2019-11-01", "2022-11-01", 3)
        # Atributos del periodo MAS RECIENTE (la mudanza de CP de 2022 gana)
        assert cp == "44200"
        assert nom == "TACOS EL GUERO SA"
        # fecha_alta_date = la MINIMA conocida entre observaciones
        assert alta.isoformat() == "2009-01-01"
        assert sintetica is False

    def test_synthetic_flag(self):
        conn = self._build()
        assert conn.execute(
            f"SELECT es_sintetica FROM {TABLE_NAME} WHERE clee_propagado_radio LIKE 'NEW_%'"
        ).fetchone() == (True,)

    def test_rebuild_is_idempotent(self):
        conn = self._build()
        assert build_entidades_economicas(conn) == 2  # rebuild total, sin duplicados

    def test_empty_resolution_yields_empty_dimension(self):
        conn = duckdb.connect()
        _make_observations(conn)
        conn.execute("UPDATE establecimientos SET clee_propagado_radio = NULL")
        assert build_entidades_economicas(conn) == 0
