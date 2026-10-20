"""Los endpoints de `/ops` reportan lo que el tracker registro.

Se prueban contra el tracker real sobre el Postgres de pruebas, no contra
un doble: lo que puede romperse aqui es justamente que la lectura del API y
la escritura del ETL dejen de coincidir, y un mock no lo detectaria.
"""

import pytest
from fastapi.testclient import TestClient

from src.api.main import app
from src.api.routers.ops import get_tracker
from src.shared.config import get_settings
from src.shared.enums import JobStatus, TaskStatus

pytestmark = pytest.mark.postgres

PREFIX = f"{get_settings().API_PREFIX}/ops"


@pytest.fixture
def client(tracker):
    """Cliente con el tracker apuntando a la base de pruebas."""
    app.dependency_overrides[get_tracker] = lambda: tracker
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def run_con_tareas(tracker):
    """Una corrida terminada con una descarga exitosa y una fallida."""
    run_id = tracker.create_run(job_name="denue_downloader", job_type="DOWNLOAD")

    ok = tracker.create_task(run_id, "ok.zip", "http://x/ok.zip", "/data/ok.zip")
    tracker.start_task(ok)
    tracker.complete_task(ok, TaskStatus.SUCCESS, actual_size_bytes=100)

    mal = tracker.create_task(run_id, "mal.zip", "http://x/mal.zip", "/data/mal.zip")
    tracker.start_task(mal)
    tracker.complete_task(mal, TaskStatus.FAILED, error_message="timeout")

    tracker.complete_run(run_id)
    return run_id


class TestListarCorridas:
    def test_devuelve_la_corrida_creada(self, client, run_con_tareas):
        runs = client.get(f"{PREFIX}/runs").json()
        assert [r["run_id"] for r in runs] == [run_con_tareas]

    def test_respeta_el_limite(self, client, tracker):
        for i in range(5):
            tracker.create_run(job_name=f"job_{i}")
        assert len(client.get(f"{PREFIX}/runs?limit=3").json()) == 3

    def test_rechaza_un_limite_absurdo(self, client):
        assert client.get(f"{PREFIX}/runs?limit=999999").status_code == 422


class TestDetalleDeCorrida:
    def test_una_exitosa_y_una_fallida_dan_PARTIAL(self, client, run_con_tareas):
        run = client.get(f"{PREFIX}/runs/{run_con_tareas}").json()
        assert run["status"] == JobStatus.PARTIAL.value
        assert run["tasks_succeeded"] == 1
        assert run["tasks_failed"] == 1

    def test_el_conteo_real_cuadra_con_el_agregado(self, client, run_con_tareas):
        """Si difieren, la corrida murio antes de consolidar sus metricas."""
        run = client.get(f"{PREFIX}/runs/{run_con_tareas}").json()
        assert run["actual_total_tasks"] == run["total_tasks"]
        assert run["actual_succeeded"] == run["tasks_succeeded"]

    def test_corrida_inexistente_da_404(self, client):
        assert client.get(f"{PREFIX}/runs/no-existe").status_code == 404


class TestTareasDeCorrida:
    def test_lista_las_dos_descargas(self, client, run_con_tareas):
        tasks = client.get(f"{PREFIX}/runs/{run_con_tareas}/tasks").json()
        assert {t["task_name"] for t in tasks} == {"ok.zip", "mal.zip"}

    def test_corrida_inexistente_da_404(self, client):
        assert client.get(f"{PREFIX}/runs/no-existe/tasks").status_code == 404


class TestSaludDelPipeline:
    def test_pipeline_sin_estrenar_esta_sano(self, client):
        """Una etapa que nunca corrio no es un fallo."""
        salud = client.get(f"{PREFIX}/health/pipeline").json()
        assert salud["healthy"] is True
        assert salud["failing_stages"] == []
        assert salud["stages"]["transform"]["status"] is None

    def test_una_etapa_fallida_lo_marca_sin_tumbar_el_endpoint(self, client, tracker):
        run_id = tracker.create_run(job_name="t", job_type="TRANSFORM")
        tracker.complete_run(run_id, status=JobStatus.FAILED)

        respuesta = client.get(f"{PREFIX}/health/pipeline")
        # 200 aunque el pipeline este mal: el endpoint sano y el pipeline
        # sano son dos cosas distintas.
        assert respuesta.status_code == 200
        salud = respuesta.json()
        assert salud["healthy"] is False
        assert salud["failing_stages"] == ["transform"]
