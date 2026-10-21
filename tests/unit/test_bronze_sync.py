"""Registrar en metadata los ZIP que ya estan en Bronze.

El ingest no mira el disco: lee de metadata. Eso deja un hueco cuando Bronze
llega por copia en vez de por descarga, y sin cerrarlo la unica salida seria
volver a bajar 484 archivos de INEGI con Selenium.
"""

import pytest

from src.etl.extractors.bronze_sync import LOCAL_SCHEME, sync_bronze

pytestmark = pytest.mark.postgres


@pytest.fixture
def bronze(tmp_path):
    """Bronze con dos cortes, como lo deja el extractor.

    La estructura es `<bronze>/<corte>/<archivo>.zip`, con el corte en el
    formato `MM_AAAA` que usa INEGI.
    """
    raiz = tmp_path / "data" / "bronze" / "denue"
    for corte, archivos in {"03_2018": ["comercio", "servicios"], "11_2024": ["comercio"]}.items():
        (raiz / corte).mkdir(parents=True)
        for nombre in archivos:
            (raiz / corte / f"{nombre}.zip").write_bytes(b"PK\x03\x04 contenido")
    return raiz


class TestRegistro:
    def test_registra_cada_zip_de_cada_corte(self, bronze, tracker, tmp_path):
        assert sync_bronze(bronze, tracker, project_root=tmp_path) == {
            "03_2018": 2,
            "11_2024": 1,
        }

    def test_el_ingest_ve_lo_registrado(self, bronze, tracker, tmp_path):
        """Es el unico punto de la operacion: que el ingest los encuentre."""
        sync_bronze(bronze, tracker, project_root=tmp_path)
        descargas = tracker.get_all_successful_downloads()
        assert len(descargas) == 3
        assert {d["period"] for d in descargas} == {"03_2018", "11_2024"}

    def test_las_rutas_quedan_relativas(self, bronze, tracker, tmp_path):
        """Absolutas harian el historial inservible al cambiar de maquina.

        El ingest resuelve `Path.cwd() / source_path`, asi que una ruta
        absoluta de otra maquina no apuntaria a nada.
        """
        sync_bronze(bronze, tracker, project_root=tmp_path)
        rutas = [d["source_path"] for d in tracker.get_all_successful_downloads()]
        assert all(not r.startswith("/") for r in rutas)
        assert "data/bronze/denue/03_2018/comercio.zip" in rutas

    def test_el_origen_queda_marcado_como_local(self, bronze, tracker, tmp_path):
        """Que no sea una URL de INEGI es informacion, no un detalle."""
        sync_bronze(bronze, tracker, project_root=tmp_path)
        urls = tracker.get_successful_download_urls()
        assert all(u.startswith(LOCAL_SCHEME) for u in urls)

    def test_registra_el_tamano_del_archivo(self, bronze, tracker, tmp_path):
        sync_bronze(bronze, tracker, project_root=tmp_path)
        run = tracker.get_recent_runs(limit=1)[0]
        tareas = tracker.get_tasks_by_run(run["run_id"])
        assert all(t["file_size_bytes"] > 0 for t in tareas)


class TestIdempotencia:
    def test_la_segunda_corrida_no_duplica(self, bronze, tracker, tmp_path):
        sync_bronze(bronze, tracker, project_root=tmp_path)
        assert sync_bronze(bronze, tracker, project_root=tmp_path) == {}
        assert len(tracker.get_all_successful_downloads()) == 3

    def test_un_archivo_nuevo_si_se_registra(self, bronze, tracker, tmp_path):
        """El caso real: llega un corte nuevo a un Bronze ya sincronizado."""
        sync_bronze(bronze, tracker, project_root=tmp_path)

        (bronze / "05_2026").mkdir()
        (bronze / "05_2026" / "comercio.zip").write_bytes(b"PK\x03\x04")

        assert sync_bronze(bronze, tracker, project_root=tmp_path) == {"05_2026": 1}
        assert len(tracker.get_all_successful_downloads()) == 4


class TestCasosLimite:
    def test_un_bronze_inexistente_lo_dice(self, tracker, tmp_path):
        with pytest.raises(FileNotFoundError, match="No existe"):
            sync_bronze(tmp_path / "no_existe", tracker, project_root=tmp_path)

    def test_un_bronze_sin_cortes_no_es_error(self, tracker, tmp_path):
        """Un directorio vacio es un Bronze sin descargar, no un fallo."""
        vacio = tmp_path / "bronze"
        vacio.mkdir()
        assert sync_bronze(vacio, tracker, project_root=tmp_path) == {}

    def test_un_corte_sin_zips_se_omite(self, bronze, tracker, tmp_path):
        """Una descarga interrumpida deja el directorio pero no archivos."""
        (bronze / "05_2026").mkdir()
        registrados = sync_bronze(bronze, tracker, project_root=tmp_path)
        assert "05_2026" not in registrados

    def test_lo_que_no_es_zip_se_ignora(self, bronze, tracker, tmp_path):
        """Bronze acumula logs y archivos sueltos que no son datos."""
        (bronze / "03_2018" / "notas.txt").write_text("no es un corte")
        assert sync_bronze(bronze, tracker, project_root=tmp_path)["03_2018"] == 2
