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
from src_etl.utils.denue_schema import DOMAIN_ID_COLUMNS, DOMAIN_VALUE_ALIASES


@pytest.mark.unit
class TestDominioSeeds:
    """Los catalogos derivan de los enums: ids deterministas + descripcion."""

    def test_row_counts(self):
        rows = build_rows()
        assert len(rows["denue_tipos_vialidad"]) == 39
        assert len(rows["denue_tipos_asentamiento"]) == 45
        assert len(rows["denue_tipos_unidad_economica"]) == 3

    def test_ids_are_enum_positions(self):
        """domain_id() debe coincidir EXACTO con los ids sembrados."""
        rows = build_rows()
        for row, member in zip(rows["denue_tipos_vialidad"], TipoVial):
            assert row["id"] == domain_id(member)
            assert row["tipo_vial"] == member.value
        assert domain_id(TipoUnidadEconomica.FIJO) == 2
        assert domain_id(PersonalOcupado.PERSONAS_11_30) == 4

    def test_domain_id_columns_cover_every_catalog(self):
        """DOMAIN_ID_COLUMNS es lo que usa la validacion del ingest para
        saber que tabla consultar: debe cubrir los 4 dominios."""
        assert set(DOMAIN_ID_COLUMNS) == {"tipo_vial", "tipo_asent", "tipounieco", "per_ocu"}
        tables = {t for _id, t, _label in DOMAIN_ID_COLUMNS.values()}
        assert tables == {
            "denue_tipos_vialidad",
            "denue_tipos_asentamiento",
            "denue_tipos_unidad_economica",
            "denue_estratos_personal",
        }

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

    def _conn_with_catalogs(self, tmp_path):
        """Writer + conexion con los catalogos de dominio ya sembrados.

        La validacion ahora consulta la BD, asi que el guard necesita las
        tablas presentes (seeds generados en tmp_path, sin tocar los del
        repo)."""
        from src_etl.loaders.denue import EstablecimientoWriter
        from src_etl.loaders.dominios import load_seeds as load_dominios_seeds
        from src_etl.loaders.estratos import load_seeds as load_estratos_seeds

        writer = EstablecimientoWriter(tmp_path / "guard.duckdb")
        conn = writer._get_connection()
        load_dominios_seeds(conn, tmp_path)
        load_estratos_seeds(conn, tmp_path)
        return writer, conn

    def test_rejects_out_of_domain_value(self, tmp_path):
        writer, conn = self._conn_with_catalogs(tmp_path)
        df = pl.DataFrame({"per_ocu": ["0 A 5 PERSONAS", "VALOR INVENTADO"]})
        with pytest.raises(ValueError, match="per_ocu"):
            writer.validate_integrity(conn, df)

    def test_accepts_domain_and_nulls(self, tmp_path):
        writer, conn = self._conn_with_catalogs(tmp_path)
        df = pl.DataFrame(
            {
                "per_ocu": ["0 A 5 PERSONAS", None],
                "tipo_vial": ["CALLE DE RODAJE", "GLORIETA"],
                "tipounieco": ["FIJO", "SEMIFIJO"],
            }
        )
        writer.validate_integrity(conn, df)  # no debe lanzar

    def test_new_catalog_row_accepted_without_code_change(self, tmp_path):
        """El punto del refactor: un valor de dominio nuevo se habilita
        agregandolo a la TABLA (via seed), sin tocar codigo Python."""
        writer, conn = self._conn_with_catalogs(tmp_path)
        df = pl.DataFrame({"tipo_asent": ["INVENTADO NUEVO"]})
        with pytest.raises(ValueError, match="tipo_asent"):
            writer.validate_integrity(conn, df)

        conn.execute(
            "INSERT INTO denue_tipos_asentamiento VALUES "
            "(999, 'INVENTADO NUEVO', 'alta de un corte nuevo')"
        )
        writer.validate_integrity(conn, df)  # ahora pasa, sin cambiar codigo


@pytest.mark.unit
class TestDomainValueAliases:
    """Sinonimos de INEGI que se unifican al valor canonico del catalogo."""

    def test_otro_is_alias_of_otra(self):
        assert DOMAIN_VALUE_ALIASES["tipo_asent"]["OTRO"] == "OTRA (ESPECIFICAR)"

    def test_alias_target_exists_in_catalog(self):
        """Todo canonico de un alias debe existir en el dominio sembrado."""
        rows = build_rows()
        labels = {r["tipo_asent"] for r in rows["denue_tipos_asentamiento"]}
        for canonical in DOMAIN_VALUE_ALIASES["tipo_asent"].values():
            assert canonical in labels

    def test_cleaning_maps_otro_to_canonical(self):
        """La limpieza convierte OTRO -> OTRA (ESPECIFICAR) antes de validar."""
        from src_etl.utils.denue_schema import DOMAIN_VALUE_ALIASES as aliases

        df = pl.DataFrame({"tipo_asent": ["OTRO", "COLONIA"]})
        out = df.with_columns(pl.col("tipo_asent").replace(aliases["tipo_asent"]))
        assert out["tipo_asent"].to_list() == ["OTRA (ESPECIFICAR)", "COLONIA"]


@pytest.mark.unit
class TestSeedParquets:
    """Los seeds se materializan como parquet (patron geografia/SCIAN); el
    ARCHIVO manda: los enums solo hacen el bootstrap inicial."""

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

    def test_existing_parquet_is_never_overwritten(self, tmp_path):
        """El parquet es la fuente de verdad: un valor agregado a mano
        sobrevive, para poder dar de alta dominios nuevos sin tocar codigo."""
        from src_etl.loaders.dominios import SEED_FILES, ensure_seed_parquets

        ensure_seed_parquets(tmp_path)
        path = tmp_path / SEED_FILES["denue_tipos_asentamiento"]
        extended = pl.concat(
            [
                pl.read_parquet(path),
                pl.DataFrame(
                    {"id": [999], "tipo_asent": ["NUEVO"], "descripcion": ["corte nuevo"]}
                ),
            ]
        )
        extended.write_parquet(path)

        ensure_seed_parquets(tmp_path)
        after = pl.read_parquet(path)
        assert after.height == extended.height
        assert "NUEVO" in after["tipo_asent"].to_list()


@pytest.mark.unit
class TestEntityValueNormalization:
    """Nombres de entidad federativa unificados al canonico.

    Regresion: el mapa es {valor: canonico} y se aplicaba recorriendolo
    como si fuera {columna: mapa}, asi que el `if col in df.columns` era
    siempre falso y la normalizacion nunca corria.
    """

    def _clean(self, df):
        """Replica el paso de normalizacion de processor._clean_raw_data."""
        from src_etl.utils.denue_schema import ENTITY_VALUE_NORMALIZATION

        if "entidad" in df.columns:
            df = df.with_columns(pl.col("entidad").replace(ENTITY_VALUE_NORMALIZATION))
        return df

    def test_normalizes_known_aliases(self):
        df = pl.DataFrame({"entidad": ["DISTRITO FEDERAL", "DF", "MÉXICO", "JALISCO"]})
        assert self._clean(df)["entidad"].to_list() == [
            "CIUDAD DE MÉXICO",
            "CIUDAD DE MÉXICO",
            "ESTADO DE MÉXICO",
            "JALISCO",
        ]

    def test_map_is_keyed_by_value_not_column(self):
        """Sus claves son valores del dato: recorrerlo por columna no aplica."""
        from src_etl.utils.denue_schema import ENTITY_VALUE_NORMALIZATION

        assert "DISTRITO FEDERAL" in ENTITY_VALUE_NORMALIZATION
        assert all(isinstance(v, str) for v in ENTITY_VALUE_NORMALIZATION.values())

    def test_noop_when_column_absent(self):
        df = pl.DataFrame({"cve_ent": ["09"]})
        assert self._clean(df).equals(df)
