"""Tests de los catalogos de dominio y su linkeo por FKs reales.

Cubre la normalizacion a esquema relacional por ids:
  - Catalogos con surrogate id (posicion 1-based en su enum), label UNIQUE
    y columna descripcion poblada; contenido derivado de los enums.
  - `domain_id()` coincide con los ids sembrados.
  - Relationships FK-driven en Establecimiento navegan a los catalogos.
  - Los FOREIGN KEYs son reales: DuckDB rechaza ids fuera de catalogo.
  - validate_integrity() sigue dando fail-fast por labels en el ingest.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src_etl.db.base import Base
from src_etl.db.enums import PersonalOcupado, TipoUnidadEconomica, TipoVial, domain_id
from src_etl.db.models import (
    ActividadEconomica,
    Establecimiento,
    EstratoPersonal,
    TipoVialidad,
)
from src_etl.db.models import (
    TipoAsentamiento as TipoAsentamientoCat,
)
from src_etl.db.models import (
    TipoUnidadEconomica as TipoUnidadEconomicaCat,
)
from src_etl.loaders.dominios import build_rows
from src_etl.loaders.estratos import build_rows as build_estrato_rows
from src_etl.utils.denue_schema import DOMAIN_CATALOGS


@pytest.mark.unit
class TestDominioSeeds:
    """Los catalogos derivan de los enums: ids deterministas + descripcion."""

    def test_row_counts(self):
        rows = build_rows()
        assert len(rows["denue_tipos_vialidad"]) == 39
        assert len(rows["denue_tipos_asentamiento"]) == 46
        assert len(rows["denue_tipos_unidad_economica"]) == 3

    def test_ids_are_enum_positions(self):
        """domain_id() debe coincidir EXACTO con los ids sembrados."""
        rows = build_rows()
        for row, member in zip(rows["denue_tipos_vialidad"], TipoVial):
            assert row["id"] == domain_id(member)
            assert row["tipo_vial"] == member.value
        assert domain_id(TipoUnidadEconomica.FIJO) == 2
        assert domain_id(PersonalOcupado.PERSONAS_11_30) == 4

    def test_seeds_match_ingest_domain(self):
        """Catalogo y validacion del ingest deben ser el MISMO dominio."""
        rows = build_rows()
        assert [r["tipo_vial"] for r in rows["denue_tipos_vialidad"]] == (
            DOMAIN_CATALOGS["tipo_vial"]
        )
        assert [r["tipo_asent"] for r in rows["denue_tipos_asentamiento"]] == (
            DOMAIN_CATALOGS["tipo_asent"]
        )
        assert [r["tipounieco"] for r in rows["denue_tipos_unidad_economica"]] == (
            DOMAIN_CATALOGS["tipounieco"]
        )
        assert [r["per_ocu"] for r in build_estrato_rows()] == DOMAIN_CATALOGS["per_ocu"]

    def test_all_rows_have_descripcion(self):
        """Todo valor de dominio lleva descripcion no vacia (seed generado)."""
        for rows in build_rows().values():
            for row in rows:
                assert row["descripcion"].strip(), f"sin descripcion: {row}"

    def test_estratos_have_ids(self):
        rows = build_estrato_rows()
        assert [r["id"] for r in rows] == list(range(1, 9))
        assert rows[3]["categoria"] == "Small"


@pytest.fixture()
def linked_session(tmp_path):
    """Sesion ORM sobre DuckDB scratch (create_all = esquema con FKs reales)
    con catalogos sembrados y un establecimiento referenciando cada uno."""
    engine = create_engine(f"duckdb:///{tmp_path}/linked.duckdb")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [EstratoPersonal(**r) for r in build_estrato_rows()]
            + [TipoVialidad(**r) for r in build_rows()["denue_tipos_vialidad"]]
            + [TipoAsentamientoCat(**r) for r in build_rows()["denue_tipos_asentamiento"]]
            + [
                TipoUnidadEconomicaCat(**r)
                for r in build_rows()["denue_tipos_unidad_economica"]
            ]
        )
        session.add(
            ActividadEconomica(
                codigo_act="722515",
                nombre_act="Cafeterias y similares",
                sector="72",
                subsector="722",
                rama="7225",
            )
        )
        session.commit()
        session.add(
            Establecimiento(
                id="test-estab-1",
                nom_estab="CAFE TEST",
                codigo_act="722515",
                per_ocu_id=domain_id(PersonalOcupado.PERSONAS_11_30),
                tipo_vial_id=domain_id(TipoVial.CALLE),
                tipo_asent_id=8,  # COLONIA (posicion 8 en TipoAsentamiento)
                tipounieco_id=domain_id(TipoUnidadEconomica.FIJO),
                period=date(2024, 11, 1),
            )
        )
        session.commit()
        yield session

    engine.dispose()


@pytest.mark.unit
class TestEstablecimientoLinks:
    """Relationships FK-driven navegables desde Establecimiento."""

    def test_estrato_link(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.estrato is not None
        assert estab.estrato.categoria == "Small"
        assert (estab.estrato.empleados_min, estab.estrato.empleados_max) == (11, 30)

    def test_actividad_link(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.actividad.sector == "72"
        assert estab.actividad.rama == "7225"

    def test_tipo_links_resuelven_labels(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.tipo_vialidad.tipo_vial == "CALLE"
        assert estab.tipo_asentamiento.tipo_asent == "COLONIA"
        assert "permanente" in estab.tipo_unidad.descripcion
        assert estab.tipo_vialidad.descripcion.strip()

    def test_links_are_none_when_fk_null(self, linked_session):
        linked_session.add(Establecimiento(id="test-estab-2", period=date(2024, 11, 1)))
        linked_session.commit()
        estab = linked_session.get(Establecimiento, "test-estab-2")
        assert estab.estrato is None
        assert estab.tipo_vialidad is None
        assert estab.tipo_unidad is None

    def test_fk_real_rechaza_id_invalido(self, linked_session):
        """El FK es fisico: un id fuera de catalogo debe fallar al insertar."""
        from sqlalchemy.exc import DBAPIError

        linked_session.add(
            Establecimiento(id="test-estab-bad", per_ocu_id=999, period=date(2024, 11, 1))
        )
        with pytest.raises(DBAPIError):
            linked_session.commit()
        linked_session.rollback()

    def test_fk_real_rechaza_codigo_act_invalido(self, linked_session):
        from sqlalchemy.exc import DBAPIError

        linked_session.add(
            Establecimiento(id="test-estab-bad2", codigo_act="999999", period=date(2024, 11, 1))
        )
        with pytest.raises(DBAPIError):
            linked_session.commit()
        linked_session.rollback()


@pytest.mark.unit
class TestIngestDomainGuard:
    """validate_integrity() sigue siendo el fail-fast por labels del ingest."""

    def _writer(self, tmp_path):
        from src_etl.loaders.denue import EstablecimientoWriter

        return EstablecimientoWriter(tmp_path / "guard.duckdb")

    def test_rejects_out_of_domain_value(self, tmp_path):
        writer = self._writer(tmp_path)
        df = pl.DataFrame({"per_ocu": ["0 A 5 PERSONAS", "VALOR INVENTADO"]})
        with pytest.raises(ValueError, match="per_ocu"):
            writer.validate_integrity(df)

    def test_accepts_domain_and_nulls(self, tmp_path):
        writer = self._writer(tmp_path)
        df = pl.DataFrame(
            {
                "per_ocu": ["0 A 5 PERSONAS", None],
                "tipo_vial": ["CALLE DE RODAJE", "GLORIETA"],
                "tipounieco": ["FIJO", "SEMIFIJO"],
            }
        )
        writer.validate_integrity(df)  # no debe lanzar


@pytest.mark.unit
class TestSeedParquets:
    """Los seeds se materializan como parquet (patron geografia/SCIAN) y
    los enums mandan: drift en el archivo -> se regenera."""

    def test_parquets_generated_and_match_enums(self, tmp_path):
        from src_etl.loaders.dominios import ensure_seed_parquets
        from src_etl.loaders.estratos import ensure_seed_parquet

        paths = ensure_seed_parquets(tmp_path)
        assert {p.name for p in paths.values()} == {
            "seed_tipos_vialidad.parquet",
            "seed_tipos_asentamiento.parquet",
            "seed_tipos_unidad_economica.parquet",
        }
        for table, path in paths.items():
            assert pl.read_parquet(path).to_dicts() == build_rows()[table]

        path_est = ensure_seed_parquet(tmp_path)
        assert path_est.name == "seed_estratos_personal.parquet"
        assert pl.read_parquet(path_est).to_dicts() == build_estrato_rows()

    def test_drifted_parquet_is_regenerated(self, tmp_path):
        from src_etl.loaders.dominios import SEED_FILES, ensure_seed_parquets

        ensure_seed_parquets(tmp_path)
        drifted = tmp_path / SEED_FILES["denue_tipos_vialidad"]
        pl.DataFrame(
            {"id": [1], "tipo_vial": ["FALSO"], "descripcion": ["drift"]}
        ).write_parquet(drifted)

        ensure_seed_parquets(tmp_path)
        assert pl.read_parquet(drifted).height == 39  # regenerado desde el enum
