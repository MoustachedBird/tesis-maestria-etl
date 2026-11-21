"""Los buscadores leídos como página, contra HTML guardado y sin red.

El HTML de aquí imita la forma de cada motor, no la copia: lo que se afirma
es lo que el parser decide --sacar la URL real de un enlace de seguimiento,
saltar anuncios y enlaces del propio motor, que un CAPTCHA lance `Bloqueado`
y no "sin resultados", que un motor que bloquea seguido se apague--. Si un
motor cambia su HTML, esto no lo detecta: lo detecta `sondear` en indominus.
"""

import base64

import pytest

from src.agents.survival.sources import buscadores as b
from src.agents.survival.sources.busqueda import Resultado
from src.agents.survival.sources.cortesia import Bloqueado, Cortacircuitos


def _bing_href(url: str) -> str:
    cod = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
    return f"https://www.bing.com/ck/a?!&&p=abc&u=a1{cod}&ntb=1"


BING = f"""
<ol id="b_results">
  <li class="b_algo"><h2><a href="{_bing_href("https://www.tacospaco.mx/")}">Tacos Paco</a></h2>
    <div class="b_caption"><p>3 nov 2025 · Los mejores tacos de Guadalajara.</p></div></li>
  <li class="b_algo"><h2><a href="https://www.facebook.com/tacospaco">Tacos Paco | Facebook</a></h2>
    <div class="b_caption"><p>Tacos Paco. 1,234 Me gusta.</p></div></li>
  <li class="b_algo"><h2><a href="https://www.bing.com/maps?q=x">Mapa</a></h2></li>
</ol>
"""

DDG = """
<div class="result results_links result--ad"><a class="result__a" href="https://anuncio.com">Anuncio</a></div>
<div class="result results_links web-result">
  <h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.tripadvisor.com.mx%2FRestaurant_Review-tacos&rut=x">Tacos Paco - Tripadvisor</a></h2>
  <a class="result__snippet">Cerrado permanentemente. Opiniones de Tacos Paco.</a>
</div>
"""

GOOGLE = """
<div id="search">
  <div class="g"><div><a href="https://www.tacospaco.mx/"><h3>Tacos Paco - Inicio</h3><cite>tacospaco.mx</cite></a></div>
    <div><span>Taquería en Av Vallarta 1234. Abierto todos los días.</span></div></div>
  <div class="g"><div><a href="https://www.google.com/maps/place/x"><h3>Tacos Paco - Google Maps</h3></a></div></div>
  <div class="g"><div><a href="https://nota.mx/1"><h3>Cierra Tacos Paco</h3></a></div>
    <div><span>Publicado el 5 de noviembre de 2026.</span></div></div>
</div>
"""

BRAVE = """
<div class="snippet" data-type="web"><a href="https://www.yelp.com.mx/biz/tacos-paco"><div class="title">Tacos Paco - Yelp</div></a>
  <div class="snippet-description">Cerrado definitivamente.</div></div>
<div class="snippet" data-type="news"><a href="https://n.mx"><div class="title">Noticia</div></a></div>
"""


@pytest.mark.unit
class TestParsers:
    def test_bing_saca_la_url_real_y_salta_lo_propio(self):
        rs = b.resultados_bing(BING)
        assert [r.url for r in rs] == [
            "https://www.tacospaco.mx/",
            "https://www.facebook.com/tacospaco",
        ]
        assert rs[0].titulo == "Tacos Paco" and "3 nov 2025" in rs[0].extracto

    def test_duckduckgo_salta_anuncios_y_desenvuelve_el_enlace(self):
        (r,) = b.resultados_duckduckgo(DDG)
        assert r.url == "https://www.tripadvisor.com.mx/Restaurant_Review-tacos"
        assert r.extracto == "Cerrado permanentemente. Opiniones de Tacos Paco."

    def test_google_por_h3_con_el_texto_del_bloque(self):
        rs = b.resultados_google(GOOGLE)
        assert [r.url for r in rs] == ["https://www.tacospaco.mx/", "https://nota.mx/1"]
        assert "Av Vallarta 1234" in rs[0].extracto and "Tacos Paco - Inicio" not in rs[0].extracto
        assert "5 de noviembre de 2026" in rs[1].extracto

    def test_brave_solo_resultados_web(self):
        (r,) = b.resultados_brave(BRAVE)
        assert (
            r.url == "https://www.yelp.com.mx/biz/tacos-paco"
            and r.extracto == "Cerrado definitivamente."
        )

    def test_si_el_selector_no_encuentra_nada_hay_respaldo(self):
        html = '<div><a href="https://otro.mx"><h2>Otro formato</h2></a><p>Texto del resultado.</p></div>'
        (r,) = b.resultados_bing(html)
        assert r.url == "https://otro.mx" and "Texto del resultado" in r.extracto

    def test_extracto_acotado(self):
        largo = "palabra " * 200
        html = f'<li class="b_algo"><h2><a href="https://a.mx">A</a></h2><p>{largo}</p></li>'
        (r,) = b.resultados_bing(html)
        assert len(r.extracto) <= 502 and r.extracto.endswith("…")


class _Resp:
    def __init__(self, texto, status=200, url="https://www.bing.com/search"):
        self.text, self.status_code, self.url = texto, status, url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


@pytest.mark.unit
class TestConRequests:
    def test_bing_busca_en_espanol_de_mexico(self, monkeypatch):
        capturado = {}

        def get(url, params=None, headers=None, timeout=None):
            capturado.update(params=params, headers=headers)
            return _Resp(BING)

        monkeypatch.setattr(b.requests, "get", get)
        rs = b.BuscadorBing().buscar("tacos paco", n=1)
        assert len(rs) == 1 and capturado["params"]["cc"] == "MX"
        assert "Chrome" in capturado["headers"]["User-Agent"]

    def test_un_captcha_lanza_bloqueado(self, monkeypatch):
        monkeypatch.setattr(
            b.requests, "post", lambda *a, **k: _Resp("Unfortunately, bots use DuckDuckGo too.")
        )
        with pytest.raises(Bloqueado):
            b.BuscadorDuckDuckGo().buscar("tacos")

    def test_un_429_es_bloqueo(self, monkeypatch):
        monkeypatch.setattr(b.requests, "get", lambda *a, **k: _Resp("", status=429))
        with pytest.raises(Bloqueado):
            b.BuscadorBing().buscar("tacos")


class _NavFalso:
    def __init__(self, html="", bloquea=False):
        self.html_, self.bloquea, self.urls = html, bloquea, []

    def html(self, url, min_caracteres=200):
        self.urls.append(url)
        if self.bloquea:
            raise Bloqueado("www.google.com", "captcha")
        return self.html_


@pytest.mark.unit
def test_google_va_por_el_navegador_en_espanol():
    nav = _NavFalso(GOOGLE)
    rs = b.BuscadorGoogle(nav).buscar("tacos paco guadalajara")
    assert len(rs) == 2 and "hl=es" in nav.urls[0] and "gl=mx" in nav.urls[0]


@pytest.mark.unit
class TestVigilado:
    def test_se_apaga_tras_bloqueos_seguidos(self):
        nav = _NavFalso(bloquea=True)
        v = b.BuscadorVigilado(b.BuscadorGoogle(nav), Cortacircuitos(2))
        for _ in range(2):
            with pytest.raises(Bloqueado):
                v.buscar("x")
        with pytest.raises(RuntimeError, match="apagado"):
            v.buscar("x")
        assert len(nav.urls) == 2  # la tercera ya no se pidió

    def test_conserva_el_nombre(self):
        class Uno:
            nombre = "uno"

            def buscar(self, consulta, n=8):
                return [Resultado("a", "https://a.mx", "x")]

        v = b.BuscadorVigilado(Uno(), Cortacircuitos(2))
        assert v.nombre == "uno" and len(v.buscar("q")) == 1
