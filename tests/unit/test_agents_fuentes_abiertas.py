"""Las fuentes de v5 que leen datos abiertos, contra respuestas guardadas y sin red.

Lo que se afirma es lo que decide el código --qué negocio mapeado se le
muestra al modelo y con qué datos para identificarlo, qué fecha queda escrita
para poder citarla, que un servicio caído sea una pieza ERROR y no una
caída--, no que los servicios respondan hoy igual (eso lo dice `sondear`).
"""

from datetime import date
from pathlib import Path

import pytest

from src.agents.survival.fuentes_abiertas import (
    FuenteCommonCrawl,
    FuenteDominio,
    FuenteOsm,
    FuenteSat,
    FuenteWayback,
    urls_para_archivo,
)
from src.agents.survival.recoleccion import FuenteMaps, Pieza
from src.agents.survival.sources import archivo, dominio, osm, sat
from tests.unit.test_agents_recoleccion import NavegadorFalso
from tests.unit.test_agents_verificador import negocio


class _Resp:
    def __init__(self, datos=None, status=200, texto=""):
        self.datos, self.status_code, self.text = datos, status, texto
        self.headers = {}

    def json(self):
        return self.datos

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


OVERPASS = {
    "elements": [
        {  # el negocio, a unos metros, con su fecha de comprobacion en campo
            "type": "node",
            "id": 1,
            "lat": 20.6701,
            "lon": -103.3501,
            "timestamp": "2024-05-03T10:00:00Z",
            "tags": {
                "name": "Tacos Paco",
                "amenity": "fast_food",
                "check_date": "2024-04-20",
                "opening_hours": "Mo-Su 09:00-18:00",
            },
        },
        {  # un local desocupado en el mismo punto
            "type": "way",
            "id": 2,
            "center": {"lat": 20.6702, "lon": -103.3500},
            "timestamp": "2025-01-10T00:00:00Z",
            "tags": {"disused:shop": "bakery", "name": "Panadería La Espiga"},
        },
        {  # una calle: no es negocio
            "type": "way",
            "id": 3,
            "center": {"lat": 20.67, "lon": -103.35},
            "tags": {"name": "Avenida Vallarta", "highway": "primary"},
        },
    ]
}


@pytest.mark.unit
class TestOsm:
    def test_ordena_por_parecido_y_deja_fuera_las_calles(self, monkeypatch):
        monkeypatch.setattr(osm.requests, "post", lambda *a, **k: _Resp(OVERPASS))
        objetos = osm.buscar_cerca(20.67, -103.35, ["TACOS PACO"], radio_m=150, url="http://o")
        assert [o.id for o in objetos] == [1, 2]
        assert objetos[0].similitud == 100 and objetos[0].distancia_m < 20
        assert objetos[1].ciclo_de_vida == ("disused:shop=bakery",)

    def test_la_pieza_trae_lo_necesario_para_identificar_y_fechar(self, monkeypatch):
        monkeypatch.setattr(osm.requests, "post", lambda *a, **k: _Resp(OVERPASS))
        (p,) = FuenteOsm(url="http://o", radio_m=150, timeout=5).recolectar(negocio(), [])
        assert p.citable and p.recolector == "osm" and p.fuente == "openstreetmap"
        for dato in (
            "«Tacos Paco»",
            "amenity=fast_food",
            "nombre 100/100",
            "check_date=2024-04-20",
            "CICLO DE VIDA: disused:shop=bakery",
            "última edición en OSM: 2024-05-03",
        ):
            assert dato in p.contenido
        assert "Avenida Vallarta" not in p.contenido

    def test_sin_coordenadas_no_consulta(self, monkeypatch):
        def no_llamar(*a, **k):
            raise AssertionError("no debía consultar")

        monkeypatch.setattr(osm.requests, "post", no_llamar)
        (p,) = FuenteOsm(url="http://o", radio_m=150, timeout=5).recolectar(
            negocio(lat=None, lon=None), []
        )
        assert not p.citable

    def test_overpass_caido_es_error(self, monkeypatch):
        monkeypatch.setattr(osm.requests, "post", lambda *a, **k: _Resp(status=500))
        (p,) = FuenteOsm(url="http://o", radio_m=150, timeout=5).recolectar(negocio(), [])
        assert p.estado == "error"

    def test_nada_cerca_es_sin_resultados(self, monkeypatch):
        monkeypatch.setattr(osm.requests, "post", lambda *a, **k: _Resp({"elements": []}))
        (p,) = FuenteOsm(url="http://o", radio_m=150, timeout=5).recolectar(negocio(), [])
        assert p.estado == "sin_resultados"


class _NavConFicha(NavegadorFalso):
    """Toda búsqueda cae en la misma ficha, que tiene reseñas."""

    FICHA = "https://www.google.com/maps/place/Tacos+Paco/@20.67,-103.35,17z/data=!3m1"

    def __init__(self):
        super().__init__()
        self.ultima_url = None
        self.resenas = 0

    def buscar_en_maps(self, consulta, max_caracteres=6000, min_caracteres=200):
        url, texto = super().buscar_en_maps(consulta, max_caracteres, min_caracteres)
        self.ultima_url = self.FICHA
        return url, texto

    def resenas_recientes(self, max_caracteres=6000, min_caracteres=200):
        self.resenas += 1
        return "Juan · hace 2 meses [≈ 2026-09] · Muy ricos. " * 10


@pytest.mark.unit
def test_maps_lee_las_resenas_recientes_una_vez_por_ficha():
    nav = _NavConFicha()
    piezas = FuenteMaps(nav, 6000, 200).recolectar(negocio(), [])
    # Dos búsquedas que caen en la misma ficha: dos piezas de búsqueda, una de reseñas.
    assert len(piezas) == 3 and nav.resenas == 1
    assert piezas[1].consulta.startswith("reseñas más recientes") and piezas[1].url == nav.FICHA


# ----- archivo web -----

WAYBACK_PRIMERA = (
    '[["timestamp","statuscode","original"],["20150302101010","200","http://tacospaco.mx/"]]'
)
WAYBACK_RECIENTES = (
    '[["timestamp","statuscode","original"],'
    '["20221105000000","200","http://tacospaco.mx/"],'
    '["20230612000000","404","http://tacospaco.mx/"]]'
)


class _Texto(_Resp):
    def __init__(self, texto, status=200):
        super().__init__(status=status, texto=texto)


@pytest.mark.unit
class TestWayback:
    def test_indice_y_lectura_de_la_ultima_buena(self, monkeypatch):
        def get(url, params=None, headers=None, timeout=None):
            return _Texto(WAYBACK_PRIMERA if params.get("limit") == "1" else WAYBACK_RECIENTES)

        monkeypatch.setattr(archivo.requests, "get", get)
        leidas: list[str] = []

        def leer(url):
            leidas.append(url)
            return "TITULO: Tacos Paco\nMenú y horarios. " * 20

        piezas = FuenteWayback(leer, max_urls=3).recolectar(negocio(sitios=["tacospaco.mx"]), [])
        indice, captura = piezas
        assert "primera captura: 2015-03-02" in indice.contenido
        assert "2022-11-05 (HTTP 200), 2023-06-12 (HTTP 404)" in indice.contenido
        assert indice.fuente == "wayback" and indice.citable
        # Se lee la más reciente que respondió 200, cruda (id_), y la fecha queda escrita.
        assert leidas == ["https://web.archive.org/web/20221105000000id_/http://tacospaco.mx/"]
        assert captura.contenido.startswith("Captura del Internet Archive del 2022-11-05")

    def test_sin_capturas(self, monkeypatch):
        monkeypatch.setattr(archivo.requests, "get", lambda *a, **k: _Texto("[]"))
        (p,) = FuenteWayback(lambda u: "", max_urls=3).recolectar(
            negocio(sitios=["tacospaco.mx"]), []
        )
        assert p.estado == "sin_resultados"

    def test_tambien_busca_las_redes_halladas(self):
        exp = [Pieza("bing", "x", "ok", url="https://www.facebook.com/tacospaco")]
        urls = urls_para_archivo(negocio(sitios=["tacospaco.mx"]), exp, 3)
        assert urls == ["https://tacospaco.mx", "https://www.facebook.com/tacospaco"]


@pytest.mark.unit
def test_common_crawl_dice_en_que_crawl_aparecio(monkeypatch):
    crawls = [
        {"id": "CC-MAIN-2026-40", "name": "October 2026 Index", "cdx-api": "https://cc/40"},
        {"id": "CC-MAIN-2026-33", "name": "August 2026 Index", "cdx-api": "https://cc/33"},
    ]

    def get(url, params=None, headers=None, timeout=None):
        if url.endswith("collinfo.json"):
            r = _Resp(crawls)
            return r
        if url.endswith("/40"):
            return _Texto(
                '{"timestamp": "20261003120000", "status": "200", "url": "https://tacospaco.mx/"}'
            )
        return _Texto("", status=404)

    monkeypatch.setattr(archivo.requests, "get", get)
    (p,) = FuenteCommonCrawl(max_urls=3, crawls=2).recolectar(negocio(sitios=["tacospaco.mx"]), [])
    assert p.citable
    assert "CC-MAIN-2026-40 (October 2026 Index): capturada el 2026-10-03 (HTTP 200)" in p.contenido
    assert "CC-MAIN-2026-33 (August 2026 Index): sin capturas" in p.contenido


# ----- dominio -----


@pytest.mark.unit
class TestDominio:
    def test_dominios_propios_sin_plataformas_ni_correo_gratuito(self):
        ds = dominio.dominios_del_negocio(
            ["www.tacospaco.com.mx", "tacospaco.wixsite.com/menu", "facebook.com/tacospaco"],
            ["ventas@tacospaco.com.mx", "paco@gmail.com", "contacto@netflix.com"],
        )
        assert ds == [("tacospaco.com.mx", "sitio"), ("netflix.com", "correo")]

    def test_registrable(self):
        assert dominio.registrable("tienda.tacospaco.com.mx") == "tacospaco.com.mx"
        assert dominio.registrable("www.tacospaco.mx") == "tacospaco.mx"
        assert dominio.registrable("blog.tacospaco.com") == "tacospaco.com"

    def test_rdap_vencido_lo_dice_con_fechas(self, monkeypatch):
        datos = {
            "events": [
                {"eventAction": "registration", "eventDate": "2012-03-01T00:00:00Z"},
                {"eventAction": "expiration", "eventDate": "2025-03-01T00:00:00Z"},
            ],
            "status": ["client hold"],
        }
        monkeypatch.setattr(dominio.requests, "get", lambda *a, **k: _Resp(datos))
        r = dominio.consultar_registro("tacospaco.com", hoy=date(2026, 11, 20))
        assert r.registrado and r.vence == date(2025, 3, 1)
        assert "registrado: 2012-03-01" in r.lineas
        assert r.lineas[-1] == "el registro vence el 2025-03-01; hoy es 2026-11-20: VENCIDO"

    def test_rdap_404_es_no_registrado(self, monkeypatch):
        monkeypatch.setattr(dominio.requests, "get", lambda *a, **k: _Resp(status=404))
        assert dominio.consultar_registro("tacospaco.com").registrado is False

    def test_sin_dominio_propio_no_consulta(self):
        (p,) = FuenteDominio().recolectar(negocio(sitios=[], correos=["x@hotmail.com"]), [])
        assert not p.citable


# ----- SAT -----

CSV_SAT = (
    "Listado de contribuyentes no localizados\n"
    "RFC,RAZÓN SOCIAL,TIPO PERSONA,SUPUESTO,FECHAS DE PRIMERA PUBLICACION\n"
    'TPA120301AB1,"TACOS PACO, S.A. DE C.V.",Moral,NO LOCALIZADOS,15/03/2022\n'
    "XAXX010101000,OTRA EMPRESA SA,Moral,NO LOCALIZADOS,01/01/2020\n"
).encode("latin-1")


@pytest.mark.unit
class TestSat:
    def test_indexa_con_titulo_antes_de_la_cabecera_y_en_latin1(self):
        indice, n = sat.indexar(CSV_SAT)
        assert n == 2
        (c,) = indice[sat.llave("Tacos Paco SA de CV")]
        assert "FECHAS DE PRIMERA PUBLICACION: 15/03/2022" in c.fila

    def test_la_pieza_cita_la_fila_y_advierte_del_homonimo(self, tmp_path):
        archivo_csv = tmp_path / "no_localizados.csv"
        archivo_csv.write_bytes(CSV_SAT)
        lista = sat.ListaSat("http://sat", tmp_path, dias_validez=30)
        (p,) = FuenteSat(lista, "http://sat").recolectar(
            negocio(razones_sociales=["TACOS PACO SA DE CV"]), []
        )
        assert p.citable and "15/03/2022" in p.contenido and "homónimo" in p.contenido

    def test_sin_coincidencia_no_es_citable(self, tmp_path):
        (tmp_path / "no_localizados.csv").write_bytes(CSV_SAT)
        lista = sat.ListaSat("http://sat", tmp_path, dias_validez=30)
        (p,) = FuenteSat(lista, "http://sat").recolectar(
            negocio(razones_sociales=["TACOS PACO DOS SA"]), []
        )
        assert p.estado == "sin_resultados"

    def test_sin_razon_social_no_descarga(self):
        lista = sat.ListaSat("http://no-debe-bajarse", Path("/nonexistent"))
        (p,) = FuenteSat(lista, "http://sat").recolectar(negocio(razones_sociales=[]), [])
        assert not p.citable and lista.descargada is None


@pytest.mark.unit
def test_rdap_rechazado_va_directo_al_servidor_del_tld(monkeypatch):
    pedidas: list[str] = []
    datos = {"events": [{"eventAction": "expiration", "eventDate": "2030-01-01T00:00:00Z"}]}

    def get(url, timeout=None, headers=None):
        pedidas.append(url)
        assert "User-Agent" in headers
        if "rdap.org" in url:
            return _Resp(status=403)
        if url.endswith("dns.json"):
            return _Resp({"services": [[["com", "net"], ["https://rdap.verisign.com/com/v1/"]]]})
        return _Resp(datos)

    monkeypatch.setattr(dominio, "_servidores_rdap", None)
    monkeypatch.setattr(dominio.requests, "get", get)
    r = dominio.consultar_registro("tacospaco.com", hoy=date(2026, 11, 20))
    assert r.vence == date(2030, 1, 1)
    assert pedidas[-1] == "https://rdap.verisign.com/com/v1/domain/tacospaco.com"
