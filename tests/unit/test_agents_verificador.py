"""El equipo (especialistas + decisor), las citas y el juez, contra un modelo FALSO.

Sin red, sin Ollama, sin base: lo que se prueba son las garantías que el
modelo por sí solo no da --que una cita apunte a una pieza que existe y
diga lo que se le atribuye, que una fecha esté escrita, que un directorio
que copia al DENUE no pruebe nada, que un especialista no cite fuera de su
fuente, que `activo/alta` exija una fecha posterior al último corte, que el
juez estime a ciegas antes de ver el veredicto--, y que cuando el modelo no
produce algo válido la salida sea automática y lo diga.
"""

from datetime import date

import pytest

from src.agents.survival.citas import (
    Cita,
    extracto_en,
    fecha_sustentada,
    parsear_fecha,
    validar_citas,
)
from src.agents.survival.equipo import Decisor, Especialista, Informe
from src.agents.survival.insumo import Negocio
from src.agents.survival.juez import Juez, JuezSinDictamen, VeredictoAuditado
from src.agents.survival.llamada import pedir
from src.agents.survival.prompts import (
    SISTEMA_DECISOR,
    SISTEMA_JUEZ,
    formatear_insumo,
    sistema_especialista,
)
from src.agents.survival.recoleccion import Pieza
from src.agents.survival.verificador import Equipo

VENTANA_FIN = date(2026, 5, 1)
MAPS = "https://www.google.com/maps/search/TACOS%20PACO?hl=es"
FACEBOOK = "https://www.facebook.com/tacospacogdl"


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
    """Devuelve respuestas en orden y recuerda cada conversación que recibió."""

    modelo = "falso:1b"

    def __init__(self, *respuestas: dict):
        self.respuestas = list(respuestas)
        self.conversaciones: list[list[dict]] = []

    def chat(self, mensajes, herramientas=None):
        self.conversaciones.append([dict(m) for m in mensajes])
        if not self.respuestas:
            return respuesta(texto="...")  # sin libreto: se queda callado en prosa
        return self.respuestas.pop(0)

    def tools(self) -> list[str]:
        return [m["content"] for m in self.conversaciones[-1] if m.get("role") == "tool"]


def expediente() -> list[Pieza]:
    """Un expediente chico, con números 1..6."""
    return [
        Pieza(
            "maps",
            "TITULO: Tacos Paco\nAv Vallarta 1234 · Cerrado permanentemente · "
            "reseña hace 2 meses [≈ 2026-09]",
            "ok",
            consulta="TACOS PACO Guadalajara",
            url=MAPS,
        ),
        Pieza("maps", "ERROR: no entregó contenido legible", "error", consulta="otra", url=MAPS),
        Pieza(
            "web",
            "Tacos Paco, Av Vallarta 1234. Promo 2x1 todos los martes.",
            "ok",
            consulta="TACOS PACO",
            url=FACEBOOK,
            titulo="Tacos Paco | Facebook",
        ),
        Pieza(
            "web",
            "TACOS PACO · Guadalajara · Restaurantes · Directorio de empresas",
            "ok",
            consulta="TACOS PACO",
            url="https://www.mexicopymes.com/info/tacos-paco",
        ),
        Pieza(
            "web",
            "Publicado el 5 de noviembre de 2026: abrimos nueva sucursal",
            "ok",
            consulta="TACOS PACO",
            url="https://nota.mx/1",
            fecha=date(2026, 11, 5),
        ),
        Pieza(
            "redes",
            "Tacos Paco. Última publicación: 3 de junio de 2026. Horario 9-18",
            "ok",
            url=FACEBOOK,
        ),
    ]


def cita(pieza: int, extracto: str, senal: str, fecha=None) -> dict:
    return {"pieza": pieza, "extracto": extracto, "senal": senal, "fecha_publicacion": fecha}


# ----- citas -----


@pytest.mark.unit
class TestLasCitas:
    def test_una_cita_textual_pasa(self):
        citas, errores = validar_citas(
            [cita(1, "Cerrado permanentemente", "cerrado")], expediente()
        )
        assert errores == [] and citas == [Cita(1, "Cerrado permanentemente", "cerrado")]

    def test_un_extracto_parafraseado_se_rechaza(self):
        _, errores = validar_citas(
            [cita(1, "el local cerró para siempre", "cerrado")], expediente()
        )
        assert any("no aparece textual" in e for e in errores)

    def test_se_puede_recortar_con_puntos_suspensivos(self):
        _, errores = validar_citas(
            [cita(3, "Tacos Paco, Av Vallarta 1234... todos los martes", "activo")], expediente()
        )
        assert errores == []

    def test_mayusculas_espacios_y_comillas_no_importan(self):
        assert extracto_en('"cerrado   PERMANENTEMENTE"', "Cerrado permanentemente")

    def test_un_extracto_demasiado_corto_no_vale(self):
        assert not extracto_en("abierto", "abierto de 9 a 18")

    def test_una_pieza_que_no_existe(self):
        _, errores = validar_citas([cita(99, "x" * 10, "activo")], expediente())
        assert any("no hay pieza [99]" in e for e in errores)

    def test_una_pieza_con_error_no_se_cita(self):
        _, errores = validar_citas(
            [cita(2, "no entregó contenido legible", "neutral")], expediente()
        )
        assert any("no se puede citar" in e for e in errores)

    def test_fuera_de_las_permitidas(self):
        _, errores = validar_citas(
            [cita(1, "Cerrado permanentemente", "cerrado")], expediente(), {3}
        )
        assert any("no es de las que puedes citar" in e for e in errores)

    def test_un_directorio_que_copia_al_denue_solo_es_neutral(self):
        crudas = [cita(4, "TACOS PACO · Guadalajara · Restaurantes", "activo")]
        _, errores = validar_citas(crudas, expediente())
        assert any("copia al DENUE" in e for e in errores)
        crudas[0]["senal"] = "neutral"
        assert validar_citas(crudas, expediente())[1] == []

    def test_una_fecha_que_no_esta_en_la_pieza_se_rechaza(self):
        _, errores = validar_citas(
            [cita(3, "Promo 2x1 todos los martes", "activo", "2026-11-05")], expediente()
        )
        assert any("no está escrita en la pieza [3]" in e for e in errores)

    def test_la_fecha_que_dio_el_buscador_vale(self):
        _, errores = validar_citas(
            [cita(5, "abrimos nueva sucursal", "activo", "2026-11-05")], expediente()
        )
        assert errores == []

    def test_la_fecha_relativa_anotada_por_el_navegador_vale(self):
        _, errores = validar_citas(
            [cita(1, "Cerrado permanentemente", "cerrado", "2026-09")], expediente()
        )
        assert errores == []

    def test_una_senal_fuera_del_vocabulario(self):
        _, errores = validar_citas([cita(1, "Cerrado permanentemente", "closed")], expediente())
        assert any("senal 'closed'" in e for e in errores)

    @pytest.mark.parametrize(
        "texto",
        [
            "5 de noviembre de 2026",
            "5 nov 2026",
            "nov. 2026",
            "Nov 5, 2026",
            "November 5th, 2026",
            "Noviembre del 2026",
            "2026-11-05",
            "05/11/2026",
            "11/05/2026",
            "11/2026",
            "hace 3 días [≈ 2026-11]",
        ],
    )
    def test_formas_que_sustentan(self, texto):
        assert fecha_sustentada(date(2026, 11, 5), f"Publicado: {texto}. Promo.")

    @pytest.mark.parametrize(
        "texto",
        [
            "",
            "hace 3 días",
            "noviembre",
            "2026",
            "5 de octubre de 2026",
            "5 de noviembre de 2025",
            "novedades 2026",
            "tel 3312026115",
            "12026-11",
        ],
    )
    def test_formas_que_no_sustentan(self, texto):
        assert not fecha_sustentada(date(2026, 11, 5), texto)

    @pytest.mark.parametrize(
        "valor,esperado",
        [
            ("2024-03-15", date(2024, 3, 15)),
            ("2024-03", date(2024, 3, 1)),
            (None, None),
            ("", None),
        ],
    )
    def test_parsear_fecha(self, valor, esperado):
        assert parsear_fecha(valor) == (esperado, None)

    def test_parsear_fecha_invalida(self):
        fecha, error = parsear_fecha("marzo 2024")
        assert fecha is None and "no es AAAA-MM-DD" in error


# ----- el bucle compartido -----

FIN = [{"function": {"name": "fin"}}]


@pytest.mark.unit
class TestPedir:
    def test_prosa_recibe_el_recordatorio_y_luego_vale(self):
        cliente = ClienteFalso(
            respuesta(texto="creo que está cerrado"), respuesta(llamada("fin", x=1))
        )
        mensajes = [{"role": "user", "content": "hola"}]
        salida = pedir(cliente, mensajes, FIN, {"fin"}, lambda n, a: (a["x"], []), max_vueltas=3)
        assert salida.valor == 1 and salida.vueltas == 2
        assert any("Responde únicamente llamando a: fin" in m["content"] for m in mensajes)

    def test_un_rechazo_vuelve_con_el_error(self):
        cliente = ClienteFalso(respuesta(llamada("fin", x=0)), respuesta(llamada("fin", x=2)))
        mensajes: list[dict] = []

        def validar(_n, a):
            return a["x"], ([] if a["x"] else ["x no puede ser 0"])

        salida = pedir(cliente, mensajes, FIN, {"fin"}, validar, max_vueltas=3)
        assert salida.valor == 2
        assert any("RECHAZADO" in m["content"] for m in mensajes if m.get("role") == "tool")

    def test_se_agota(self):
        assert pedir(ClienteFalso(), [], FIN, {"fin"}, lambda n, a: (0, []), max_vueltas=2) is None


# ----- especialista -----


class FuenteFalsa:
    """Una fuente cuya ampliación agrega una pieza fija."""

    def __init__(self, recolector="maps", herramienta=True):
        self.recolector = recolector
        self.herramienta = (
            {"type": "function", "function": {"name": "buscar_en_maps"}} if herramienta else None
        )
        self.ampliaciones: list[dict] = []

    def recolectar(self, n, expediente):
        return []

    def ampliar(self, n, args, expediente):
        self.ampliaciones.append(args)
        return [
            Pieza(
                self.recolector,
                "Taquería Don Paco · Av Vallarta 1234 · Abierto · reseña de mayo de 2026",
                "ok",
                consulta=args.get("consulta"),
                url=MAPS + "&2",
                ampliada=True,
            )
        ]

    def contexto(self, expediente):
        return ""


def informar(**kw) -> dict:
    base = dict(
        identificado="si",
        senal="cerrado",
        resumen="La ficha de Maps en el domicilio del DENUE dice cerrado permanentemente.",
        citas=[cita(1, "Cerrado permanentemente", "cerrado")],
    )
    base.update(kw)
    return llamada("informar", **base)


def especialista(cliente, ampliaciones=0, intentos=3) -> Especialista:
    return Especialista(cliente, max_ampliaciones=ampliaciones, max_intentos=intentos)


@pytest.mark.unit
class TestElEspecialista:
    def test_informa_con_citas_de_su_fuente(self):
        cliente = ClienteFalso(respuesta(informar()))
        i = especialista(cliente, 2).informar(negocio(), FuenteFalsa(), expediente())
        assert (i.identificado, i.senal, i.automatico) == ("si", "cerrado", False)
        assert i.citas[0].pieza == 1
        # Solo vio las piezas de SU fuente, con su número global.
        visto = cliente.conversaciones[0][1]["content"]
        assert "[1] maps" in visto and "[3] web" not in visto

    def test_no_puede_citar_otra_fuente(self):
        malo = informar(senal="activo", citas=[cita(3, "Promo 2x1 todos los martes", "activo")])
        cliente = ClienteFalso(respuesta(malo), respuesta(informar()))
        i = especialista(cliente).informar(negocio(), FuenteFalsa(), expediente())
        assert i.senal == "cerrado"
        assert any("no es de las que puedes citar" in t for t in cliente.tools())

    def test_sin_identificar_la_senal_es_neutral(self):
        malo = informar(identificado="no")
        bueno = informar(identificado="no", senal="neutral", citas=[])
        cliente = ClienteFalso(respuesta(malo), respuesta(bueno))
        i = especialista(cliente).informar(negocio(), FuenteFalsa(), expediente())
        assert (i.identificado, i.senal) == ("no", "neutral")
        assert any("la señal es neutral" in t for t in cliente.tools())

    def test_una_senal_sin_cita_que_la_sostenga(self):
        cliente = ClienteFalso(respuesta(informar(citas=[])), respuesta(informar()))
        especialista(cliente).informar(negocio(), FuenteFalsa(), expediente())
        assert any("al menos una cita con esa señal" in t for t in cliente.tools())

    def test_amplia_su_fuente_y_puede_citar_lo_nuevo(self):
        exp = expediente()
        nueva = len(exp) + 1
        cliente = ClienteFalso(
            respuesta(llamada("buscar_en_maps", consulta="TAQUERIA DON PACO Guadalajara")),
            respuesta(
                informar(
                    senal="activo",
                    citas=[cita(nueva, "Abierto · reseña de mayo de 2026", "activo", "2026-05")],
                )
            ),
        )
        fuente = FuenteFalsa()
        i = especialista(cliente, 2).informar(negocio(), fuente, exp)
        assert fuente.ampliaciones == [{"consulta": "TAQUERIA DON PACO Guadalajara"}]
        assert len(exp) == nueva and exp[-1].ampliada
        assert i.senal == "activo" and i.fecha_mas_reciente == date(2026, 5, 1)

    def test_respeta_el_tope_de_ampliaciones(self):
        cliente = ClienteFalso(
            respuesta(llamada("buscar_en_maps", consulta="a")),
            respuesta(llamada("buscar_en_maps", consulta="b")),
            respuesta(informar()),
        )
        fuente = FuenteFalsa()
        especialista(cliente, 1).informar(negocio(), fuente, expediente())
        assert len(fuente.ampliaciones) == 1
        assert any("PRESUPUESTO AGOTADO" in t for t in cliente.tools())

    def test_sin_nada_citable_ni_herramienta_no_llama_al_modelo(self):
        cliente = ClienteFalso()
        i = especialista(cliente, 2).informar(
            negocio(), FuenteFalsa("sitio", herramienta=False), expediente()
        )
        assert i.automatico and (i.identificado, i.senal) == ("no", "neutral")
        assert cliente.conversaciones == []

    def test_si_nunca_informa_queda_automatico_y_lo_dice(self):
        i = especialista(ClienteFalso(), intentos=2).informar(
            negocio(), FuenteFalsa(), expediente()
        )
        assert i.automatico and i.identificado == "dudoso" and "no produjo" in i.resumen


# ----- decisor -----


def concluir(**kw) -> dict:
    base = dict(
        estado="cerrado",
        confianza="alta",
        razonamiento="Maps marca el local del domicilio del DENUE como cerrado permanentemente.",
        fecha_cierre_estimada=None,
        evidencias=[cita(1, "Cerrado permanentemente", "cerrado")],
    )
    base.update(kw)
    return llamada("concluir", **base)


INFORMES = [
    Informe(
        "maps",
        "si",
        "cerrado",
        "Maps dice cerrado.",
        [Cita(1, "Cerrado permanentemente", "cerrado")],
    ),
    Informe(
        "web",
        "si",
        "activo",
        "Hay una nota fechada.",
        [Cita(5, "abrimos nueva sucursal", "activo", date(2026, 11, 5))],
    ),
]


@pytest.mark.unit
class TestElDecisor:
    def test_concluye_con_los_informes_y_no_con_el_expediente_crudo(self):
        cliente = ClienteFalso(respuesta(concluir()))
        d = Decisor(cliente, max_intentos=3).decidir(negocio(), INFORMES, expediente())
        assert (d.estado, d.confianza, d.automatica) == ("cerrado", "alta", False)
        visto = cliente.conversaciones[0][1]["content"]
        assert "INFORME DE GOOGLE MAPS" in visto and "TITULO: Tacos Paco" not in visto

    def test_no_puede_citar_lo_que_ningun_especialista_cito(self):
        malo = concluir(
            estado="activo", confianza="baja", evidencias=[cita(6, "Horario 9-18 ok", "activo")]
        )
        cliente = ClienteFalso(respuesta(malo), respuesta(concluir()))
        Decisor(cliente, max_intentos=3).decidir(negocio(), INFORMES, expediente())
        assert any("no es de las que puedes citar" in t for t in cliente.tools())

    def test_activo_alta_exige_fecha_desde_el_ultimo_corte(self):
        sin_fecha = concluir(
            estado="activo", evidencias=[cita(5, "abrimos nueva sucursal", "activo")]
        )
        con_fecha = concluir(
            estado="activo",
            evidencias=[cita(5, "abrimos nueva sucursal", "activo", "2026-11-05")],
        )
        cliente = ClienteFalso(respuesta(sin_fecha), respuesta(con_fecha))
        d = Decisor(cliente, max_intentos=3).decidir(negocio(), INFORMES, expediente())
        assert (d.estado, d.confianza) == ("activo", "alta")
        assert any("activo/alta exige" in t for t in cliente.tools())

    def test_una_fecha_anterior_al_ultimo_corte_no_alcanza_para_alta(self):
        n = negocio(ultimo_corte_ventana=date(2026, 12, 1))
        cliente = ClienteFalso(
            respuesta(
                concluir(
                    estado="activo",
                    evidencias=[cita(5, "abrimos nueva sucursal", "activo", "2026-11-05")],
                )
            )
        )
        d = Decisor(cliente, max_intentos=1).decidir(n, INFORMES, expediente())
        assert d.automatica and d.estado == "indeterminado"

    def test_cerrado_sin_evidencia_de_cierre(self):
        malo = concluir(evidencias=[cita(5, "abrimos nueva sucursal", "activo")])
        cliente = ClienteFalso(respuesta(malo), respuesta(concluir()))
        Decisor(cliente, max_intentos=3).decidir(negocio(), INFORMES, expediente())
        assert any("al menos una evidencia con esa señal" in t for t in cliente.tools())

    def test_sin_nada_citado_es_indeterminado_sin_llamar_al_modelo(self):
        cliente = ClienteFalso()
        vacios = [
            Informe("maps", "no", "neutral", "nada", []),
            Informe("web", "dudoso", "neutral", "nada", []),
        ]
        d = Decisor(cliente, max_intentos=3).decidir(negocio(), vacios, expediente())
        assert (d.estado, d.confianza, d.automatica) == ("indeterminado", "baja", True)
        assert "maps: no; web: dudoso" in d.razonamiento
        assert cliente.conversaciones == []

    def test_si_nunca_concluye_queda_indeterminado(self):
        d = Decisor(ClienteFalso(), max_intentos=2).decidir(negocio(), INFORMES, expediente())
        assert d.automatica and d.estado == "indeterminado" and "2 intentos" in d.razonamiento


# ----- el equipo completo -----


class FuenteFija:
    """Recolecta piezas fijas; no amplía."""

    herramienta = None

    def __init__(self, recolector, piezas):
        self.recolector = recolector
        self.piezas = piezas

    def recolectar(self, n, expediente):
        return list(self.piezas)

    def ampliar(self, n, args, expediente):
        return []

    def contexto(self, expediente):
        return ""


@pytest.mark.unit
def test_el_equipo_recolecta_informa_por_fuente_y_decide():
    exp = expediente()
    fuentes = [
        FuenteFija("maps", exp[:2]),
        FuenteFija("web", exp[2:5]),
        FuenteFija("redes", exp[5:]),
        FuenteFija("sitio", []),  # nada que leer: informe automatico, sin modelo
    ]
    cliente = ClienteFalso(
        respuesta(informar()),
        respuesta(
            informar(
                senal="activo",
                resumen="Una nota fechada dice que abrieron otra sucursal.",
                citas=[cita(5, "abrimos nueva sucursal", "activo", "2026-11-05")],
            )
        ),
        respuesta(
            informar(
                senal="activo",
                resumen="La página tiene una publicación de junio de 2026.",
                citas=[cita(6, "Última publicación: 3 de junio de 2026", "activo", "2026-06-03")],
            )
        ),
        respuesta(concluir()),
    )
    r = Equipo(cliente, fuentes, version="v4", max_ampliaciones=0, max_intentos=2).verificar(
        negocio()
    )
    assert r.agente == "falso:1b/v4"
    assert [i.especialista for i in r.informes] == ["maps", "web", "redes", "sitio"]
    assert r.informes[-1].automatico
    assert (r.estado, r.confianza) == ("cerrado", "alta")
    assert len(r.expediente) == 6 and r.n_consultas >= 4


# ----- juez -----


VEREDICTO = VeredictoAuditado(
    veredicto_id="v1",
    estado="cerrado",
    confianza="alta",
    evidencias=[Cita(1, "Cerrado permanentemente", "cerrado")],
)


def estimar(**kw) -> dict:
    base = dict(
        estado="cerrado",
        confianza="alta",
        razonamiento="La ficha de Maps del domicilio dice cerrado permanentemente.",
    )
    base.update(kw)
    return llamada("estimar", **base)


def dictaminar(**kw) -> dict:
    base = dict(
        dictamen="sostenido",
        motivo="La pieza [1] es la ficha del domicilio y dice cerrado permanentemente.",
        piezas_objetadas=[],
    )
    base.update(kw)
    return llamada("dictaminar", **base)


def juez(cliente, intentos=3) -> Juez:
    return Juez(cliente, version="v4", max_intentos=intentos)


@pytest.mark.unit
class TestElJuez:
    def test_estima_a_ciegas_y_luego_dictamina(self):
        cliente = ClienteFalso(
            respuesta(estimar(estado="activo", confianza="media")), respuesta(dictaminar())
        )
        r = juez(cliente).auditar(negocio(), expediente(), VEREDICTO)
        assert (r.estado_ciego, r.confianza_ciega, r.dictamen) == ("activo", "media", "sostenido")
        assert r.juez == "falso:1b/v4"
        # El paso 1 se pidió SIN el veredicto; ve el expediente crudo, no los informes.
        paso_1 = cliente.conversaciones[0]
        assert not any("PASO 2" in m.get("content", "") for m in paso_1)
        assert "[4] web · directorio_denue" in paso_1[1]["content"]
        # El paso 2 trae el veredicto, sin el razonamiento del equipo.
        paso_2 = cliente.conversaciones[1][-1]["content"]
        assert "estado: cerrado · confianza: alta" in paso_2 and "[1] cerrado" in paso_2

    def test_solo_objeta_piezas_citadas(self):
        malo = dictaminar(dictamen="no_sostenido", piezas_objetadas=[3])
        bueno = dictaminar(dictamen="no_sostenido", piezas_objetadas=[1])
        cliente = ClienteFalso(respuesta(estimar()), respuesta(malo), respuesta(bueno))
        r = juez(cliente).auditar(negocio(), expediente(), VEREDICTO)
        assert r.piezas_objetadas == [1]
        assert any("no están entre las citadas" in t for t in cliente.tools())

    def test_un_dictamen_fuera_del_vocabulario(self):
        cliente = ClienteFalso(
            respuesta(estimar()),
            respuesta(dictaminar(dictamen="correcto")),
            respuesta(dictaminar()),
        )
        assert juez(cliente).auditar(negocio(), expediente(), VEREDICTO).dictamen == "sostenido"

    def test_si_no_estima_falla_con_nombre(self):
        with pytest.raises(JuezSinDictamen, match="no estimó"):
            juez(ClienteFalso(), intentos=2).auditar(negocio(), expediente(), VEREDICTO)


# ----- lo que ningún rol recibe -----


@pytest.mark.unit
class TestNadieVeLaHipotesis:
    def test_el_insumo_no_menciona_la_hipotesis(self):
        texto = formatear_insumo(negocio()).lower()
        for palabra in ("cerrado_gold", "meses_sin_aparecer", "huecos", "estrato"):
            assert palabra not in texto

    def test_el_insumo_si_lleva_lo_que_el_denue_publico(self):
        texto = formatear_insumo(negocio())
        for dato in (
            "TACOS PACO",
            "TAQUERIA DON PACO",
            "3312345678",
            "AV VALLARTA 1234",
            "2026-05",
        ):
            assert dato in texto

    def test_el_negocio_no_tiene_atributo_de_la_hipotesis(self):
        for campo in ("cerrado", "cerrado_gold", "meses_sin_aparecer", "huecos"):
            assert not hasattr(negocio(), campo)

    def test_los_prompts_explican_las_citas_y_admiten_indeterminado(self):
        assert "indeterminado" in SISTEMA_DECISOR and "indeterminado" in SISTEMA_JUEZ
        assert "COPIADO TEXTUALMENTE" in SISTEMA_DECISOR
        assert "directorio_denue" in sistema_especialista("web", 2)
        assert "buscar_en_maps" in sistema_especialista("maps", 2)
        assert "buscar_en_maps" not in sistema_especialista("maps", 0)
