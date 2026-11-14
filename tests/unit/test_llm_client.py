"""El cliente de Ollama sin Ollama: lo que manda y cómo falla."""

import pytest
import requests

from src.shared.llm import OllamaCliente, OllamaNoDisponible
from src.shared.llm import client as modulo


class RespuestaFalsa:
    def __init__(self, json, status=200):
        self._json = json
        self.status_code = status

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.mark.unit
class TestChat:
    def test_manda_modelo_contexto_herramientas_y_sin_stream(self, monkeypatch):
        capturado = {}

        def post(url, json=None, timeout=None):
            capturado.update(url=url, cuerpo=json, timeout=timeout)
            return RespuestaFalsa(
                {
                    "message": {"role": "assistant", "content": "hola"},
                    "prompt_eval_count": 10,
                    "eval_count": 2,
                }
            )

        monkeypatch.setattr(modulo.requests, "post", post)
        cliente = OllamaCliente(
            "http://ollama:11434/", "qwen3:32b", timeout=99, num_ctx=1234, pensar=True
        )
        mensaje = cliente.chat([{"role": "user", "content": "hola"}], [{"type": "function"}])
        assert capturado["url"] == "http://ollama:11434/api/chat"
        cuerpo = capturado["cuerpo"]
        assert (
            cuerpo["model"] == "qwen3:32b" and cuerpo["stream"] is False and cuerpo["think"] is True
        )
        assert cuerpo["options"] == {"num_ctx": 1234, "temperature": 0.0}
        assert cuerpo["tools"] == [{"type": "function"}]
        assert capturado["timeout"] == 99
        assert mensaje["content"] == "hola" and mensaje["_tokens"] == {"prompt": 10, "respuesta": 2}

    def test_sin_herramientas_no_manda_la_clave(self, monkeypatch):
        capturado = {}
        monkeypatch.setattr(
            modulo.requests,
            "post",
            lambda url, json=None, timeout=None: (
                capturado.update(json) or RespuestaFalsa({"message": {}})
            ),
        )
        OllamaCliente("http://o", "m").chat([])
        assert "tools" not in capturado

    def test_reintenta_la_red_y_luego_se_rinde(self, monkeypatch):
        intentos = []

        def post(*a, **k):
            intentos.append(1)
            raise requests.ConnectionError("abajo")

        monkeypatch.setattr(modulo.requests, "post", post)
        monkeypatch.setattr(modulo.time, "sleep", lambda s: None)
        with pytest.raises(OllamaNoDisponible, match="no respondio tras 3 intentos"):
            OllamaCliente("http://o", "m").chat([])
        assert len(intentos) == 3

    def test_un_error_del_modelo_no_se_reintenta(self, monkeypatch):
        """Una peticion rechazada no se arregla repitiendola."""
        intentos = []

        def post(*a, **k):
            intentos.append(1)
            return RespuestaFalsa({}, status=400)

        monkeypatch.setattr(modulo.requests, "post", post)
        with pytest.raises(requests.HTTPError):
            OllamaCliente("http://o", "m").chat([])
        assert len(intentos) == 1


@pytest.mark.unit
class TestComprobar:
    def test_sin_servidor_dice_que_arrancarlo(self, monkeypatch):
        def get(*a, **k):
            raise requests.ConnectionError("x")

        monkeypatch.setattr(modulo.requests, "get", get)
        with pytest.raises(OllamaNoDisponible, match="ollama serve"):
            OllamaCliente("http://o", "qwen3:32b").comprobar()

    def test_sin_el_modelo_dice_que_hacer_pull(self, monkeypatch):
        monkeypatch.setattr(
            modulo.requests,
            "get",
            lambda *a, **k: RespuestaFalsa({"models": [{"name": "llama3.1:8b"}]}),
        )
        with pytest.raises(OllamaNoDisponible, match="ollama pull qwen3:32b"):
            OllamaCliente("http://o", "qwen3:32b").comprobar()

    def test_con_el_modelo_pasa_aunque_el_tag_traiga_cuantizacion(self, monkeypatch):
        monkeypatch.setattr(
            modulo.requests,
            "get",
            lambda *a, **k: RespuestaFalsa({"models": [{"name": "qwen3:32b-q4_K_M"}]}),
        )
        OllamaCliente("http://o", "qwen3:32b").comprobar()
