"""Las fuentes de v5 que leen datos abiertos, contra respuestas guardadas y sin red.

Lo que se afirma es lo que decide el código --qué negocio mapeado se le
muestra al modelo y con qué datos para identificarlo, qué fecha queda escrita
para poder citarla, que un servicio caído sea una pieza ERROR y no una
caída--, no que los servicios respondan hoy igual (eso lo dice `sondear`).
"""

import pytest

from src.agents.survival.fuentes_abiertas import FuenteOsm
from src.agents.survival.recoleccion import FuenteMaps
from src.agents.survival.sources import osm
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
