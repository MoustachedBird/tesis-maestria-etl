"""Los prompts del equipo y las herramientas con que cada rol termina.

Tres roles, cada uno con un contexto chico y una sola tarea:

- **Especialista** (uno por grupo de fuentes): lee lo que trajeron SUS fuentes y responde
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
    Especialidad,
    EstadoVerificado,
    Identificacion,
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
    """El negocio para el modelo, en texto: todo lo del DENUE, nada de la hipótesis.

    Los campos que cambian entre cortes van con sus variantes y los cortes en
    que apareció cada una, la más reciente primero.
    """

    def campo(etiqueta: str, clave: str, valores: list[str]) -> str:
        variantes = n.variantes.get(clave)
        if variantes:
            return f"{etiqueta} (y en qué cortes, el más reciente primero): " + " | ".join(
                f"«{valor}» ({', '.join(cortes)})" for valor, cortes in variantes
            )
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
    buscado = []
    if n.nombres_busqueda:
        buscado.append("Nombres: " + " | ".join(n.nombres_busqueda))
    if n.domicilios_busqueda:
        buscado.append("Domicilios: " + " | ".join(n.domicilios_busqueda))
    if n.telefono_busqueda:
        buscado.append(f"Teléfono: {n.telefono_busqueda}")
    return "\n".join(
        [
            "NEGOCIO A VERIFICAR",
            f"Nombre principal: {n.nombre or '(sin nombre)'}",
            campo("Nombres con que apareció", "nombre", n.nombres),
            campo("Razón social", "razon_social", n.razones_sociales),
            f"Actividad: {actividad}",
            f"Tamaño (último corte): {n.tamano_final or 'desconocido'}",
            f"Localidad: {n.localidad or '?'} · Municipio: {n.municipio or '?'} · Estado: {n.entidad or '?'}",
            campo("Domicilios publicados", "domicilio", n.domicilios),
            *(
                [campo("Entre calles", "entre_calles", [])]
                if n.variantes.get("entre_calles")
                else []
            ),
            f"Coordenadas: {coordenadas}",
            campo("Teléfonos", "telefono", n.telefonos),
            campo("Sitios web", "sitio", n.sitios),
            campo("Correos", "correo", n.correos),
            *(["", "SE BUSCÓ EN LÍNEA CON", *buscado] if buscado else []),
            "",
            "PRESENCIA EN EL DENUE",
            f"Apareció en los cortes: {periodos}",
            f"Primera vez: {n.primer_periodo:%Y-%m}. Última vez: {n.ultimo_periodo:%Y-%m}.",
            f"Último corte publicado del DENUE: {n.ultimo_corte_ventana:%Y-%m}.",
        ]
    )


# ----- planificador -----

SISTEMA_PLANIFICADOR = """Eres quien decide CON QUÉ DATOS se busca en línea a un negocio de México, antes de que el equipo lo busque en Google Maps, en buscadores web, en redes sociales y en otras fuentes. El Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) lo registró en varios cortes, y como son datos de encuesta, lo que anotó el encuestador cambia de un corte a otro y a veces está mal escrito: el nombre (con y sin artículo, en otro orden, abreviado, con errores de captura, con el giro pegado, o porque el negocio cambió de nombre), el domicilio (el número con letra o con dígitos, abreviaturas, la calle escrita de otra forma, o porque se mudó) y el teléfono.

Recibes cada variante con los cortes en que apareció, la más reciente primero. Decide solo los campos que se te piden. Tu objetivo es que el equipo tenga LA MAYOR PROBABILIDAD DE ENCONTRARLO en internet hoy. Tú decides el criterio (cuántas veces aparece cada variante, combinar partes de varias, quitar lo que estorba), y lo más reciente pesa más: si el negocio se mudó o cambió de teléfono, el dato viejo ya no sirve para encontrarlo.

NOMBRES. Cómo es probable que aparezca en Google Maps o en Facebook. Puedes quitar "SA DE CV" o el giro genérico, reordenar y combinar; si hubo un cambio de nombre real, conviene buscar los dos. Solo con palabras que aparezcan en las variantes o en la razón social: no agregues ni corrijas palabras.

DOMICILIOS. No copies el más reciente: LÉELO. Usa todas las variantes y el contexto (tipo de vialidad, colonia o asentamiento, código postal, entre qué calles, municipio) para entender cuál es el domicilio real y escríbelo como lo escribiría Google Maps: tipo y nombre de la vialidad, número en dígitos, colonia, código postal. Por ejemplo, si un corte dice "CALLE FRANCISCO NUMERO 20" y otro "CALLE FRANCISCO NO. VEINTE", son el mismo domicilio: "Calle Francisco 20". Escribe solo la parte de la calle (vialidad, número, colonia, código postal): la localidad, el municipio y el estado los agrega el equipo a cada búsqueda. Corrige el formato (números escritos con letra a dígitos, abreviaturas como C., AV., COL., NO. desarrolladas o quitadas, "SN" como "sin número"), pero NO inventes una calle, un número ni una colonia que no estén en las variantes. Si se mudó, da primero el domicilio actual y, si sirve para encontrar rastros del negocio, el anterior. Si la vialidad tiene nombre poco buscable, puedes dar una segunda forma con la esquina ("Calle Francisco esquina Hidalgo").

TELÉFONO. Uno de los publicados, el que más probablemente siga vigente.

AL TERMINAR (herramienta elegir_datos, obligatoria)
- nombres: de 1 a {k}, el MEJOR primero. Solo si se te pide.
- domicilios: de 1 a {d}, el MEJOR primero, escritos para buscarse. Solo si se te pide.
- telefono: el que se usará. Solo si se te pide.
- razonamiento: por qué, en una a tres frases (si corregiste un domicilio, di qué corregiste)."""
"""Prompt de sistema del planificador; `{k}` es AGENTE_MAX_NOMBRES y `{d}` AGENTE_MAX_DOMICILIOS."""

HERRAMIENTA_ELEGIR_DATOS = {
    "type": "function",
    "function": {
        "name": "elegir_datos",
        "description": "Con qué nombres, domicilios y teléfono se buscará al negocio. Obligatoria.",
        "parameters": {
            "type": "object",
            "properties": {
                "nombres": {"type": "array", "items": {"type": "string"}},
                "domicilios": {"type": "array", "items": {"type": "string"}},
                "telefono": {"type": ["string", "null"]},
                "razonamiento": {"type": "string"},
            },
            "required": ["razonamiento"],
        },
    },
}


# ----- especialista -----

FOCOS: dict[str, tuple[str, str]] = {
    Especialidad.MAPS.value: (
        "los mapas",
        "Te tocan Google Maps y OpenStreetMap. Lo que más vale: el estado explícito de Maps "
        '("Cerrado permanentemente", "Cerrado temporalmente"), el horario vigente y la fecha de '
        "la reseña más reciente. Si la búsqueda devolvió una lista y no una ficha, revisa si "
        "alguna coincide en nombre Y domicilio. Comprueba que la dirección sea la del DENUE. "
        "En OpenStreetMap cada objeto trae su distancia al punto del DENUE y qué tanto se parece "
        "su nombre: úsalo para identificarlo. Una etiqueta disused:, was: o abandoned: dice que "
        "un mapeador registró el local como desocupado. La fecha de última edición de OSM es "
        "cuándo alguien editó el mapa, NO actividad del negocio: no la cites como señal activo.",
    ),
    Especialidad.WEB.value: (
        "los buscadores web",
        "Te tocan los resultados de varios buscadores (título, URL, extracto); un resultado que "
        "ya trajo un buscador no se repite en el siguiente. Busca: avisos de cierre, traspaso o "
        "cambio de domicilio; noticias (clausuras, incendios); presencia en plataformas de "
        "entrega (Rappi, Uber Eats, DiDi Food) o en directorios con reseñas fechadas. Los "
        "extractos casi nunca traen fecha: sin fecha escrita, no la inventes.",
    ),
    Especialidad.REDES.value: (
        "las redes sociales",
        "Te tocan las páginas de Facebook e Instagram, abiertas sin iniciar sesión. Lo que "
        "vale: la fecha de la última publicación, avisos de cierre o de mudanza, el horario. "
        "Sin sesión a veces solo se ve el encabezado de la página: dilo así, no lo completes.",
    ),
    Especialidad.PLATAFORMAS.value: (
        "las plataformas de reseñas y de entrega",
        "Te tocan fichas de TripAdvisor, Yelp, Foursquare, Sección Amarilla y plataformas de "
        'entrega (Rappi, Uber Eats, DiDi Food). Lo que vale: "cerrado definitivamente" o '
        '"cerrado permanentemente", si sigue recibiendo pedidos, y la fecha de la reseña más '
        "reciente. Comprueba que el domicilio sea el del DENUE.",
    ),
    Especialidad.SITIO.value: (
        "la huella digital",
        "Te tocan el sitio web que el DENUE publicó, sus capturas en el Internet Archive y en "
        "Common Crawl, y el registro de su dominio (o del dominio del correo). Si el sitio ahora "
        "es de otra cosa o está en venta, es señal de cierre; si solo falló la conexión, es "
        "neutral. Un dominio vencido, liberado o que ya no está registrado apunta a cierre, con "
        "la fecha del registro. La captura archivada más reciente fecha el último rastro del "
        "sitio: si es anterior a la última vez que el DENUE vio al negocio, no prueba actividad "
        "posterior. Que un sitio no tenga capturas es neutral.",
    ),
    Especialidad.REGISTROS.value: (
        "los registros oficiales",
        "Te tocan registros del gobierno buscados por la razón social: la lista de contribuyentes "
        "no localizados del SAT (el SAT no encontró al contribuyente en su domicilio fiscal), y "
        "publicaciones de sociedades mercantiles y del Diario Oficial (disolución, liquidación). "
        "Solo cuentan si la razón social coincide completa; una coincidencia parcial es otro "
        "contribuyente. Estar en la lista de no localizados apunta a cierre, con la fecha de "
        "publicación.",
    ),
    Especialidad.FACHADA.value: (
        "las imágenes de la calle",
        "Te tocan descripciones de fotos de la calle en las coordenadas del DENUE, escritas por "
        "un modelo de visión, con la fecha de la foto. Lo que vale: si el letrero es de este "
        "negocio, de otro, o si el local está vacío o en renta. La descripción es de un modelo, "
        "no de la fuente: úsala con prudencia.",
    ),
}
"""Por especialista: cómo se llama su grupo de fuentes para el modelo y en qué fijarse."""


def sistema_especialista(
    especialidad: str, max_ampliaciones: int, herramientas: list[dict] | None = None
) -> str:
    """El prompt de sistema del especialista de un grupo de fuentes.

    Args:
        especialidad: el grupo (`Especialidad`).
        max_ampliaciones: consultas extra que puede pedir; 0 = ninguna.
        herramientas: las de ampliación de sus fuentes; se le explican por nombre.
    """
    nombre, foco = FOCOS[especialidad]
    ampliar = ""
    if max_ampliaciones and herramientas:
        lista = "\n".join(
            f"- {h['function']['name']}: {h['function'].get('description', '')}"
            for h in herramientas
        )
        ampliar = (
            f"Si las piezas no bastan para identificarlo, puedes pedir hasta {max_ampliaciones} "
            f"consultas más en total, con:\n{lista}"
        )
    return f"""Eres el especialista en {nombre} de un equipo que verifica si un negocio de México SIGUE ACTIVO o CERRÓ. El Directorio Estadístico Nacional de Unidades Económicas (DENUE, del INEGI) no reporta cierres: solo deja de listar negocios. Tú solo ves lo que {nombre} dijeron de este negocio; otro miembro del equipo junta los informes de todas las fuentes y decide. Tu trabajo es leer bien TUS fuentes.

{foco}

{ampliar}

TU INFORME (herramienta informar, obligatoria para terminar)
- identificado: si (las piezas son de ESTE negocio: nombre y lugar coinciden), no (no aparece, o solo hay homónimos), dudoso.
- senal: activo, cerrado o neutral: lo que tus fuentes, por sí solas, indican del negocio. Si identificado no es "si", la señal es neutral.
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

Recibes lo que el DENUE publicó del negocio y los INFORMES de los especialistas: uno por grupo de fuentes (mapas, buscadores web, redes sociales, plataformas de reseñas y entrega, huella digital del sitio, registros oficiales, imágenes de la calle). Cada informe dice si la fuente identificó al negocio, qué indica y con qué piezas. Tú no puedes buscar: decides con los informes.

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
