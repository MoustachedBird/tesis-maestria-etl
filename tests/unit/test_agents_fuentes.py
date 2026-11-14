"""Las fuentes del agente: clasificación de URLs, el buscador y el lector, sin red."""

import pytest
import requests

from src.agents.survival.sources import busqueda, paginas
from src.agents.survival.sources.busqueda import BuscadorBrave, BuscadorSearxng, clasificar_fuente


class RespuestaFalsa:
    def __init__(self, json=None, status=200, text="", content_type="application/json"):
        self._json = json
        self.status_code = status
        self.text = text
        self.headers = {"content-type": content_type}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


@pytest.mark.unit
class TestClasificarFuente:
    @pytest.mark.parametrize(
        "url,fuente",
        [
            ("https://www.google.com/maps/place/tacos", "google_maps"),
            ("https://maps.app.goo.gl/abc", "google_maps"),
            ("https://m.facebook.com/tacospaco", "facebook"),
            ("https://www.instagram.com/tacospaco/", "instagram"),
            ("https://www.seccionamarilla.com.mx/x", "directorio"),
            ("https://www.ubereats.com/mx/store/x", "marketplace"),
            ("https://www.informador.mx/jalisco/x", "noticia"),
            ("https://web.archive.org/web/2024/x", "wayback"),
            ("https://tacospaco.mx/", "sitio_web"),
        ],
    )
    def test_por_dominio(self, url, fuente):
        assert clasificar_fuente(url) == fuente

    def test_es_determinista_y_no_lo_decide_el_modelo(self):
        """Es el eje de los GROUP BY del análisis, así que lo fija el código."""
        assert clasificar_fuente("HTTPS://WWW.FACEBOOK.COM/X") == "facebook"


@pytest.mark.unit
class TestSearxng:
    def test_parsea_la_api_json(self, monkeypatch):
        capturado = {}

        def get(url, params=None, headers=None, timeout=None):
            capturado.update(url=url, params=params)
            return RespuestaFalsa(
                json={
                    "results": [
                        {"title": "Tacos Paco", "url": "https://x.mx/1", "content": "Cerrado"},
                        {"title": "Sin url", "content": "..."},
                        {"title": "Otro", "url": "https://x.mx/2", "content": None},
                    ]
                }
            )

        monkeypatch.setattr(busqueda.requests, "get", get)
        resultados = BuscadorSearxng("http://searxng:8080/").buscar("tacos paco", n=5)
        assert capturado["url"] == "http://searxng:8080/search"
        assert capturado["params"]["format"] == "json" and capturado["params"]["q"] == "tacos paco"
        assert [r.url for r in resultados] == [
            "https://x.mx/1",
            "https://x.mx/2",
        ]  # la sin url se cae
        assert resultados[1].extracto == ""
        assert "https://x.mx/1" in resultados[0].como_texto()

    def test_un_403_se_propaga(self, monkeypatch):
        """Sin `json` en search.formats SearXNG responde 403: tiene que verse, no taparse."""
        monkeypatch.setattr(busqueda.requests, "get", lambda *a, **k: RespuestaFalsa(status=403))
        with pytest.raises(requests.HTTPError):
            BuscadorSearxng("http://s").buscar("x")


@pytest.mark.unit
def test_brave_manda_la_llave_y_parsea_web_results(monkeypatch):
    capturado = {}

    def get(url, params=None, headers=None, timeout=None):
        capturado.update(headers=headers, params=params)
        return RespuestaFalsa(
            json={"web": {"results": [{"title": "T", "url": "https://b.mx", "description": "d"}]}}
        )

    monkeypatch.setattr(busqueda.requests, "get", get)
    resultados = BuscadorBrave("llave").buscar("x", n=3)
    assert capturado["headers"]["X-Subscription-Token"] == "llave"
    assert capturado["params"]["country"] == "MX" and capturado["params"]["count"] == 3
    assert resultados[0].extracto == "d"


@pytest.mark.unit
class TestLeerPagina:
    def test_quita_el_ruido_y_recorta(self, monkeypatch):
        html = "<html><head><title>Tacos Paco</title><script>var x=1</script></head><body><nav>menu</nav><p>Cerrado  permanentemente</p><footer>pie</footer></body></html>"
        monkeypatch.setattr(
            paginas.requests,
            "get",
            lambda *a, **k: RespuestaFalsa(text=html, content_type="text/html"),
        )
        texto = paginas.leer_pagina("https://x.mx", max_caracteres=1000)
        assert texto.startswith("TITULO: Tacos Paco")
        assert "Cerrado permanentemente" in texto
        for ruido in ("var x", "menu", "pie"):
            assert ruido not in texto

    def test_recorta_al_tope(self, monkeypatch):
        html = "<p>" + "palabra " * 500 + "</p>"
        monkeypatch.setattr(
            paginas.requests,
            "get",
            lambda *a, **k: RespuestaFalsa(text=html, content_type="text/html"),
        )
        texto = paginas.leer_pagina("https://x.mx", max_caracteres=100)
        assert "recortado a 100" in texto and len(texto) < 200

    def test_un_404_es_texto_no_excepcion(self, monkeypatch):
        """El que lee es el modelo: un sitio caído es información, no un fallo del agente."""
        monkeypatch.setattr(
            paginas.requests,
            "get",
            lambda *a, **k: RespuestaFalsa(status=404, content_type="text/html"),
        )
        assert paginas.leer_pagina("https://x.mx").startswith(
            "ERROR: https://x.mx respondio HTTP 404"
        )

    def test_un_error_de_red_tambien(self, monkeypatch):
        def get(*a, **k):
            raise requests.ConnectionError("sin ruta")

        monkeypatch.setattr(paginas.requests, "get", get)
        assert paginas.leer_pagina("https://x.mx").startswith("ERROR: no se pudo abrir")

    def test_un_pdf_no_se_intenta_leer(self, monkeypatch):
        monkeypatch.setattr(
            paginas.requests,
            "get",
            lambda *a, **k: RespuestaFalsa(text="%PDF", content_type="application/pdf"),
        )
        assert "no es texto ni HTML" in paginas.leer_pagina("https://x.mx/a.pdf")
