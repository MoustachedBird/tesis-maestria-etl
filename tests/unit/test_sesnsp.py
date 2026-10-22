"""Tests for the SESNSP loader/transformer (crime incidence ingestion)."""

from datetime import date
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import func, insert, select

from src.etl.loaders.sesnsp import (
    DELITOS_TABLE,
    SEED_DELITOS_FILE,
    build_delitos_rows,
    delitos_seed_path,
    validate_delitos_del_csv,
    validate_municipios_del_csv,
)
from src.etl.transformers.catalogs import CatalogoDesactualizado
from src.etl.transformers.sesnsp import (
    SesnspProcessor,
    cut_sort_key,
    detect_csv_encoding,
)
from src.shared.db.base import get_table

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


def _seed_delitos(tmp_path: Path) -> Path:
    """Escribe un seed del catalogo con las 2 combinaciones de muestra."""
    catalogo = (
        _sample_delitos().with_row_index("id", offset=1).with_columns(pl.col("id").cast(pl.Int32))
    )
    path = tmp_path / SEED_DELITOS_FILE
    catalogo.write_parquet(path)
    return path


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


@pytest.fixture
def catalogos(silver_engine, tmp_path):
    """Conexion con el catalogo de delitos y la geografia minima sembrados.

    Las tablas vienen del modelo --el mismo esquema que crea la migracion--
    y el catalogo de delitos, del seed, igual que en produccion.
    """
    _seed_delitos(tmp_path)
    with silver_engine.begin() as conn:
        conn.execute(insert(get_table(DELITOS_TABLE)), build_delitos_rows(tmp_path))
        conn.execute(
            insert(get_table("denue_entidades")),
            [{"cve_ent": "01", "entidad": "Aguascalientes"}],
        )
        conn.execute(
            insert(get_table("denue_municipios")),
            [{"cve_ent": "01", "cve_mun": "001", "municipio": "AGUASCALIENTES"}],
        )
        yield conn


@pytest.mark.postgres
@pytest.mark.unit
class TestLinkeoDelCorte:
    """Cada corte se valida contra los catalogos, como un negocio del DENUE."""

    def test_delitos_del_catalogo_pasan(self, catalogos):
        validate_delitos_del_csv(catalogos, _sample_delitos())

    def test_delito_nuevo_se_reporta_para_darlo_de_alta(self, catalogos):
        """El SESNSP agrega delitos entre cortes; hay que darlos de alta."""
        nuevo = pl.DataFrame(
            {
                "bien_juridico": ["La libertad personal"],
                "tipo": ["Secuestro"],
                "subtipo": ["Secuestro extorsivo"],
                "modalidad": ["Secuestro extorsivo"],
            }
        )
        with pytest.raises(CatalogoDesactualizado, match="sesnsp_delitos"):
            validate_delitos_del_csv(catalogos, nuevo)

    def test_municipio_del_catalogo_pasa(self, catalogos):
        claves = pl.DataFrame({"cve_ent": ["01"], "cve_mun": ["001"]})
        validate_municipios_del_csv(catalogos, claves)

    def test_municipio_ausente_se_reporta(self, catalogos):
        """Incluye los sinteticos 998/999 si no estan en el seed geografico."""
        claves = pl.DataFrame({"cve_ent": ["01"], "cve_mun": ["998"]})
        with pytest.raises(CatalogoDesactualizado, match="denue_municipios"):
            validate_municipios_del_csv(catalogos, claves)


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


@pytest.mark.postgres
@pytest.mark.unit
class TestLoadCsv:
    """`_load_csv`: el CSV ancho se vuelve filas con FK y fecha."""

    def _cargar(self, catalogos, tmp_path, encoding: str) -> int:
        """Corre la carga completa y devuelve las filas escritas.

        El catalogo de delitos ya esta sembrado por el fixture, como hace
        la migracion: el transform no lo descubre del CSV.
        """
        csv_path = tmp_path / "corte.csv"
        _write_sample_csv(csv_path, encoding=encoding)

        processor = SesnspProcessor.__new__(SesnspProcessor)
        processor.seeds_dir = tmp_path
        nuevos, filas = processor._load_csv(catalogos, csv_path)
        assert nuevos == 0, "el catalogo no se modifica aqui"
        assert filas == 24, "2 filas anchas x 12 meses"
        return filas

    def _carpetas(self, conn, subtipo: str, fecha: str) -> int:
        """Carpetas registradas para un delito en un mes."""
        hechos = get_table("sesnsp_incidencia_municipal")
        delitos = get_table(DELITOS_TABLE)
        return conn.execute(
            select(hechos.c.carpetas)
            .select_from(hechos.join(delitos, delitos.c.id == hechos.c.delito_id))
            .where(delitos.c.subtipo == subtipo, hechos.c.fecha == date.fromisoformat(fecha))
        ).scalar_one()

    def test_cada_mes_queda_en_su_fecha(self, catalogos, tmp_path):
        """El unpivot tiene que mandar cada columna al mes correcto."""
        self._cargar(catalogos, tmp_path, "latin-1")
        assert self._carpetas(catalogos, "Homicidio doloso", "2020-01-01") == 2
        assert self._carpetas(catalogos, "Robo a negocio", "2020-12-01") == 1

    def test_las_claves_geograficas_van_con_ceros(self, catalogos, tmp_path):
        """El SESNSP las publica como enteros; las FKs las exigen con ceros."""
        self._cargar(catalogos, tmp_path, "latin-1")
        hechos = get_table("sesnsp_incidencia_municipal")
        assert catalogos.execute(select(hechos.c.cve_ent, hechos.c.cve_mun).distinct()).all() == [
            ("01", "001")
        ]

    def test_utf8_carga_identico(self, catalogos, tmp_path):
        """El corte llega en latin-1 o utf-8 segun quien lo publique."""
        self._cargar(catalogos, tmp_path, "utf-8")
        hechos = get_table("sesnsp_incidencia_municipal")
        total = catalogos.execute(select(func.sum(hechos.c.carpetas))).scalar_one()
        assert total == 2 + 1 + 3 + 5 + 4 + 1

    def test_recargar_reemplaza_en_vez_de_acumular(self, catalogos, tmp_path):
        """Cada corte revisa cifras historicas: la tabla se reconstruye."""
        self._cargar(catalogos, tmp_path, "latin-1")
        self._cargar(catalogos, tmp_path, "latin-1")

        hechos = get_table("sesnsp_incidencia_municipal")
        assert catalogos.execute(select(func.count()).select_from(hechos)).scalar_one() == 24
