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
    presupuesto_de,
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
            "TACOS PACO AV VALLARTA 1234 AMERICANA 44160, Guadalajara, Jalisco",
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
            "TACOS PACO AV VALLARTA 1234 AMERICANA 44160, Guadalajara, Jalisco",
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

    def test_busca_tambien_los_otros_nombres_del_planificador(self):
        buscador = BuscadorFalso()
        n = negocio(nombres_busqueda=["Tacos Paco", "Taquería Don Paco"])
        FuenteWeb(buscador, 8).recolectar(n, [])
        assert buscador.consultas[0] == '"Tacos Paco" Guadalajara, Jalisco'
        assert buscador.consultas[-1] == '"Taquería Don Paco" Guadalajara, Jalisco'
        nav = NavegadorFalso()
        FuenteMaps(nav, 6000, 200).recolectar(n, [])
        assert nav.maps[-1] == "Taquería Don Paco Guadalajara, Jalisco"

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

    def test_la_pagina_del_negocio_antes_que_publicaciones_y_grupos(self):
        nav = NavegadorFalso()
        grupo = "https://www.facebook.com/groups/1803558436594459/posts/4691821994434741/"
        post = "https://www.facebook.com/100083341502594/posts/526093946845318/"
        pagina = "https://www.facebook.com/p/Dulceria-El-Kiosco-100083341502594/"
        ig_post = "https://www.instagram.com/p/C1x2y3/"
        FuenteRedes(nav, 3, 6000, 200).recolectar(negocio(), _web(grupo, post, ig_post, pagina))
        assert nav.leidas == [pagina, post, ig_post]

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


@pytest.mark.unit
class TestElExpedienteCabeEnElContexto:
    def test_sin_presupuesto_o_si_cabe_no_se_toca(self):
        piezas = _web("https://a.mx", "https://b.mx")
        completo = formatear_expediente(piezas)
        assert formatear_expediente(piezas, presupuesto=10_000) == completo

    def test_recorta_parejo_las_mas_largas(self):
        corta = Pieza("web", "corta", "ok", url="https://a.mx")
        largas = [Pieza("redes", "x" * 5000, "ok", url=f"https://b.mx/{i}") for i in range(3)]
        texto = formatear_expediente([corta, *largas], presupuesto=4000)
        assert len(texto) <= 4000 + 4 * len("[... recortado para caber en el contexto del modelo]")
        assert texto.count("recortado para caber") == 3  # la corta quedó intacta
        assert "corta" in texto and "[4] redes" in texto

    def test_presupuesto_de(self):
        assert presupuesto_de(32768) == int(32768 * 3.0 * 0.6)


@pytest.mark.unit
def test_plataformas_sin_nada_que_abrir_no_llama_al_modelo():
    from src.agents.survival.equipo import Especialista
    from src.agents.survival.recoleccion import Grupo
    from tests.unit.test_agents_verificador import ClienteFalso

    cliente = ClienteFalso()
    grupo = Grupo("plataformas", [FuentePlataformas(NavegadorFalso(), 3, 6000, 200)])
    exp = _web("https://nota.mx/1")  # ninguna ficha de plataforma
    i = Especialista(cliente, max_ampliaciones=2, max_intentos=2).informar(negocio(), grupo, exp)
    assert i.automatico and cliente.conversaciones == []


@pytest.mark.unit
def test_el_archivo_busca_paginas_de_redes_no_publicaciones():
    from src.agents.survival.fuentes_abiertas import urls_para_archivo

    post = "https://www.facebook.com/100083341502594/posts/526093946845318/"
    grupo = "https://www.facebook.com/groups/1803558436594459/posts/4691821994434741/"
    pagina = "https://www.facebook.com/p/Dulceria-El-Kiosco-100083341502594/"
    assert urls_para_archivo(negocio(sitios=[]), _web(post, grupo, pagina), 3) == [pagina]


@pytest.mark.unit
def test_una_pieza_nace_sin_nul():
    """PostgreSQL rechaza el NUL en TEXT; una página que lo traía tumbó la muestra."""
    p = Pieza("web", "Tienda\x00 Fabys", "ok", titulo="T\x00", url="https://a.mx/\x00")
    assert "\x00" not in p.contenido + p.titulo + p.url
    assert p.contenido == "Tienda Fabys"


@pytest.mark.unit
class TestQueEsUnaFichaDePlataforma:
    @pytest.mark.parametrize(
        "url",
        [
            "https://www.tripadvisor.com.mx/Restaurant_Review-g150801-d123-Tacos_Paco.html",
            "https://www.yelp.com.mx/biz/tacos-paco-guadalajara",
            "https://www.rappi.com.mx/restaurantes/123-tacos-paco",
            "https://restaurantguru.com/Tacos-Paco-Guadalajara",
        ],
    )
    def test_fichas(self, url):
        from src.agents.survival.recoleccion import es_plataforma

        assert es_plataforma(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://www.seccionamarilla.com.mx/",
            "https://www.tripadvisor.com.mx/Attractions-g60880-Activities-Anchorage_Alaska.html",
            "https://www.yelp.com.mx/search?find_desc=tacos",
            "https://nota.mx/rappi.",
        ],
    )
    def test_portadas_listas_y_otros_dominios_no(self, url):
        from src.agents.survival.recoleccion import es_plataforma

        assert not es_plataforma(url)


@pytest.mark.unit
def test_maps_en_vista_limitada_no_busca_resenas():
    class NavLimitado(NavegadorFalso):
        def __init__(self):
            super().__init__(
                texto="Dulceria Ilse · Estás en una vista limitada de Google Maps. " * 5
            )
            self.ultima_url = "https://www.google.com/maps/place/Dulceria+Ilse/@20,-100"

        def resenas_recientes(self, *a, **k):
            raise AssertionError("no debía buscar la pestaña de reseñas")

    piezas = FuenteMaps(NavLimitado(), 6000, 200).recolectar(negocio(), [])
    assert all(p.consulta != "reseñas más recientes de la ficha" for p in piezas)


@pytest.mark.unit
class TestElLugar:
    def test_localidad_municipio_y_estado(self):
        from src.agents.survival.recoleccion import lugar_de

        n = negocio(localidad="SAN ISIDRO", municipio="ACATEPEC", entidad="GUERRERO")
        assert lugar_de(n) == "SAN ISIDRO, ACATEPEC, GUERRERO"

    def test_si_localidad_y_municipio_se_llaman_igual_va_una_vez(self):
        from src.agents.survival.recoleccion import lugar_de

        n = negocio(localidad="XALAPA-ENRÍQUEZ", municipio="Xalapa", entidad="Veracruz")
        assert lugar_de(n) == "XALAPA-ENRÍQUEZ, Xalapa, Veracruz"
        n = negocio(
            localidad="XALAPA", municipio="Xalapa", entidad="Veracruz de Ignacio de la Llave"
        )
        assert lugar_de(n) == "XALAPA, Veracruz de Ignacio de la Llave"

    def test_la_consulta_de_domicilio_lleva_el_lugar_completo(self):
        nav = NavegadorFalso()
        n = negocio(localidad="XALAPA", municipio="Xalapa", entidad="Veracruz")
        FuenteMaps(nav, 6000, 200).recolectar(n, [])
        assert nav.maps[1] == "TACOS PACO AV VALLARTA 1234 AMERICANA 44160, XALAPA, Veracruz"


@pytest.mark.unit
def test_un_domicilio_del_planificador_se_busca_tal_cual():
    nav = NavegadorFalso()
    n = negocio(
        domicilios_busqueda=["Av. Vallarta 1234, Americana, Guadalajara, Jal."],
        lugar_busqueda="Guadalajara, Jalisco",
    )
    FuenteMaps(nav, 6000, 200).recolectar(n, [])
    assert nav.maps == [
        "TACOS PACO Guadalajara, Jalisco",
        "TACOS PACO Av. Vallarta 1234, Americana, Guadalajara, Jal.",
    ]
