"""El ingest: de los ZIP de un corte a una particion de Silver.

La unidad es el corte, no el archivo, y eso es lo que estos tests fijan: que
los ZIP de un periodo se carguen juntos, que un corte que falla no toque a
los demas, y que seleccionar cortes a mano funcione -- que es como se
estrena el ingest sin soltar los veinte de golpe.
"""

import io
import zipfile
from datetime import date

import polars as pl
import pytest
from sqlalchemy import func, insert, select

from src.etl.transformers.processor import BronzeToSilverProcessor, period_to_date
from src.meta.tracker import MetadataTracker
from src.shared.db.base import get_table
from src.shared.db.partitions import existing_partitions, partition_name
from src.shared.enums import TaskStatus

pytestmark = pytest.mark.postgres

#: Encabezado minimo del CSV del DENUE con las columnas que el ingest exige.
CSV_HEADER = (
    "id,nom_estab,raz_social,codigo_act,per_ocu,tipo_vial,nom_vial,"
    "tipo_asent,nomb_asent,cve_ent,cve_mun,cve_loc,latitud,longitud,"
    "fecha_alta,tipounieco"
)


def fila_csv(est_id: str, fecha: str = "2018-03") -> str:
    """Una fila de CSV valida para el escenario de prueba.

    El nombre del negocio se deriva de `est_id` a proposito: el ingest
    recalcula el `id` como hash de (clee, nom_estab, raz_social, codigo_act,
    period), asi que dos filas con el mismo nombre serian la MISMA fila y se
    deduplicarian. Para tener n filas distintas hacen falta n nombres.
    """
    return (
        f"{est_id},NEGOCIO {est_id},RAZON {est_id} SA,722515,11 A 30 PERSONAS,"
        f"CALLE,INSURGENTES,COLONIA,DEL VALLE,09,015,0001,19.4326,-99.1332,"
        f"{fecha},FIJO"
    )


def escribir_zip(destino, filas: list[str]) -> None:
    """Escribe un ZIP con un CSV `conjunto_de_datos` dentro.

    Es como los publica INEGI: el ZIP trae el CSV de datos y archivos de
    metadatos que el ingest debe ignorar.
    """
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("conjunto_de_datos/denue.csv", "\n".join([CSV_HEADER, *filas]))
        z.writestr("metadatos/dicc.csv", "esto no debe leerse")
    destino.write_bytes(buf.getvalue())


@pytest.fixture
def catalogos(silver_engine):
    """Los catalogos que el ingest exige para resolver las etiquetas."""
    with silver_engine.begin() as conn:
        conn.execute(
            insert(get_table("denue_tipos_vialidad")),
            [{"id": 1, "tipo_vial": "CALLE", "descripcion": "via"}],
        )
        conn.execute(
            insert(get_table("denue_tipos_asentamiento")),
            [{"id": 1, "tipo_asent": "COLONIA", "descripcion": "asentamiento"}],
        )
        conn.execute(
            insert(get_table("denue_tipos_unidad_economica")),
            [{"id": 1, "tipounieco": "FIJO", "descripcion": "fijo"}],
        )
        conn.execute(
            insert(get_table("denue_estratos_personal")),
            [
                {
                    "id": 3,
                    "per_ocu": "11 A 30 PERSONAS",
                    "descripcion": "mediana",
                    "empleados_min": 11,
                    "empleados_max": 30,
                    "categoria": "Small",
                }
            ],
        )
        conn.execute(
            insert(get_table("denue_actividades_economicas")),
            [
                {
                    "codigo_act": "722515",
                    "nombre_act": "Cafeterias",
                    "sector": "72",
                    "subsector": "722",
                    "rama": "7225",
                }
            ],
        )
        conn.execute(
            insert(get_table("denue_entidades")),
            [{"cve_ent": "09", "entidad": "Ciudad de Mexico"}],
        )
        conn.execute(
            insert(get_table("denue_municipios")),
            [{"cve_ent": "09", "cve_mun": "015", "municipio": "Cuauhtemoc"}],
        )
        conn.execute(
            insert(get_table("denue_localidades")),
            [{"cve_ent": "09", "cve_mun": "015", "cve_loc": "0001", "localidad": "Centro"}],
        )
    return silver_engine


@pytest.fixture
def ingest(catalogos, tracker, tmp_path):
    """Procesador apuntando al Postgres y al Bronze de prueba."""
    return BronzeToSilverProcessor(data_root=tmp_path, tracker=tracker, engine=catalogos)


def registrar_descargas(tracker: MetadataTracker, archivos: dict[str, list]) -> None:
    """Deja en metadata una descarga SUCCESS por archivo.

    El ingest lee de ahi que procesar, asi que sin esto no ve nada.

    Args:
        tracker: registro de corridas.
        archivos: corte -> rutas de los ZIP de ese corte.
    """
    run_id = tracker.create_run(job_name="descarga_de_prueba")
    for period, rutas in archivos.items():
        for ruta in rutas:
            task_id = tracker.create_task(
                run_id, ruta.name, f"http://x/{ruta.name}", str(ruta), period=period
            )
            tracker.start_task(task_id)
            tracker.complete_task(task_id, TaskStatus.SUCCESS, actual_size_bytes=1)


def contar(engine) -> int:
    """Filas en la tabla de hechos."""
    with engine.connect() as conn:
        return conn.execute(
            select(func.count()).select_from(get_table("establecimientos"))
        ).scalar_one()


class TestNombreDelCorte:
    """INEGI nombra sus directorios MM_AAAA, con el mes primero."""

    def test_mes_y_ano(self):
        assert period_to_date("03_2018") == date(2018, 3, 1)

    def test_mes_de_dos_digitos(self):
        assert period_to_date("11_2024") == date(2024, 11, 1)

    def test_un_ano_suelto_cae_en_enero(self):
        """Algun corte antiguo viene solo con el ano."""
        assert period_to_date("2015") == date(2015, 1, 1)

    def test_un_nombre_que_no_es_corte_falla(self):
        with pytest.raises(ValueError):
            period_to_date("marzo-2018")


class TestCargaDeUnCorte:
    def test_los_zip_del_corte_se_cargan_juntos(self, ingest, tracker, tmp_path):
        """Un corte son decenas de ZIP; la particion se construye con todos."""
        zips = []
        for i in range(3):
            z = tmp_path / f"parte{i}.zip"
            escribir_zip(z, [fila_csv(f"id-{i}-1"), fila_csv(f"id-{i}-2")])
            zips.append(z)
        registrar_descargas(tracker, {"03_2018": zips})

        assert ingest.run() is True
        assert contar(ingest.engine) == 6

    def test_queda_una_particion_por_corte(self, ingest, tracker, tmp_path):
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-1")])
        registrar_descargas(tracker, {"03_2018": [z]})
        ingest.run()

        with ingest.engine.connect() as conn:
            assert existing_partitions(conn) == [partition_name(date(2018, 3, 1))]

    def test_la_fecha_y_las_celdas_h3_vienen_calculadas(self, ingest, tracker, tmp_path):
        """Lo que antes hacian dos etapas aparte, ahora llega con el COPY."""
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-1", fecha="2010-07")])
        registrar_descargas(tracker, {"03_2018": [z]})
        ingest.run()

        estab = get_table("establecimientos")
        with ingest.engine.connect() as conn:
            fila = conn.execute(
                select(estab.c.fecha_alta_date, estab.c.h3_cell_9, estab.c.h3_cell_5)
            ).one()
        assert fila[0] == date(2010, 7, 1)
        assert fila[1] is not None and fila[2] is not None

    def test_las_etiquetas_llegan_como_llaves(self, ingest, tracker, tmp_path):
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-1")])
        registrar_descargas(tracker, {"03_2018": [z]})
        ingest.run()

        estab = get_table("establecimientos")
        with ingest.engine.connect() as conn:
            fila = conn.execute(
                select(estab.c.tipo_vial_id, estab.c.per_ocu_id, estab.c.tipounieco_id)
            ).one()
        assert fila == (1, 3, 1)

    def test_los_metadatos_del_zip_se_ignoran(self, ingest, tracker, tmp_path):
        """El ZIP de INEGI trae diccionarios que no son datos."""
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-1")])
        registrar_descargas(tracker, {"03_2018": [z]})
        ingest.run()
        assert contar(ingest.engine) == 1


class TestSeleccionDeCortes:
    """`--period` es como se estrena el ingest sin soltar los veinte."""

    def test_solo_carga_el_corte_pedido(self, ingest, tracker, tmp_path):
        a, b = tmp_path / "a.zip", tmp_path / "b.zip"
        escribir_zip(a, [fila_csv("id-a")])
        escribir_zip(b, [fila_csv("id-b")])
        registrar_descargas(tracker, {"03_2018": [a], "11_2024": [b]})

        ingest.run(periods=["03_2018"])

        with ingest.engine.connect() as conn:
            assert existing_partitions(conn) == [partition_name(date(2018, 3, 1))]
        assert contar(ingest.engine) == 1

    def test_un_corte_inexistente_se_reporta(self, ingest, tracker, tmp_path):
        """Casi siempre es un nombre mal escrito: mejor decirlo."""
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-a")])
        registrar_descargas(tracker, {"03_2018": [z]})

        with pytest.raises(ValueError, match="sin descargas"):
            ingest.run(periods=["99_1999"])

    def test_el_error_lista_los_cortes_disponibles(self, ingest, tracker, tmp_path):
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-a")])
        registrar_descargas(tracker, {"03_2018": [z]})

        with pytest.raises(ValueError, match="03_2018"):
            ingest.run(periods=["99_1999"])


class TestAislamientoEntreCortes:
    def test_un_corte_fallido_no_toca_a_los_demas(self, ingest, tracker, tmp_path):
        """El motivo de que la particion sea la unidad de recuperacion."""
        bueno = tmp_path / "bueno.zip"
        escribir_zip(bueno, [fila_csv("id-ok")])

        # El ZIP del segundo corte trae un codigo que no esta en SCIAN, asi
        # que su carga se detiene en la validacion de catalogos.
        malo = tmp_path / "malo.zip"
        escribir_zip(malo, [fila_csv("id-bad").replace("722515", "999999")])

        registrar_descargas(tracker, {"03_2018": [bueno], "11_2024": [malo]})

        assert ingest.run() is False, "la corrida reporta el fallo"
        assert contar(ingest.engine) == 1, "el corte bueno sobrevivio"
        with ingest.engine.connect() as conn:
            assert existing_partitions(conn) == [partition_name(date(2018, 3, 1))]

    def test_recargar_un_corte_lo_reconstruye(self, ingest, tracker, tmp_path):
        """La particion se construye entera, no se completa."""
        z = tmp_path / "a.zip"
        escribir_zip(z, [fila_csv("id-1"), fila_csv("id-2")])
        registrar_descargas(tracker, {"03_2018": [z]})

        ingest.run(periods=["03_2018"])
        ingest.run(periods=["03_2018"])

        assert contar(ingest.engine) == 2, "sin duplicados"

    def test_un_archivo_que_falta_en_disco_se_registra(self, ingest, tracker, tmp_path):
        """Metadata dice que se descargo, pero el archivo no esta."""
        presente = tmp_path / "presente.zip"
        escribir_zip(presente, [fila_csv("id-1")])
        ausente = tmp_path / "ausente.zip"
        registrar_descargas(tracker, {"03_2018": [presente, ausente]})

        assert ingest.run(periods=["03_2018"]) is True
        assert contar(ingest.engine) == 1

        tareas = pl.DataFrame(
            [(t["task_name"], t["status"]) for t in _process_tasks(ingest.engine)],
            schema=["task_name", "status"],
            orient="row",
        )
        por_nombre = dict(tareas.iter_rows())
        assert por_nombre["ausente.zip"] == TaskStatus.FAILED.value
        assert por_nombre["presente.zip"] == TaskStatus.SUCCESS.value


def _process_tasks(engine) -> list[dict]:
    """Las tareas de proceso registradas, como diccionarios."""
    tabla = get_table("process_tasks", schema="meta")
    with engine.connect() as conn:
        return [dict(f) for f in conn.execute(select(tabla)).mappings()]
