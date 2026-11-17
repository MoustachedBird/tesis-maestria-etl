"""El lector con navegador, contra un driver FALSO.

Sin Chrome ni red: se prueba lo que el lector decide por su cuenta --qué
URLs manda al navegador, cuándo una página no cuenta como leída, cómo anota
las fechas relativas y que un Chrome roto no tumba la corrida--, no cómo se
ve Google Maps hoy.
"""

from datetime import date

import pytest

from src.agents.survival.sources import navegador as nav
from src.agents.survival.sources.navegador import (
    Navegador,
    anotar_fechas_relativas,
    leer_con_respaldo,
    necesita_navegador,
)

HOY = date(2026, 11, 17)
FICHA = "Tacos Paco 4.5 Cerrado permanentemente Av Vallarta 1234. " + "Reseña: muy ricos. " * 15


class DriverFalso:
    """Un driver que devuelve un texto fijo y recuerda qué se le pidió."""

    def __init__(self, texto=FICHA, destino=None, titulo="Tacos Paco - Google Maps", falla=None):
        self.texto, self.destino, self.title, self.falla = texto, destino, titulo, falla
        self.urls: list[str] = []
        self.cerrado = False

    def get(self, url):
        if self.falla:
            raise self.falla
        self.urls.append(url)

    @property
    def current_url(self):
        return self.destino or self.urls[-1]

    def execute_script(self, _):
        return self.texto

    def find_elements(self, *_):
        return []

    def quit(self):
        self.cerrado = True


def navegador(driver=None, **kw) -> tuple[Navegador, list]:
    creados: list = []

    def fabrica(timeout):
        creados.append(driver or DriverFalso(**kw))
        return creados[-1]

    return Navegador(espera=0, fabrica=fabrica, hoy=lambda: HOY), creados


@pytest.mark.unit
class TestQueVaAlNavegador:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.google.com/maps/place/tacos-paco",
            "https://maps.app.goo.gl/aovUc5FRFaqBd9AM9",
            "https://www.facebook.com/p/Dulceria-El-Kiosco-100083341502594/",
            "https://m.facebook.com/tacospaco",
            "https://www.instagram.com/tacospaco/",
        ],
    )
    def test_maps_y_redes(self, url):
        assert necesita_navegador(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://tacospaco.mx",
            "https://www.google.com/search?q=x",
            "https://nota.mx/facebook.com",
        ],
    )
    def test_lo_demas_no(self, url):
        assert not necesita_navegador(url)


@pytest.mark.unit
class TestFechasRelativas:
    @pytest.mark.parametrize(
        "texto,mes",
        [
            ("hace 3 días", "2026-11"),
            ("hace un día", "2026-11"),
            ("hace 3 semanas", "2026-10"),
            ("Hace una semana", "2026-11"),
            ("hace 2 meses", "2026-09"),
            ("hace un mes", "2026-10"),
            ("hace 11 meses", "2025-12"),
            ("hace un año", "2025-11"),
            ("hace 4 años", "2022-11"),
        ],
    )
    def test_se_anota_el_mes_aproximado(self, texto, mes):
        assert anotar_fechas_relativas(f"Reseña {texto}.", HOY) == f"Reseña {texto} [≈ {mes}]."

    def test_lo_que_no_es_fecha_no_se_toca(self):
        texto = "Abierto hace mucho. Se hace pan. 5 de noviembre de 2026."
        assert anotar_fechas_relativas(texto, HOY) == texto


@pytest.mark.unit
class TestLeer:
    def test_devuelve_titulo_y_texto_con_las_fechas_anotadas(self):
        n, creados = navegador(texto=FICHA + " Última reseña hace 2 meses.")
        texto = n.leer("https://www.google.com/maps/place/x")
        assert texto.startswith("TITULO: Tacos Paco - Google Maps")
        assert "Cerrado permanentemente" in texto and "hace 2 meses [≈ 2026-09]" in texto
        assert creados[0].urls == ["https://www.google.com/maps/place/x"]

    def test_un_solo_chrome_para_varias_lecturas(self):
        n, creados = navegador()
        n.leer("https://www.facebook.com/a")
        n.leer("https://www.facebook.com/b")
        assert len(creados) == 1

    def test_una_pagina_vacia_no_cuenta_como_leida(self):
        n, _ = navegador(texto="Inicia sesión")
        texto = n.leer("https://www.facebook.com/a")
        assert texto.startswith("ERROR") and "NO la leíste" in texto

    def test_la_redireccion_al_login_es_error(self):
        n, _ = navegador(destino="https://www.facebook.com/login/?next=x")
        assert "pide iniciar sesión" in n.leer("https://www.facebook.com/a")

    def test_recorta_al_tope(self):
        n, _ = navegador(texto="palabra " * 2000)
        assert "recortado a 500" in n.leer("https://www.facebook.com/a", max_caracteres=500)

    def test_un_chrome_roto_es_texto_y_el_siguiente_abre_otro(self):
        roto = DriverFalso(falla=RuntimeError("tab crashed"))
        creados: list = []

        def fabrica(timeout):
            creados.append(roto if not creados else DriverFalso())
            return creados[-1]

        n = Navegador(espera=0, fabrica=fabrica, hoy=lambda: HOY)
        assert n.leer("https://www.facebook.com/a").startswith("ERROR: el navegador no pudo abrir")
        assert roto.cerrado
        assert not n.leer("https://www.facebook.com/a").startswith("ERROR")
        assert len(creados) == 2

    def test_comprobar_propaga_que_no_hay_chrome(self):
        def fabrica(timeout):
            raise RuntimeError("chrome not found")

        with pytest.raises(RuntimeError):
            Navegador(fabrica=fabrica).comprobar()


@pytest.mark.unit
def test_buscar_en_maps_arma_la_url_citable():
    n, creados = navegador()
    url, texto = n.buscar_en_maps("TACOS PACO Guadalajara, Jalisco")
    assert url == "https://www.google.com/maps/search/TACOS%20PACO%20Guadalajara%2C%20Jalisco?hl=es"
    assert creados[0].urls == [url] and "Cerrado permanentemente" in texto


@pytest.mark.unit
class TestLeerConRespaldo:
    def test_las_redes_no_pasan_por_requests(self, monkeypatch):
        monkeypatch.setattr(nav, "leer_pagina", lambda *a: pytest.fail("no debio usar requests"))
        n, creados = navegador()
        assert "Cerrado permanentemente" in leer_con_respaldo("https://www.facebook.com/a", n)

    def test_un_sitio_normal_no_abre_chrome(self, monkeypatch):
        monkeypatch.setattr(nav, "leer_pagina", lambda *a: "TITULO: x\ncontenido")
        n, creados = navegador()
        assert leer_con_respaldo("https://tacospaco.mx", n) == "TITULO: x\ncontenido"
        assert creados == []

    def test_un_sitio_que_llega_vacio_se_reintenta_con_chrome(self, monkeypatch):
        vacio = f"ERROR: https://tacospaco.mx {nav.SIN_CONTENIDO} (12 caracteres)"
        monkeypatch.setattr(nav, "leer_pagina", lambda *a: vacio)
        n, creados = navegador()
        assert "Cerrado permanentemente" in leer_con_respaldo("https://tacospaco.mx", n)
        assert len(creados) == 1

    def test_un_404_no_se_reintenta(self, monkeypatch):
        monkeypatch.setattr(nav, "leer_pagina", lambda *a: "ERROR: x respondio HTTP 404")
        n, creados = navegador()
        assert "404" in leer_con_respaldo("https://tacospaco.mx/x", n)
        assert creados == []
