"""El prompt del verificador y las herramientas que puede llamar.

Es la §5 de `verificacion_cierre.md` escrita para el modelo: el protocolo de
búsqueda, las reglas de decisión y lo que NO puede hacer. Cambiarlo cambia
los veredictos, así que `AGENTE_VERSION` sube con él.

Las herramientas van en el formato de funciones de Ollama/OpenAI. `concluir`
es una herramienta y no "responde en JSON" porque así el formato lo valida
el servidor y el modelo no puede terminar sin pasar por la validación de
`verificador.py`.
"""

from __future__ import annotations

from src.agents.survival.insumo import Negocio
from src.shared.enums import Confianza, EstadoVerificado, SenalEvidencia

SISTEMA = """Eres un verificador de negocios de México. Te doy TODO lo que el Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) publicó de UN negocio, y tu trabajo es averiguar, buscando en internet, si ese negocio SIGUE ACTIVO o CERRÓ. El DENUE no reporta cierres: solo deja de listar negocios, y no sabemos por qué. Tu veredicto es independiente del DENUE.

TIENES TRES HERRAMIENTAS
- buscar_web(consulta): resultados de un buscador (título, URL, extracto).
- leer_pagina(url): el texto de una página.
- concluir(...): tu veredicto. ES OBLIGATORIO terminar llamándola. No respondas en texto libre.

PROTOCOLO, EN ESTE ORDEN (detente cuando tengas evidencia para confianza alta):
1. Google Maps / Google Business: busca "<nombre> <municipio>" y "<nombre> <domicilio>". Es la fuente con estado explícito ("Cerrado permanentemente", "Cerrado temporalmente", horarios vigentes) y con fecha de la última reseña.
2. Redes sociales (Facebook, Instagram): página del negocio, fecha de la última publicación, avisos de cierre o de cambio de domicilio.
3. Sitio web del negocio, si el DENUE trae uno: si responde y qué dice; si no responde, es señal.
4. Directorios y marketplaces (Sección Amarilla, Cylex, Mercado Libre, Uber Eats, Rappi, DiDi Food).
5. Noticias locales: cierres, clausuras, incendios, mudanzas.
Usa varios nombres si el negocio tuvo varios. Busca en español. Un negocio HOMÓNIMO en otro domicilio o municipio NO es evidencia: la identidad es nombre + lugar. Para cadenas y franquicias (OXXO, farmacias, bancos) exige el domicilio exacto; si no lo encuentras, indeterminado.

REGLAS DE DECISIÓN
- cerrado / alta: estado explícito de cierre en una fuente con fecha (Maps "cerrado permanentemente", aviso propio del negocio), o el local con otro ocupante verificable en ese mismo domicilio.
- cerrado / media: ninguna señal de actividad posterior a la fecha en que el DENUE lo vio por última vez, en al menos DOS fuentes que SÍ tenían actividad antes de esa fecha.
- cerrado / baja: solo silencio: no aparece en ningún lado y tampoco aparecía antes.
- activo / alta: actividad FECHADA posterior al último corte de la ventana del DENUE (reseña, publicación, horario vigente, pedido en marketplace).
- activo / media: actividad fechada posterior a la última vez que el DENUE lo vio, pero anterior al último corte de la ventana.
- activo / baja: ficha vigente sin fecha.
- indeterminado: sin huella, huella ambigua, homónimos imposibles de separar, o cadena sin domicilio exacto. Es un resultado LEGÍTIMO; no lo fuerces a activo ni a cerrado.

AL CONCLUIR
- razonamiento: por qué llegaste a ese estado, en prosa breve, nombrando las fuentes y las fechas.
- evidencias: UNA por pieza en que te apoyaste. Cada una con la URL EXACTA que viste en una búsqueda o leíste (no inventes ni reconstruyas URLs), el extracto CITADO (no parafraseado), la fecha de publicación si la pieza la trae, y hacia dónde apunta esa pieza por sí sola: cerrado, activo o neutral. Guarda también las contradictorias.
- fecha_cierre_estimada: solo si la evidencia la da (último rastro de actividad, aviso).
- Para activo o cerrado hace falta al menos una evidencia; para indeterminado puede no haber ninguna.

Trabaja con calma pero sin dar vueltas: cuando una herramienta te diga que se agotó el presupuesto, concluye con lo que tengas."""
"""Prompt de sistema. Sube `AGENTE_VERSION` si cambia."""


def formatear_insumo(n: Negocio) -> str:
    """El negocio para el modelo, en texto: todo lo del DENUE, nada de la hipótesis."""

    def lista(etiqueta: str, valores: list[str]) -> str:
        if not valores:
            return f"{etiqueta}: (el DENUE no publicó ninguno)"
        return f"{etiqueta}: " + " | ".join(valores)

    coordenadas = (
        f"{n.lat:.5f}, {n.lon:.5f}"
        if n.lat is not None and n.lon is not None
        else "sin coordenadas"
    )
    actividad = (
        f"{n.actividad} (SCIAN {n.codigo_act})" if n.actividad else (n.codigo_act or "desconocida")
    )
    periodos = ", ".join(p.strftime("%Y-%m") for p in n.periodos)
    return "\n".join(
        [
            "NEGOCIO A VERIFICAR",
            f"Nombre principal: {n.nombre or '(sin nombre)'}",
            lista("Nombres con que apareció", n.nombres),
            lista("Razón social", n.razones_sociales),
            f"Actividad: {actividad}",
            f"Tamaño (último corte): {n.tamano_final or 'desconocido'}",
            f"Municipio: {n.municipio or '?'}, {n.entidad or '?'}",
            lista("Domicilios publicados", n.domicilios),
            f"Coordenadas: {coordenadas}",
            lista("Teléfonos", n.telefonos),
            lista("Sitios web", n.sitios),
            lista("Correos", n.correos),
            "",
            "PRESENCIA EN EL DENUE",
            f"Apareció en los cortes: {periodos}",
            f"Primera vez: {n.primer_periodo:%Y-%m}. Última vez: {n.ultimo_periodo:%Y-%m}.",
            f"Último corte publicado del DENUE: {n.ultimo_corte_ventana:%Y-%m}.",
            "",
            "Averigua el estado actual del negocio y termina con `concluir`.",
        ]
    )


def _enum(valores: type) -> list[str]:
    return [v.value for v in valores]


HERRAMIENTAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "buscar_web",
            "description": "Busca en la web. Devuelve título, URL y extracto de los primeros resultados.",
            "parameters": {
                "type": "object",
                "properties": {
                    "consulta": {
                        "type": "string",
                        "description": "Lo que buscarías en Google, en español.",
                    }
                },
                "required": ["consulta"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "leer_pagina",
            "description": "Devuelve el texto visible de una página web (recortado).",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "URL exacta, tomada de una búsqueda."}
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "concluir",
            "description": "Registra el veredicto final. Obligatoria para terminar.",
            "parameters": {
                "type": "object",
                "properties": {
                    "estado": {"type": "string", "enum": _enum(EstadoVerificado)},
                    "confianza": {"type": "string", "enum": _enum(Confianza)},
                    "razonamiento": {
                        "type": "string",
                        "description": "Por qué, en prosa breve, con fuentes y fechas.",
                    },
                    "fecha_cierre_estimada": {
                        "type": ["string", "null"],
                        "description": "AAAA-MM-DD o AAAA-MM si la evidencia la da; null si no.",
                    },
                    "evidencias": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "url": {
                                    "type": "string",
                                    "description": "URL exacta vista en una búsqueda o leída.",
                                },
                                "titulo": {"type": ["string", "null"]},
                                "extracto": {
                                    "type": "string",
                                    "description": "Cita textual de la pieza.",
                                },
                                "fecha_publicacion": {
                                    "type": ["string", "null"],
                                    "description": "AAAA-MM-DD o AAAA-MM si la pieza la trae.",
                                },
                                "senal": {"type": "string", "enum": _enum(SenalEvidencia)},
                            },
                            "required": ["url", "extracto", "senal"],
                        },
                    },
                },
                "required": ["estado", "confianza", "razonamiento", "evidencias"],
            },
        },
    },
]
"""Las tres herramientas, en el formato de funciones de Ollama."""
