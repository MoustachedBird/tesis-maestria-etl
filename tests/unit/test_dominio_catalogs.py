"""Tests de los catalogos de dominio y su linkeo con establecimientos.

Cubre la normalizacion de los antiguos tipos ENUM de DuckDB:
  - Los catalogos denue_tipos_* se siembran desde los enums (fuente unica)
    y coinciden con DOMAIN_CATALOGS (la validacion fail-fast del ingest).
  - Los relationships del ORM (Establecimiento.estrato / .tipo_unidad /
    .tipo_vialidad / .tipo_asentamiento / .actividad) navegan a los
    catalogos via JOIN, sin FK fisico en la tabla de hechos.
  - validate_integrity() rechaza valores fuera de dominio (la garantia de
    integridad referencial del ingest).
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from src_etl.db.base import Base
from src_etl.db.models import (
    ActividadEconomica,
    Establecimiento,
    EstratoPersonal,
    TipoAsentamiento,
    TipoUnidadEconomica,
    TipoVialidad,
)
from src_etl.loaders.dominios import build_rows
from src_etl.loaders.estratos import build_rows as build_estrato_rows
from src_etl.utils.denue_schema import DOMAIN_CATALOGS


@pytest.mark.unit
class TestDominioSeeds:
    """Los catalogos derivan de los enums y cubren el dominio del ingest."""

    def test_row_counts(self):
        rows = build_rows()
        assert len(rows["denue_tipos_vialidad"]) == 39
        assert len(rows["denue_tipos_asentamiento"]) == 45
        assert len(rows["denue_tipos_unidad_economica"]) == 3

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

    def test_tipounieco_has_semantics(self):
        """La semantica FIJO/SEMIFIJO se materializa como descripcion."""
        rows = {r["tipounieco"]: r["descripcion"] for r in build_rows()["denue_tipos_unidad_economica"]}
        assert "permanente" in rows["FIJO"]
        assert "temporal" in rows["SEMIFIJO"]


@pytest.fixture()
def linked_session(tmp_path):
    """Sesion ORM sobre DuckDB scratch con catalogos sembrados y un
    establecimiento que referencia cada dominio."""
    engine = create_engine(f"duckdb:///{tmp_path}/linked.duckdb")
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [EstratoPersonal(**r) for r in build_estrato_rows()]
            + [TipoVialidad(**r) for r in build_rows()["denue_tipos_vialidad"]]
            + [TipoAsentamiento(**r) for r in build_rows()["denue_tipos_asentamiento"]]
            + [TipoUnidadEconomica(**r) for r in build_rows()["denue_tipos_unidad_economica"]]
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
        session.add(
            Establecimiento(
                id="test-estab-1",
                nom_estab="CAFE TEST",
                codigo_act="722515",
                per_ocu="11 A 30 PERSONAS",
                tipo_vial="CALLE",
                tipo_asent="COLONIA",
                tipounieco="FIJO",
                period=date(2024, 11, 1),
            )
        )
        session.commit()
        yield session

    engine.dispose()


@pytest.mark.unit
class TestEstablecimientoLinks:
    """Relationships navegables desde Establecimiento a cada catalogo."""

    def test_estrato_link(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.estrato is not None
        assert estab.estrato.categoria == "Small"
        assert (estab.estrato.empleados_min, estab.estrato.empleados_max) == (11, 30)

    def test_actividad_link(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.actividad.sector == "72"
        assert estab.actividad.rama == "7225"

    def test_tipo_links(self, linked_session):
        estab = linked_session.get(Establecimiento, "test-estab-1")
        assert estab.tipo_vialidad.tipo_vial == "CALLE"
        assert estab.tipo_asentamiento.tipo_asent == "COLONIA"
        assert "permanente" in estab.tipo_unidad.descripcion

    def test_links_are_none_when_column_null(self, linked_session):
        linked_session.add(Establecimiento(id="test-estab-2", period=date(2024, 11, 1)))
        linked_session.commit()
        estab = linked_session.get(Establecimiento, "test-estab-2")
        assert estab.estrato is None
        assert estab.tipo_vialidad is None
        assert estab.tipo_unidad is None


@pytest.mark.unit
class TestIngestDomainGuard:
    """validate_integrity() es la garantia de integridad del ingest."""

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
                "tipo_vial": ["CALLE DE RODAJE", "GLORIETA"],  # valores nuevos del dominio
                "tipounieco": ["FIJO", "SEMIFIJO"],
            }
        )
        writer.validate_integrity(df)  # no debe lanzar
