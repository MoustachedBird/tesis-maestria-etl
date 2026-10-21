"""Los logs de una corrida quedan donde dicen y en el formato que dicen.

Una corrida del ETL son horas y 484 descargas; cuando algo sale mal, el log
es lo unico que queda. Que `app.log` exista pero no se pueda parsear es peor
que no tenerlo, porque se descubre cuando ya hace falta.
"""

import json
import logging

import pytest
import structlog

from src.shared.logging import PipelineLogger, get_stage_logger


@pytest.fixture
def pipeline_logger(tmp_path):
    """Logger de corrida sobre un directorio desechable.

    Sin consola: RichHandler escribe a stderr y aqui solo interesan los
    archivos, que son el registro duradero.
    """
    pl = PipelineLogger(
        log_dir=tmp_path / "downloads", run_id="corrida-1", enable_console=False
    )
    yield pl
    # El root logger es global: dejarlo con los handlers de este test
    # contaminaria los siguientes.
    raiz = logging.getLogger()
    for handler in list(raiz.handlers):
        raiz.removeHandler(handler)
        handler.close()
    PipelineLogger._instalados.clear()


def leer_jsonl(path) -> list[dict]:
    """Las lineas de un log JSON, ya parseadas.

    Parsear es la prueba: si el handler no tuviera formatter, el archivo
    traeria la repr de un diccionario de Python --con comillas simples-- y
    `json.loads` fallaria.
    """
    return [json.loads(linea) for linea in path.read_text().splitlines() if linea.strip()]


class TestFormatoDeArchivo:
    def test_app_log_es_json_parseable(self, pipeline_logger):
        pipeline_logger.get_logger().info("descarga_ok", url="http://x/a.zip")
        assert leer_jsonl(pipeline_logger.json_log_path)

    def test_conserva_los_campos_estructurados(self, pipeline_logger):
        """El valor de loguear estructurado: poder filtrar por campo."""
        pipeline_logger.get_logger().info(
            "descarga_ok", url="http://x/a.zip", size_bytes=2716967, attempts=1
        )
        registro = leer_jsonl(pipeline_logger.json_log_path)[0]
        assert registro["event"] == "descarga_ok"
        assert registro["url"] == "http://x/a.zip"
        assert registro["size_bytes"] == 2716967, "los numeros no deben volverse texto"

    def test_lleva_nivel_y_marca_de_tiempo(self, pipeline_logger):
        pipeline_logger.get_logger().warning("lento", speed_mbps=0.3)
        registro = leer_jsonl(pipeline_logger.json_log_path)[0]
        assert registro["level"] == "warning"
        assert registro["timestamp"]

    def test_el_contexto_de_la_corrida_va_en_cada_linea(self, pipeline_logger):
        """Sin el run_id, un log de 484 descargas no se puede cruzar con meta."""
        pipeline_logger.get_logger().info("algo")
        assert leer_jsonl(pipeline_logger.json_log_path)[0]["run_id"] == "corrida-1"


class TestArchivoDeErrores:
    def test_solo_recoge_lo_accionable(self, pipeline_logger):
        """En 484 descargas, errors.log es el que se abre primero."""
        log = pipeline_logger.get_logger()
        log.info("descarga_ok")
        log.warning("reintento")
        log.error("descarga_fallida", error_type="TIMEOUT")

        eventos = [r["event"] for r in leer_jsonl(pipeline_logger.error_log_path)]
        assert eventos == ["descarga_fallida"]

    def test_el_error_tambien_queda_en_app_log(self, pipeline_logger):
        """errors.log es un filtro, no un desvio: no se pierde del completo."""
        pipeline_logger.get_logger().error("descarga_fallida")
        assert "descarga_fallida" in [
            r["event"] for r in leer_jsonl(pipeline_logger.json_log_path)
        ]

    def test_una_excepcion_deja_su_traza(self, pipeline_logger):
        try:
            raise ValueError("el ZIP venia truncado")
        except ValueError:
            pipeline_logger.get_logger().exception("proceso_fallido")

        registro = leer_jsonl(pipeline_logger.error_log_path)[0]
        assert "ValueError" in registro["exception"]
        assert "el ZIP venia truncado" in registro["exception"]


class TestLogsDeLibrerias:
    def test_un_logger_estandar_tambien_cae_en_el_archivo(self, pipeline_logger):
        """Las librerias usan `logging`, no structlog, y hay que capturarlas.

        Sin `foreign_pre_chain` sus lineas saldrian sin nivel ni timestamp, y
        el archivo dejaria de ser homogeneo.
        """
        logging.getLogger("una.libreria").error("algo revento")

        registros = leer_jsonl(pipeline_logger.error_log_path)
        assert registros[0]["event"] == "algo revento"
        assert registros[0]["level"] == "error"
        assert registros[0]["timestamp"]


class TestNoDuplicar:
    def test_dos_corridas_no_duplican_las_lineas(self, tmp_path):
        """Crear un segundo logger apilaba handlers y todo se escribia dos veces."""
        primero = PipelineLogger(
            log_dir=tmp_path / "d", run_id="r1", enable_console=False
        )
        segundo = PipelineLogger(
            log_dir=tmp_path / "d", run_id="r2", enable_console=False
        )
        segundo.get_logger().info("una_sola_vez")

        assert len(leer_jsonl(segundo.json_log_path)) == 1
        assert not primero.json_log_path.read_text(), "la corrida vieja no recibe nada"

        raiz = logging.getLogger()
        for handler in list(raiz.handlers):
            raiz.removeHandler(handler)
            handler.close()
        PipelineLogger._instalados.clear()


class TestDirectorioDeCorrida:
    def test_dos_corridas_seguidas_no_comparten_directorio(self, tmp_path):
        """Al segundo no alcanzaba: dos corridas del mismo segundo chocaban."""
        uno = PipelineLogger(log_dir=tmp_path / "d", run_id="r1", enable_console=False)
        dos = PipelineLogger(log_dir=tmp_path / "d", run_id="r2", enable_console=False)
        assert uno.log_directory != dos.log_directory

        raiz = logging.getLogger()
        for handler in list(raiz.handlers):
            raiz.removeHandler(handler)
            handler.close()
        PipelineLogger._instalados.clear()

    def test_cada_corrida_tiene_su_directorio(self, pipeline_logger):
        assert pipeline_logger.json_log_path.parent == pipeline_logger.log_directory
        assert pipeline_logger.json_log_path.name == "app.log"
        assert pipeline_logger.error_log_path.name == "errors.log"

    def test_el_enlace_latest_apunta_a_la_ultima(self, pipeline_logger):
        """`tail -f logs/downloads/latest/app.log` sin saber la fecha."""
        latest = pipeline_logger.log_directory.parent.parent / "latest"
        assert latest.is_symlink()
        assert latest.resolve() == pipeline_logger.log_directory.resolve()


class TestLoggerDeStage:
    """`get_stage_logger` es el otro camino: stdlib, archivo por etapa."""

    def test_escribe_al_archivo_que_se_le_da(self, tmp_path):
        destino = tmp_path / "etl_ingest.log"
        log = get_stage_logger("stage.de.prueba", destino)
        log.info("corte cargado")
        assert "corte cargado" in destino.read_text()

    def test_es_idempotente(self, tmp_path):
        """Se llama en cada import del modulo; no debe apilar handlers."""
        destino = tmp_path / "etl.log"
        primero = get_stage_logger("stage.idempotente", destino)
        segundo = get_stage_logger("stage.idempotente", destino)
        assert primero is segundo
        assert len(primero.handlers) == 2

    def test_no_propaga_al_root(self, tmp_path):
        """Si propagara, cada linea saldria tambien por los handlers de la corrida."""
        log = get_stage_logger("stage.aislado", tmp_path / "etl.log")
        assert log.propagate is False


def test_el_logger_global_sigue_siendo_structlog():
    """`get_logger()` lo usan los modulos que no son stage ni corrida."""
    from src.shared.logging import get_logger

    assert isinstance(get_logger("x"), structlog.typing.BindableLogger | structlog.BoundLogger)
