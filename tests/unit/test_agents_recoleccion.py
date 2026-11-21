"""Las fuentes del equipo contra un buscador y un navegador FALSOS.

Lo que se afirma es lo que hace comparables los veredictos: que la
recolección base sea siempre la misma (mismas consultas, mismo orden), que
lo que falla también quede como pieza, que un resultado repetido no se
guarde dos veces, que redes solo abra lo que salió del buscador y que lo
pedido por un especialista quede marcado `ampliada`.
"""

import pytest

from src.agents.survival.recoleccion import (
    FuenteMaps,
    FuentePlataformas,
    FuenteRedes,
    FuenteSitio,
    FuenteWeb,
    Pieza,
    clave_url,
    formatear_expediente,
)
from src.agents.survival.sources.busqueda import Resultado
from tests.unit.test_agents_verificador import negocio

FB = "https://www.facebook.com/tacospacogdl"
IG = "https://www.instagram.com/tacospaco/"


class NavegadorFalso:
    def __init__(self, texto="Tacos Paco · Abierto · " + "reseña " * 40):
        self.texto = texto
        self.maps: list[str] = []
        self.leidas: list[str] = []

    def buscar_en_maps(self, consulta, max_caracteres=6000, min_caracteres=200):
        self.maps.append(consulta)
        return f"https://www.google.com/maps/search/{len(self.maps)}", self.texto

    def leer(self, url, max_caracteres=6000, min_caracteres=200):
        self.leidas.append(url)
        return self.texto


class BuscadorFalso:
    nombre = "falso"

    def __init__(self, por_consulta=None, falla=False):
        self.por_consulta = por_consulta or {}
        self.falla = falla
        self.consultas: list[str] = []

    def buscar(self, consulta, n=8):
        self.consultas.append(consulta)
        if self.falla:
            raise RuntimeError("searxng caido")
        return self.por_consulta.get(consulta, [])[:n]


@pytest.mark.unit
class TestMaps:
    def test_nombre_mas_lugar_y_nombre_mas_domicilio(self):
        nav = NavegadorFalso()
        piezas = FuenteMaps(nav, 6000, 200).recolectar(negocio(), [])
        assert nav.maps == [
            "TACOS PACO Guadalajara, Jalisco",
            "TACOS PACO AV VALLARTA 1234 AMERICANA 44160 Guadalajara",
        ]
        assert all(p.recolector == "maps" and p.citable and not p.ampliada for p in piezas)

    def test_sin_domicilio_una_sola_consulta(self):
        nav = NavegadorFalso()
        FuenteMaps(nav, 6000, 200).recolectar(negocio(domicilios=[]), [])
        assert len(nav.maps) == 1

    def test_un_error_es_pieza_no_citable(self):
        nav = NavegadorFalso(texto="ERROR: pide iniciar sesión")
        piezas = FuenteMaps(nav, 6000, 200).recolectar(negocio(), [])
        assert all(p.estado == "error" and not p.citable for p in piezas)

    def test_la_ampliacion_queda_marcada(self):
        nav = NavegadorFalso()
        (p,) = FuenteMaps(nav, 6000, 200).ampliar(negocio(), {"consulta": "DON PACO"}, [])
        assert p.ampliada and p.consulta == "DON PACO"
        assert FuenteMaps(nav, 6000, 200).ampliar(negocio(), {"consulta": " "}, []) == []


@pytest.mark.unit
class TestWeb:
    def test_las_consultas_base_son_siempre_las_mismas(self):
        buscador = BuscadorFalso()
        FuenteWeb(buscador, 8).recolectar(negocio(), [])
        assert buscador.consultas == [
            '"TACOS PACO" Guadalajara, Jalisco',
            "TACOS PACO AV VALLARTA 1234 AMERICANA 44160 Guadalajara",
            "3312345678",
            "TACOS PACO Guadalajara facebook",
            "TACOS PACO Guadalajara opiniones",
        ]

    def test_sin_telefono_ni_domicilio(self):
        buscador = BuscadorFalso()
        FuenteWeb(buscador, 8).recolectar(negocio(telefonos=[], domicilios=[]), [])
        assert len(buscador.consultas) == 3

    def test_un_resultado_repetido_se_guarda_una_vez_y_sin_resultados_es_pieza(self):
        r = Resultado("Tacos Paco", "https://www.tacospaco.mx/", "abierto")
        buscador = BuscadorFalso({'"TACOS PACO" Guadalajara, Jalisco': [r], "3312345678": [r]})
        piezas = FuenteWeb(buscador, 8).recolectar(negocio(), [])
        urls = [p.url for p in piezas if p.url]
        assert urls == ["https://www.tacospaco.mx/"]
        assert sum(p.estado == "sin_resultados" for p in piezas) == 3

    def test_un_buscador_caido_es_pieza_error(self):
        piezas = FuenteWeb(BuscadorFalso(falla=True), 8).recolectar(negocio(), [])
        assert piezas and all(p.estado == "error" for p in piezas)

    def test_otro_buscador_no_repite_lo_que_trajo_el_primero(self):
        r = Resultado("Tacos Paco", "https://www.tacospaco.mx/", "abierto")
        q = '"TACOS PACO" Guadalajara, Jalisco'
        primero = FuenteWeb(BuscadorFalso({q: [r]}), 8).recolectar(negocio(), [])
        bing = FuenteWeb(BuscadorFalso({q: [r]}), 8, recolector="bing")
        segundo = bing.recolectar(negocio(), primero)
        assert all(p.url is None for p in segundo)
        assert all(p.recolector == "bing" for p in segundo)
        assert bing.herramienta["function"]["name"] == "buscar_en_bing"

    def test_la_ampliacion_no_repite_lo_que_ya_estaba(self):
        viejo = Pieza("web", "x", "ok", url="https://tacospaco.mx")
        r1 = Resultado("a", "https://www.tacospaco.mx/", "dup")
        r2 = Resultado("b", "https://nota.mx/1", "nuevo")
        piezas = FuenteWeb(BuscadorFalso({"otra": [r1, r2]}), 8).ampliar(
            negocio(), {"consulta": "otra"}, [viejo]
        )
        assert [p.url for p in piezas] == ["https://nota.mx/1"] and piezas[0].ampliada


def _web(*urls: str, recolector: str = "web") -> list[Pieza]:
    return [Pieza(recolector, "extracto", "ok", consulta="q", url=u) for u in urls]


@pytest.mark.unit
class TestRedes:
    def test_abre_las_redes_que_trajo_el_buscador(self):
        nav = NavegadorFalso()
        exp = _web("https://nota.mx/1", FB, IG, FB + "/")
        piezas = FuenteRedes(nav, 2, 6000, 200).recolectar(negocio(), exp)
        assert nav.leidas == [FB, IG] and all(p.recolector == "redes" for p in piezas)

    def test_abre_lo_que_trajo_cualquier_buscador(self):
        nav = NavegadorFalso()
        FuenteRedes(nav, 2, 6000, 200).recolectar(negocio(), _web(IG, recolector="duckduckgo"))
        assert nav.leidas == [IG]

    def test_respeta_el_tope(self):
        nav = NavegadorFalso()
        FuenteRedes(nav, 1, 6000, 200).recolectar(negocio(), _web(FB, IG))
        assert nav.leidas == [FB]

    def test_no_abre_una_url_que_no_salio_del_buscador(self):
        nav = NavegadorFalso()
        (p,) = FuenteRedes(nav, 1, 6000, 200).ampliar(
            negocio(), {"url": "https://www.facebook.com/otro"}, _web(FB)
        )
        assert p.estado == "error" and nav.leidas == []

    def test_el_contexto_lista_lo_que_falta_abrir(self):
        exp = _web(FB, IG) + [Pieza("redes", "x", "ok", url=FB)]
        texto = FuenteRedes(NavegadorFalso(), 1, 6000, 200).contexto(exp)
        assert IG in texto and FB not in texto


@pytest.mark.unit
class TestPlataformas:
    def test_abre_solo_fichas_de_plataformas(self):
        nav = NavegadorFalso()
        trip = "https://www.tripadvisor.com.mx/Restaurant_Review-g1-d2-Tacos_Paco.html"
        rappi = "https://www.rappi.com.mx/restaurantes/123-tacos-paco"
        exp = _web(FB, "https://nota.mx/1", trip, recolector="google") + _web(rappi)
        piezas = FuentePlataformas(nav, 3, 6000, 200).recolectar(negocio(), exp)
        assert nav.leidas == [trip, rappi]
        assert all(p.recolector == "plataformas" for p in piezas)

    def test_su_herramienta_tiene_otro_nombre(self):
        f = FuentePlataformas(NavegadorFalso(), 1, 6000, 200)
        assert f.herramienta["function"]["name"] == "leer_plataforma"


@pytest.mark.unit
class TestSitio:
    def test_lee_los_sitios_del_denue_con_esquema(self):
        leidas: list[str] = []

        def leer(url):
            leidas.append(url)
            return "ERROR: no se pudo abrir" if "caido" in url else "Bienvenidos"

        piezas = FuenteSitio(leer, 2).recolectar(
            negocio(sitios=["tacospaco.mx", "http://caido.mx", "tercero.mx"]), []
        )
        assert leidas == ["https://tacospaco.mx", "http://caido.mx"]
        assert [p.estado for p in piezas] == ["ok", "error"]

    def test_no_se_amplia(self):
        assert FuenteSitio(lambda u: "", 2).herramienta is None


@pytest.mark.unit
def test_el_expediente_numera_con_el_numero_global():
    texto = formatear_expediente(_web("https://a.mx", "https://b.mx"), [7, 9])
    assert texto.startswith("[7] web") and "[9] web" in texto


@pytest.mark.unit
def test_clave_url():
    assert clave_url("https://www.Facebook.com/x/") == clave_url("http://facebook.com/x")
