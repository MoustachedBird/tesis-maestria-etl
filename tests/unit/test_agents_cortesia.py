"""Pausas, bloqueos y cortacircuitos: lo que deja leer sitios ajenos sin API, sin red.

Lo que se afirma: que una página de CAPTCHA nunca llegue al modelo como
lectura, que una página normal que menciona "captcha" en su pie no se tome
por bloqueo, que entre dos peticiones al mismo dominio se espere, y que un
motor que bloquea seguido se apague.
"""

import pytest

from src.agents.survival.sources.cortesia import (
    Bloqueado,
    Cortacircuitos,
    Pausador,
    detectar_bloqueo,
)
from tests.unit.test_agents_navegador import FICHA, DriverFalso, navegador


@pytest.mark.unit
class TestDetectarBloqueo:
    @pytest.mark.parametrize(
        "texto,url",
        [
            ("Our systems have detected unusual traffic from your computer network.", ""),
            ("Nuestros sistemas han detectado tráfico inusual en tu red.", ""),
            ("Please complete the CAPTCHA to continue", ""),
            ("Just a moment... Checking your browser before accessing", ""),
            ("Verifica que eres un humano", ""),
            ("cualquier cosa", "https://www.google.com/sorry/index?continue=x"),
        ],
    )
    def test_reconoce_un_bloqueo(self, texto, url):
        assert detectar_bloqueo(texto, url)

    def test_una_pagina_larga_que_menciona_captcha_no_es_bloqueo(self):
        texto = FICHA * 20 + " Este sitio está protegido por reCAPTCHA."
        assert detectar_bloqueo(texto) is None

    def test_una_pagina_normal_no_es_bloqueo(self):
        assert detectar_bloqueo(FICHA, "https://www.google.com/maps/place/x") is None


@pytest.mark.unit
class TestPausador:
    def test_espera_entre_dos_peticiones_al_mismo_dominio(self):
        reloj = [100.0]
        dormido: list[float] = []

        def dormir(s):
            dormido.append(s)
            reloj[0] += s

        p = Pausador(3, azar=0, reloj=lambda: reloj[0], dormir=dormir)
        p.esperar("https://www.google.com/search?q=a")
        reloj[0] += 1
        p.esperar("https://google.com/search?q=b")  # mismo dominio, sin www
        assert dormido == [2.0]

    def test_otro_dominio_no_espera(self):
        dormido: list[float] = []
        p = Pausador(3, azar=0, reloj=lambda: 0.0, dormir=dormido.append)
        p.esperar("https://www.bing.com/search?q=a")
        p.esperar("https://duckduckgo.com/html")
        assert dormido == []

    def test_el_azar_alarga_la_pausa(self):
        dormido: list[float] = []
        p = Pausador(2, azar=0.5, reloj=lambda: 0.0, dormir=dormido.append, aleatorio=lambda: 1.0)
        p.esperar("https://a.mx")
        p.esperar("https://a.mx/2")
        assert dormido == [3.0]


@pytest.mark.unit
def test_el_cortacircuitos_apaga_tras_bloqueos_seguidos():
    c = Cortacircuitos(2)
    c.registrar("google", bloqueado=True)
    c.registrar("google", bloqueado=False)  # una buena reinicia la cuenta
    c.registrar("google", bloqueado=True)
    assert not c.apagado("google")
    c.registrar("google", bloqueado=True)
    assert c.apagado("google") and not c.apagado("bing")
    assert not Cortacircuitos(0).apagado("google")  # 0 = nunca se apaga


@pytest.mark.unit
class TestElNavegadorNoLeeUnBloqueo:
    def test_leer_devuelve_error(self):
        n, _ = navegador(texto="Our systems have detected unusual traffic. " * 10)
        texto = n.leer("https://www.google.com/maps/search/x")
        assert texto.startswith("ERROR") and "bloqueo" in texto

    def test_html_lanza(self):
        n, _ = navegador(
            texto="x" * 300, destino="https://www.google.com/sorry/index?continue=https://x"
        )
        with pytest.raises(Bloqueado):
            n.html("https://www.google.com/search?q=x")

    def test_recuerda_a_donde_llego(self):
        ficha = "https://www.google.com/maps/place/Tacos+Paco/@20.6,-103.3"
        n, _ = navegador(DriverFalso(destino=ficha))
        n.leer("https://www.google.com/maps/search/tacos")
        assert n.ultima_url == ficha


@pytest.mark.unit
def test_el_desafio_de_brave_es_bloqueo_aunque_la_pagina_sea_larga():
    texto = (
        "Captcha - Brave Search Verificando que no eres un bot. Arrastra el control deslizante. "
    )
    assert detectar_bloqueo(texto + "x " * 3000)
