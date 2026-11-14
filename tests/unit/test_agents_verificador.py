"""El bucle del verificador contra un modelo y un buscador FALSOS.

Sin red ni Ollama: lo que se prueba son las garantías del bucle, que el
modelo por sí solo no da --que no cite URLs que no vio, que no se salga del
vocabulario, que un `cerrado` lleve evidencia, que el presupuesto se
respete, y que si nunca concluye quede `indeterminado` diciendo por qué.
"""

from datetime import date

import pytest

from src.agents.survival.insumo import Negocio
from src.agents.survival.prompts import HERRAMIENTAS, SISTEMA, formatear_insumo
from src.agents.survival.sources.busqueda import Resultado as ResultadoBusqueda
from src.agents.survival.verificador import Verificador, _fecha, _normalizar

VENTANA_FIN = date(2026, 5, 1)


def negocio(**extra) -> Negocio:
    campos = dict(
        muestra_id="m1",
        clee="14TAC0001",
        orden=1,
        estrato="cerrado|reocupado",
        nombre="TACOS PACO",
        codigo_act="722513",
        actividad="Restaurantes con servicio de preparación de tacos y tortas",
        tamano_final="Micro",
        cve_geo_final="14039",
        entidad="Jalisco",
        municipio="Guadalajara",
        lat=20.67,
        lon=-103.35,
        primer_periodo=date(2021, 5, 1),
        ultimo_periodo=date(2024, 11, 1),
        periodos=[date(2021, 5, 1), date(2022, 5, 1), date(2024, 11, 1)],
        ultimo_corte_ventana=VENTANA_FIN,
        nombres=["TACOS PACO", "TAQUERIA DON PACO"],
        telefonos=["3312345678"],
        domicilios=["AV VALLARTA 1234 AMERICANA 44160"],
    )
    campos.update(extra)
    return Negocio(**campos)


def llamada(nombre: str, **args) -> dict:
    return {"function": {"name": nombre, "arguments": args}}


def respuesta(*llamadas: dict, texto: str = "") -> dict:
    return {"role": "assistant", "content": texto, "tool_calls": list(llamadas), "_tokens": {}}


class ClienteFalso:
    """Devuelve respuestas en orden y recuerda la conversación que recibió."""

    modelo = "falso:1b"

    def __init__(self, *respuestas: dict):
        self.respuestas = list(respuestas)
        self.conversaciones: list[list[dict]] = []

    def chat(self, mensajes, herramientas=None):
        self.conversaciones.append([dict(m) for m in mensajes])
        if not self.respuestas:
            # Sin libreto: el modelo se queda callado en prosa.
            return respuesta(texto="...")
        return self.respuestas.pop(0)

    def mensajes_tool(self) -> list[str]:
        return [m["content"] for m in self.conversaciones[-1] if m.get("role") == "tool"]


class BuscadorFalso:
    nombre = "falso"

    def __init__(self, resultados=None):
        self.resultados = resultados or [
            ResultadoBusqueda(
                "Tacos Paco - Google Maps",
                "https://www.google.com/maps/place/tacos-paco",
                "Cerrado permanentemente",
            ),
            ResultadoBusqueda(
                "Tacos Paco | Facebook",
                "https://www.facebook.com/tacospacogdl",
                "Última publicación 2024",
            ),
        ]
        self.consultas: list[str] = []

    def buscar(self, consulta, n=8):
        self.consultas.append(consulta)
        return self.resultados


def leer_falso(url: str) -> str:
    if "404" in url:
        return f"ERROR: {url} respondio HTTP 404"
    return f"TITULO: pagina de {url}\nCerrado permanentemente desde marzo de 2024."


def verificador(cliente, buscador=None, **kw) -> Verificador:
    params = dict(version="v1", max_busquedas=8, max_paginas=6, max_vueltas=6)
    params.update(kw)
    return Verificador(cliente, buscador or BuscadorFalso(), leer_falso, **params)


CONCLUSION_CERRADO = dict(
    estado="cerrado",
    confianza="alta",
    razonamiento="Google Maps marca el local como cerrado permanentemente y la página lo confirma.",
    fecha_cierre_estimada="2024-03",
    evidencias=[
        {
            "url": "https://www.google.com/maps/place/tacos-paco",
            "titulo": "Tacos Paco - Google Maps",
            "extracto": "Cerrado permanentemente",
            "fecha_publicacion": None,
            "senal": "cerrado",
        }
    ],
)


@pytest.mark.unit
class TestElCaminoFeliz:
    def test_busca_lee_y_concluye(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="TACOS PACO Guadalajara")),
            respuesta(llamada("leer_pagina", url="https://www.google.com/maps/place/tacos-paco")),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        r = verificador(cliente).verificar(negocio())
        assert (r.estado, r.confianza) == ("cerrado", "alta")
        assert r.fecha_cierre_estimada == date(2024, 3, 1)
        assert r.n_busquedas == 1 and r.n_paginas == 1 and r.vueltas == 3
        assert r.agente == "falso:1b/v1"
        assert len(r.evidencias) == 1
        assert r.evidencias[0].fuente == "google_maps"  # la clasifica el codigo, no el modelo

    def test_indeterminado_sin_evidencias_es_legitimo(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="TACOS PACO")),
            respuesta(
                llamada(
                    "concluir",
                    estado="indeterminado",
                    confianza="baja",
                    razonamiento="No hay huella del negocio en ninguna fuente consultada.",
                    evidencias=[],
                )
            ),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.estado == "indeterminado" and r.evidencias == []

    def test_la_url_leida_tambien_cuenta_como_vista(self):
        """Una página leída es evidencia válida aunque no saliera en una búsqueda."""
        cliente = ClienteFalso(
            respuesta(llamada("leer_pagina", url="https://tacospaco.mx/")),
            respuesta(
                llamada(
                    "concluir",
                    estado="activo",
                    confianza="baja",
                    razonamiento="El sitio del negocio responde y muestra el menú vigente.",
                    evidencias=[
                        {"url": "https://tacospaco.mx", "extracto": "menú", "senal": "activo"}
                    ],
                )
            ),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.estado == "activo" and r.evidencias[0].fuente == "sitio_web"


@pytest.mark.unit
class TestLoQueSeRechaza:
    """El modelo recibe el error y vuelve a intentar; el código no corrige por él."""

    def test_una_url_inventada(self):
        inventada = dict(
            CONCLUSION_CERRADO,
            evidencias=[{"url": "https://inventada.mx/x", "extracto": "cerró", "senal": "cerrado"}],
        )
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="TACOS PACO")),
            respuesta(llamada("concluir", **inventada)),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.estado == "cerrado" and r.vueltas == 3
        rechazos = [m for m in cliente.mensajes_tool() if m.startswith("RECHAZADO")]
        assert len(rechazos) == 1 and "no apareció en ninguna búsqueda" in rechazos[0]

    def test_mayusculas_y_espacios_se_normalizan_no_se_rechazan(self):
        """Rechazar "CERRADO " costaría otra vuelta del modelo por nada; el CHECK recibe el valor limpio."""
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(
                llamada(
                    "concluir", **dict(CONCLUSION_CERRADO, estado="CERRADO ", confianza=" Alta")
                )
            ),
        )
        r = verificador(cliente).verificar(negocio())
        assert (r.estado, r.confianza) == ("cerrado", "alta")

    @pytest.mark.parametrize("estado", ["closed", "quebrado", ""])
    def test_un_estado_fuera_del_vocabulario(self, estado):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(llamada("concluir", **dict(CONCLUSION_CERRADO, estado=estado))),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.estado == "cerrado"
        assert any("estado" in m and "RECHAZADO" in m for m in cliente.mensajes_tool())

    def test_un_cerrado_sin_evidencias(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(llamada("concluir", **dict(CONCLUSION_CERRADO, evidencias=[]))),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.estado == "cerrado" and len(r.evidencias) == 1
        assert any("al menos una evidencia" in m for m in cliente.mensajes_tool())

    def test_una_fecha_que_no_parsea(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(
                llamada("concluir", **dict(CONCLUSION_CERRADO, fecha_cierre_estimada="marzo 2024"))
            ),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        r = verificador(cliente).verificar(negocio())
        assert r.fecha_cierre_estimada == date(2024, 3, 1)
        assert any("fecha_cierre_estimada" in m for m in cliente.mensajes_tool())

    def test_prosa_sin_herramienta_recibe_el_recordatorio(self):
        cliente = ClienteFalso(
            respuesta(texto="Creo que cerró."),
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        verificador(cliente).verificar(negocio())
        usuario = [m["content"] for m in cliente.conversaciones[-1] if m["role"] == "user"]
        assert any("`concluir`" in m for m in usuario[1:])


@pytest.mark.unit
class TestElPresupuesto:
    def test_la_segunda_busqueda_se_niega_con_tope_en_una(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="uno")),
            respuesta(llamada("buscar_web", consulta="dos")),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),
        )
        buscador = BuscadorFalso()
        r = verificador(cliente, buscador, max_busquedas=1).verificar(negocio())
        assert buscador.consultas == ["uno"]
        assert r.n_busquedas == 1
        assert any(m.startswith("PRESUPUESTO AGOTADO") for m in cliente.mensajes_tool())

    def test_si_nunca_concluye_queda_indeterminado_y_lo_dice(self):
        cliente = ClienteFalso()  # sin libreto: siempre prosa
        r = verificador(cliente, max_vueltas=3).verificar(negocio())
        assert (r.estado, r.confianza) == ("indeterminado", "baja")
        assert "no concluyó" in r.razonamiento
        assert r.vueltas == 4  # las 3 mas la ultima oportunidad
        assert len(cliente.conversaciones) == 4

    def test_la_ultima_oportunidad_cuenta_si_concluye(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="uno")),
            respuesta(llamada("buscar_web", consulta="dos")),
            respuesta(llamada("concluir", **CONCLUSION_CERRADO)),  # llega en la vuelta extra
        )
        r = verificador(cliente, max_vueltas=2).verificar(negocio())
        assert r.estado == "cerrado" and r.vueltas == 3

    def test_el_buscador_caido_no_tumba_el_veredicto(self):
        class BuscadorRoto(BuscadorFalso):
            def buscar(self, consulta, n=8):
                raise ConnectionError("searxng abajo")

        cliente = ClienteFalso(
            respuesta(llamada("buscar_web", consulta="x")),
            respuesta(
                llamada(
                    "concluir",
                    estado="indeterminado",
                    confianza="baja",
                    razonamiento="El buscador no respondió, así que no hay con qué decidir.",
                    evidencias=[],
                )
            ),
        )
        r = verificador(cliente, BuscadorRoto()).verificar(negocio())
        assert r.estado == "indeterminado"
        assert any(m.startswith("ERROR del buscador") for m in cliente.mensajes_tool())


@pytest.mark.unit
class TestElInsumoEsCiego:
    def test_no_menciona_la_hipotesis(self):
        """Ni `cerrado`, ni meses de ausencia, ni huecos: el veredicto no puede saber qué se mide."""
        texto = formatear_insumo(negocio()).lower()
        for prohibido in ("cerrado", "cerró", "hueco", "meses sin", "hipótesis", "estrato"):
            assert prohibido not in texto, prohibido

    def test_si_lleva_lo_que_el_denue_publico(self):
        texto = formatear_insumo(negocio())
        for esperado in (
            "TACOS PACO",
            "TAQUERIA DON PACO",
            "Guadalajara",
            "3312345678",
            "2024-11",
            "2026-05",
        ):
            assert esperado in texto

    def test_el_negocio_no_tiene_atributo_de_la_hipotesis(self):
        for campo in ("cerrado", "cerrado_gold", "meses_sin_aparecer", "huecos"):
            assert not hasattr(negocio(), campo)

    def test_el_prompt_exige_concluir_y_admite_indeterminado(self):
        assert "concluir" in SISTEMA and "indeterminado" in SISTEMA
        nombres = {h["function"]["name"] for h in HERRAMIENTAS}
        assert nombres == {"buscar_web", "leer_pagina", "concluir"}


@pytest.mark.unit
class TestUtilidades:
    def test_normalizar_compara_sin_esquema_ni_www_ni_barra(self):
        assert _normalizar("https://www.Tacos.mx/") == _normalizar("http://tacos.mx")

    @pytest.mark.parametrize(
        "valor,esperado",
        [
            ("2024-03-15", date(2024, 3, 15)),
            ("2024-03", date(2024, 3, 1)),
            (None, None),
            ("", None),
            ("null", None),
        ],
    )
    def test_fecha_valida(self, valor, esperado):
        assert _fecha(valor) == (esperado, None)

    def test_fecha_invalida_devuelve_el_error(self):
        fecha, error = _fecha("marzo 2024")
        assert fecha is None and "AAAA-MM" in error
