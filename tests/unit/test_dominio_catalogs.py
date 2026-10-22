"""Tests de los catalogos de dominio y su linkeo por FKs reales.

Cubre la normalizacion a esquema relacional por ids:
  - Catalogos con surrogate id, label UNIQUE y descripcion poblada;
    contenido leido de los parquet de data/seeds.
  - Los ids salen del seed parquet (fuente de verdad), no de enums.
  - Relationships FK-driven en Establecimiento navegan a los catalogos.
  - Los FOREIGN KEYs son reales: un id fuera de catalogo se rechaza.

El fail-fast por etiquetas del ingest vive en test_catalogs.py.
"""

from datetime import date

import polars as pl
import pytest
from sqlalchemy.orm import Session

from src.etl.loaders.dominios import build_rows
from src.etl.loaders.estratos import build_rows as build_estrato_rows
from src.etl.utils.denue_schema import DOMAIN_ID_COLUMNS, DOMAIN_VALUE_ALIASES
from src.shared.db.models import (
    ActividadEconomica,
    Establecimiento,
    EstratoPersonal,
    TipoVialidad,
)
from src.shared.db.models import (
    TipoAsentamiento as TipoAsentamientoCat,
)
from src.shared.db.models import (
    TipoUnidadEconomica as TipoUnidadEconomicaCat,
)
from src.shared.db.partitions import create_partition

#: Corte al que pertenecen los establecimientos de prueba.
PERIODO = date(2024, 11, 1)


def _seed_id(table: str, label_col: str, label: str) -> int:
    """Id de un valor de catalogo segun el seed parquet (fuente de verdad)."""
    rows = build_estrato_rows() if table == "denue_estratos_personal" else build_rows()[table]
    return next(r["id"] for r in rows if r[label_col] == label)


@pytest.mark.unit
class TestDominioSeeds:
    """Los catalogos salen del parquet: ids unicos + descripcion poblada."""

    def test_row_counts(self):
        rows = build_rows()
        assert len(rows["denue_tipos_vialidad"]) == 39
        assert len(rows["denue_tipos_asentamiento"]) == 45
        assert len(rows["denue_tipos_unidad_economica"]) == 3

    def test_ids_are_unique_and_stable(self):
        """Los ids salen del parquet y son unicos: los FKs de millones de
        filas dependen de ellos, por eso el catalogo es append-only."""
        for table, rows in build_rows().items():
            ids = [r["id"] for r in rows]
            assert len(ids) == len(set(ids)), f"ids duplicados en {table}"
            assert all(isinstance(i, int) and i > 0 for i in ids)
        estratos = build_estrato_rows()
        assert [r["id"] for r in estratos] == sorted({r["id"] for r in estratos})

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
def linked_session(silver_engine):
    """Sesion con los catalogos sembrados y un establecimiento que los liga."""
    engine = silver_engine

    # `establecimientos` esta particionada: sin la particion del corte, el
    # INSERT falla con "no partition of relation found for row".
    with engine.begin() as conn:
        create_partition(conn, PERIODO)

    with Session(engine) as session:
        session.add_all(
            [EstratoPersonal(**r) for r in build_estrato_rows()]
            + [TipoVialidad(**r) for r in build_rows()["denue_tipos_vialidad"]]
            + [TipoAsentamientoCat(**r) for r in build_rows()["denue_tipos_asentamiento"]]
            + [TipoUnidadEconomicaCat(**r) for r in build_rows()["denue_tipos_unidad_economica"]]
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
                per_ocu_id=_seed_id("denue_estratos_personal", "per_ocu", "11 A 30 PERSONAS"),
                tipo_vial_id=_seed_id("denue_tipos_vialidad", "tipo_vial", "CALLE"),
                tipo_asent_id=_seed_id("denue_tipos_asentamiento", "tipo_asent", "COLONIA"),
                tipounieco_id=_seed_id("denue_tipos_unidad_economica", "tipounieco", "FIJO"),
                period=PERIODO,
            )
        )
        session.commit()
        yield session

    engine.dispose()


@pytest.mark.postgres
@pytest.mark.unit
class TestEstablecimientoLinks:
    """Relationships FK-driven navegables desde Establecimiento."""

    def test_estrato_link(self, linked_session):
        estab = linked_session.get(Establecimiento, ("test-estab-1", PERIODO))
        assert estab.estrato is not None
        assert estab.estrato.categoria == "Small"
        assert (estab.estrato.empleados_min, estab.estrato.empleados_max) == (11, 30)

    def test_actividad_link(self, linked_session):
        estab = linked_session.get(Establecimiento, ("test-estab-1", PERIODO))
        assert estab.actividad.sector == "72"
        assert estab.actividad.rama == "7225"

    def test_tipo_links_resuelven_labels(self, linked_session):
        estab = linked_session.get(Establecimiento, ("test-estab-1", PERIODO))
        assert estab.tipo_vialidad.tipo_vial == "CALLE"
        assert estab.tipo_asentamiento.tipo_asent == "COLONIA"
        assert "permanente" in estab.tipo_unidad.descripcion
        assert estab.tipo_vialidad.descripcion.strip()

    def test_links_are_none_when_fk_null(self, linked_session):
        linked_session.add(Establecimiento(id="test-estab-2", period=PERIODO))
        linked_session.commit()
        estab = linked_session.get(Establecimiento, ("test-estab-2", PERIODO))
        assert estab.estrato is None
        assert estab.tipo_vialidad is None
        assert estab.tipo_unidad is None

    def test_fk_real_rechaza_id_invalido(self, linked_session):
        """El FK es fisico: un id fuera de catalogo debe fallar al insertar."""
        from sqlalchemy.exc import DBAPIError

        linked_session.add(Establecimiento(id="test-estab-bad", per_ocu_id=999, period=PERIODO))
        with pytest.raises(DBAPIError):
            linked_session.commit()
        linked_session.rollback()

    def test_fk_real_rechaza_codigo_act_invalido(self, linked_session):
        from sqlalchemy.exc import DBAPIError

        linked_session.add(
            Establecimiento(id="test-estab-bad2", codigo_act="999999", period=PERIODO)
        )
        with pytest.raises(DBAPIError):
            linked_session.commit()
        linked_session.rollback()


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
        from src.etl.utils.denue_schema import DOMAIN_VALUE_ALIASES as aliases

        df = pl.DataFrame({"tipo_asent": ["OTRO", "COLONIA"]})
        out = df.with_columns(pl.col("tipo_asent").replace(aliases["tipo_asent"]))
        assert out["tipo_asent"].to_list() == ["OTRA (ESPECIFICAR)", "COLONIA"]


@pytest.mark.unit
class TestSeedParquets:
    """Los parquet de data/seeds son la fuente de verdad del catalogo."""

    def test_seed_files_exist_and_are_versioned(self):
        from src.etl.loaders.dominios import TABLES, seed_path
        from src.etl.loaders.estratos import seed_path as estrato_seed_path

        for table, *_ in TABLES:
            assert seed_path(table).exists()
        assert estrato_seed_path().exists()

    def test_missing_seed_fails_loudly(self, tmp_path):
        """Sin parquet no hay dominio: debe fallar claro, no silencioso."""
        from src.etl.loaders.dominios import seed_path

        with pytest.raises(FileNotFoundError, match="Seed requerido"):
            seed_path("denue_tipos_vialidad", tmp_path)

    def test_rows_come_from_the_parquet(self):
        """build_rows refleja el archivo, sin lista paralela en Python."""
        import polars as pl

        from src.etl.loaders.dominios import seed_path

        rows = build_rows()["denue_tipos_asentamiento"]
        assert rows == pl.read_parquet(seed_path("denue_tipos_asentamiento")).to_dicts()


@pytest.mark.unit
class TestEntityValueNormalization:
    """Nombres de entidad federativa unificados al canonico.

    Regresion: el mapa es {valor: canonico} y se aplicaba recorriendolo
    como si fuera {columna: mapa}, asi que el `if col in df.columns` era
    siempre falso y la normalizacion nunca corria.
    """

    def _clean(self, df):
        """Replica el paso de normalizacion de processor._clean_raw_data."""
        from src.etl.utils.denue_schema import ENTITY_VALUE_NORMALIZATION

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
        from src.etl.utils.denue_schema import ENTITY_VALUE_NORMALIZATION

        assert "DISTRITO FEDERAL" in ENTITY_VALUE_NORMALIZATION
        assert all(isinstance(v, str) for v in ENTITY_VALUE_NORMALIZATION.values())

    def test_noop_when_column_absent(self):
        df = pl.DataFrame({"cve_ent": ["09"]})
        assert self._clean(df).equals(df)
