"""Los prompts del equipo y las herramientas con que cada rol termina.

Tres roles, cada uno con un contexto chico y una sola tarea:

- **Especialista** (uno por fuente): lee lo que trajo SU fuente y responde
  si es este negocio y qué indica, con citas.
- **Decisor**: integra los informes de los especialistas y concluye.
- **Juez**: relee el expediente crudo, primero a ciegas y luego viendo el
  veredicto, y dictamina si las citas lo sostienen.

Cambiar cualquier prompt cambia los veredictos, así que `AGENTE_VERSION`
sube con él. Las herramientas terminales (`informar`, `concluir`, `estimar`,
`dictaminar`) van en el formato de funciones de Ollama/OpenAI para que el
formato lo valide el servidor y ningún rol pueda terminar sin pasar por la
validación en código (`citas.py`, `equipo.py`, `juez.py`).
"""

from __future__ import annotations

from src.agents.survival.insumo import Negocio
from src.shared.enums import (
    Confianza,
    Dictamen,
    EstadoVerificado,
    Identificacion,
    Recolector,
    SenalEvidencia,
)

# ----- piezas comunes -----

IDENTIDAD = """IDENTIDAD
La identidad de un negocio es nombre + lugar. Un homónimo en otro domicilio o municipio NO es este negocio. Un cambio de nombre en el mismo domicilio puede serlo (el DENUE lista los nombres con que apareció). Para cadenas y franquicias (OXXO, farmacias, bancos) exige el domicilio exacto."""

COMO_CITAR = """CÓMO CITAR
Cada cita lleva:
- pieza: el número [n] de la pieza.
- extracto: COPIADO TEXTUALMENTE de esa pieza. Se rechaza si no aparece tal cual; puedes recortar con '...'.
- fecha_publicacion: AAAA-MM-DD o AAAA-MM SOLO si está escrita en esa pieza; si no, null. No la deduzcas ni la estimes. Las fechas relativas que vienen anotadas como "hace 2 meses [≈ 2026-09]" valen como ese mes; las que no traen anotación no valen.
- senal: hacia dónde apunta esa pieza por sí sola: activo, cerrado o neutral.
Una pieza del negocio cuya actividad fechada más reciente es anterior a la última vez que el DENUE lo vio, y que no muestra nada después, apunta a cerrado. No se pueden citar piezas marcadas ERROR o SIN_RESULTADOS. Las piezas de fuente directorio_denue copian al propio DENUE: que el negocio aparezca ahí no prueba que siga abierto ni que cerró, así que su señal es neutral."""

REGLAS_DE_DECISION = """REGLAS DE DECISIÓN
- cerrado / alta: estado explícito de cierre ("Cerrado permanentemente" en Maps, aviso del propio negocio), o el local con otro ocupante verificable en ese mismo domicilio.
- cerrado / media: ninguna señal de actividad posterior a la última vez que el DENUE lo vio, en al menos DOS fuentes donde SÍ se identificó al negocio con actividad anterior a esa fecha.
- cerrado / baja: una sola fuente que identificó al negocio apunta a cierre, sin estado explícito (el sitio propio ahora es de otra cosa, la última publicación es vieja y no hay nada después).
- activo / alta: actividad FECHADA en o después del último corte publicado del DENUE (reseña, publicación, pedido). Una ficha o un extracto sin fecha NO alcanza para alta.
- activo / media: actividad fechada posterior a la última vez que el DENUE lo vio, pero anterior al último corte publicado.
- activo / baja: ficha vigente (horario, "abierto") sin fecha.
- indeterminado: sin huella, huella ambigua, homónimos imposibles de separar, o cadena sin domicilio exacto. El silencio solo (no aparece en ningún lado) es indeterminado: muchos negocios chicos nunca tuvieron huella en línea. Es un resultado LEGÍTIMO; no lo fuerces a activo ni a cerrado."""


def _enum(valores: type) -> list[str]:
    return [v.value for v in valores]


_CITAS_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "pieza": {"type": "integer", "description": "El número [n] de la pieza."},
            "extracto": {"type": "string", "description": "Copiado textual de la pieza."},
            "fecha_publicacion": {
                "type": ["string", "null"],
                "description": "AAAA-MM-DD o AAAA-MM, solo si está escrita en la pieza; null si no.",
            },
            "senal": {"type": "string", "enum": _enum(SenalEvidencia)},
        },
        "required": ["pieza", "extracto", "senal"],
    },
}
"""Una lista de citas: la misma forma para especialista, decisor y juez."""


# ----- el negocio -----


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
        ]
    )


# ----- especialista -----

FOCOS: dict[str, tuple[str, str]] = {
    Recolector.MAPS.value: (
        "Google Maps",
        "Te tocan las fichas de Google Maps. Lo que más vale: el estado explícito "
        '("Cerrado permanentemente", "Cerrado temporalmente"), el horario vigente y la '
        "antigüedad de la reseña más reciente. Si la búsqueda devolvió una lista y no una "
        "ficha, revisa si alguna coincide en nombre Y domicilio. Comprueba que la dirección "
        "de la ficha sea la del DENUE.",
    ),
    Recolector.WEB.value: (
        "el buscador web",
        "Te tocan los resultados de un buscador (título, URL, extracto). Busca: avisos de "
        "cierre, traspaso o cambio de domicilio; noticias (clausuras, incendios); presencia en "
        "plataformas de entrega (Rappi, Uber Eats, DiDi Food) o en directorios con reseñas "
        "fechadas. Los extractos casi nunca traen fecha: sin fecha escrita, no la inventes.",
    ),
    Recolector.REDES.value: (
        "las redes sociales",
        "Te tocan las páginas de Facebook e Instagram, abiertas sin iniciar sesión. Lo que "
        "vale: la fecha de la última publicación, avisos de cierre o de mudanza, el horario. "
        "Sin sesión a veces solo se ve el encabezado de la página: dilo así, no lo completes.",
    ),
    Recolector.SITIO.value: (
        "el sitio web propio",
        "Te toca el sitio web que el DENUE publicó del negocio. Si ahora es de otra cosa o "
        "está en venta, es señal de cierre; si solo falló la conexión, es neutral; si muestra "
        "actividad vigente con fecha, es señal de actividad.",
    ),
}
"""Por fuente: cómo se llama para el modelo y en qué fijarse."""

_AMPLIAR: dict[str, str] = {
    Recolector.MAPS.value: "Puedes hacer hasta {k} búsquedas más con buscar_en_maps (otro nombre con que apareció, el domicilio) si las piezas no bastan para identificarlo.",
    Recolector.WEB.value: "Puedes hacer hasta {k} búsquedas más con buscar_web.",
    Recolector.REDES.value: "Puedes abrir hasta {k} páginas más con leer_red, solo de la lista que se te da.",
}
"""Cómo se le explica a cada especialista su herramienta de ampliación."""


def sistema_especialista(recolector: str, max_ampliaciones: int) -> str:
    """El prompt de sistema del especialista de una fuente."""
    nombre, foco = FOCOS[recolector]
    ampliar = _AMPLIAR.get(recolector, "").format(k=max_ampliaciones) if max_ampliaciones else ""
    return f"""Eres el especialista en {nombre} de un equipo que verifica si un negocio de México SIGUE ACTIVO o CERRÓ. El Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) no reporta cierres: solo deja de listar negocios. Tú solo ves lo que {nombre} dijo de este negocio; otro miembro del equipo junta los informes de todas las fuentes y decide. Tu trabajo es leer bien TU fuente.

{foco}

{ampliar}

TU INFORME (herramienta informar, obligatoria para terminar)
- identificado: si (las piezas son de ESTE negocio: nombre y lugar coinciden), no (no aparece, o solo hay homónimos), dudoso.
- senal: activo, cerrado o neutral: lo que tu fuente, por sí sola, indica del negocio. Si identificado no es "si", la señal es neutral.
- resumen: una a tres frases con lo que encontraste, nombrando fechas si las hay.
- citas: las piezas que sostienen tu informe. Si la señal es activo o cerrado, al menos una cita con esa señal.

{IDENTIDAD}

{COMO_CITAR}"""


HERRAMIENTA_INFORMAR = {
    "type": "function",
    "function": {
        "name": "informar",
        "description": "Tu informe sobre lo que tu fuente dice del negocio. Obligatoria para terminar.",
        "parameters": {
            "type": "object",
            "properties": {
                "identificado": {"type": "string", "enum": _enum(Identificacion)},
                "senal": {"type": "string", "enum": _enum(SenalEvidencia)},
                "resumen": {"type": "string"},
                "citas": _CITAS_SCHEMA,
            },
            "required": ["identificado", "senal", "resumen", "citas"],
        },
    },
}


# ----- decisor -----

SISTEMA_DECISOR = f"""Eres quien decide en un equipo que verifica si un negocio de México SIGUE ACTIVO o CERRÓ. El Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) no reporta cierres: solo deja de listar negocios, y no sabemos por qué. Tu veredicto es independiente del DENUE.

Recibes lo que el DENUE publicó del negocio y los INFORMES de los especialistas: uno por fuente (Google Maps, buscador web, redes sociales, sitio propio). Cada informe dice si la fuente identificó al negocio, qué indica y con qué piezas. Tú no puedes buscar: decides con los informes.

Al pesar los informes: un estado explícito en Maps pesa más que una publicación; una pieza fechada pesa más que una sin fecha; una fuente que no identificó al negocio no aporta nada; si dos fuentes se contradicen, gana la más reciente con fecha y lo dices en el razonamiento.

{REGLAS_DE_DECISION}

AL CONCLUIR (herramienta concluir, obligatoria)
- estado y confianza según las reglas.
- razonamiento: por qué, en prosa breve, nombrando fuentes y fechas.
- evidencias: las piezas en que te apoyas, SOLO de las que citó algún especialista, con el mismo formato de cita. Para activo o cerrado, al menos una evidencia con esa señal. Guarda también las contradictorias.
- fecha_cierre_estimada: solo si la evidencia la da (último rastro de actividad, aviso).

{COMO_CITAR}"""
"""Prompt de sistema del decisor."""

HERRAMIENTA_CONCLUIR = {
    "type": "function",
    "function": {
        "name": "concluir",
        "description": "Registra el veredicto final. Obligatoria para terminar.",
        "parameters": {
            "type": "object",
            "properties": {
                "estado": {"type": "string", "enum": _enum(EstadoVerificado)},
                "confianza": {"type": "string", "enum": _enum(Confianza)},
                "razonamiento": {"type": "string"},
                "fecha_cierre_estimada": {
                    "type": ["string", "null"],
                    "description": "AAAA-MM-DD o AAAA-MM si la evidencia la da; null si no.",
                },
                "evidencias": _CITAS_SCHEMA,
            },
            "required": ["estado", "confianza", "razonamiento", "evidencias"],
        },
    },
}


# ----- juez -----

SISTEMA_JUEZ = f"""Eres el juez de un equipo que verifica si un negocio de México SIGUE ACTIVO o CERRÓ. El Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) no reporta cierres: solo deja de listar negocios. Tu papel es auditar, y trabajas en DOS pasos:

PASO 1 (herramienta estimar). Recibes el negocio y el EXPEDIENTE completo: todo lo que trajo cada fuente, citado o no. Sin saber qué concluyó el equipo, di qué concluirías tú según las reglas.

PASO 2 (herramienta dictaminar). Después verás el veredicto del equipo y las piezas que citó. Dictamina:
- sostenido: las piezas citadas, tal como dicen en el expediente, son de ESTE negocio y alcanzan para ese estado CON esa confianza.
- no_sostenido: alguna cita no dice lo que se le atribuye, es de un homónimo, es de un directorio que copia al DENUE usada como prueba, o la confianza excede lo que las piezas permiten. También si el expediente trae una pieza que contradice el veredicto y el equipo la ignoró.
Lista en piezas_objetadas los [n] citados que rechazas. Tu dictamen no depende de tu estimación del paso 1: puedes haber concluido otra cosa y aun así sostener el veredicto si sus citas lo permiten.

{IDENTIDAD}

{REGLAS_DE_DECISION}"""
"""Prompt de sistema del juez."""

HERRAMIENTA_ESTIMAR = {
    "type": "function",
    "function": {
        "name": "estimar",
        "description": "Paso 1: tu propio veredicto, sin conocer el del equipo.",
        "parameters": {
            "type": "object",
            "properties": {
                "estado": {"type": "string", "enum": _enum(EstadoVerificado)},
                "confianza": {"type": "string", "enum": _enum(Confianza)},
                "razonamiento": {"type": "string"},
            },
            "required": ["estado", "confianza", "razonamiento"],
        },
    },
}

HERRAMIENTA_DICTAMINAR = {
    "type": "function",
    "function": {
        "name": "dictaminar",
        "description": "Paso 2: si las citas del equipo sostienen su veredicto.",
        "parameters": {
            "type": "object",
            "properties": {
                "dictamen": {"type": "string", "enum": _enum(Dictamen)},
                "motivo": {"type": "string"},
                "piezas_objetadas": {"type": "array", "items": {"type": "integer"}},
            },
            "required": ["dictamen", "motivo", "piezas_objetadas"],
        },
    },
}
