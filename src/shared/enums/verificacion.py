"""Vocabulario del agente verificador: qué puede concluir y con qué apoyo.

Son enums y no texto libre porque las tres columnas que los usan son las que
se cuentan: la tasa de acierto de la regla de cierre sale de cruzar
`gold.negocios_clee_oficial.cerrado` contra `EstadoVerificado`, y un agente
que escribiera "CERRADO", "cerrado " o "closed" rompería ese conteo sin
error. El `CHECK` de la tabla se genera de estos valores.
"""

from enum import StrEnum


class EstadoVerificado(StrEnum):
    """Lo que el agente concluyó sobre el negocio, buscándolo en línea.

    Tres valores y no dos a propósito: `INDETERMINADO` es un resultado
    legítimo --el negocio no deja huella en línea, o la que deja no permite
    decidir-- y forzarlo a `activo` o `cerrado` contaminaría la medida de la
    regla con adivinanzas. La proporción de indeterminados es, además, un
    dato: dice qué tan verificable es la cohorte.
    """

    ACTIVO = "activo"
    CERRADO = "cerrado"
    INDETERMINADO = "indeterminado"


class Confianza(StrEnum):
    """Cuánto respalda la evidencia al veredicto, en tres niveles.

    Ordinal y no un número entre 0 y 1: un LLM que reporta 0.83 no está
    midiendo nada, y tres niveles son lo que un lector puede interpretar y
    lo que permite filtrar la muestra ("solo los veredictos con confianza
    alta") sin discutir un umbral decimal.
    """

    ALTA = "alta"
    MEDIA = "media"
    BAJA = "baja"


class SenalEvidencia(StrEnum):
    """Hacia dónde apunta UNA pieza de evidencia, por separado del veredicto.

    Se guarda por pieza y no solo en el veredicto porque la evidencia suele
    ser contradictoria (una ficha de Google Maps "cerrado permanentemente"
    y una página de Facebook con una publicación del mes pasado), y lo que
    vuelve auditable al agente es ver cómo pesó cada una.
    """

    CERRADO = "cerrado"
    ACTIVO = "activo"
    NEUTRAL = "neutral"


class Recolector(StrEnum):
    """La fuente concreta que produjo una pieza del expediente.

    El expediente se arma siempre con las mismas fuentes y en el mismo orden,
    para que dos negocios se verifiquen igual y sus veredictos sean
    comparables. Es el eje para medir qué aporta cada fuente ("¿cuántos
    veredictos firmes citaron a OpenStreetMap?"). Quién LEE cada pieza es
    otro eje, más grueso: `Especialidad`.

    Los valores de las fases que todavía no tienen código (`psm`, `dof`,
    `street_view`, `mapillary`, `kartaview`) ya están aquí para que el
    `CHECK` no pida otra migración cuando lleguen (`plan_fuentes_agente.md`).
    """

    MAPS = "maps"
    """Google Maps en Chrome: la ficha o la lista, y sus reseñas más recientes."""
    OSM = "osm"
    """OpenStreetMap por Overpass, alrededor de las coordenadas del DENUE."""
    WEB = "web"
    """SearXNG del compose (o la API de Brave si hay llave)."""
    BING = "bing"
    DUCKDUCKGO = "duckduckgo"
    GOOGLE = "google"
    BRAVE = "brave"
    """La página de Brave Search en Chrome (no su API: esa va en `web`)."""
    REDES = "redes"
    """Facebook e Instagram en Chrome, sin sesión."""
    PLATAFORMAS = "plataformas"
    """Fichas de reseñas y de entrega (TripAdvisor, Yelp, Rappi...) en Chrome."""
    SITIO = "sitio"
    """Los sitios que publicó el DENUE."""
    WAYBACK = "wayback"
    """Capturas del Internet Archive del sitio y de las redes."""
    COMMON_CRAWL = "common_crawl"
    """Capturas de Common Crawl del sitio y de las redes."""
    DOMINIO = "dominio"
    """Registro (RDAP/WHOIS) y DNS del dominio del sitio o del correo."""
    SAT = "sat"
    """Lista de contribuyentes no localizados del SAT (art. 69 CFF)."""
    PSM = "psm"
    DOF = "dof"
    STREET_VIEW = "street_view"
    MAPILLARY = "mapillary"
    KARTAVIEW = "kartaview"


class Especialidad(StrEnum):
    """El especialista del equipo que lee un grupo de fuentes.

    Un especialista por fuente serían ~20 llamadas al modelo por negocio; se
    agrupan las que se leen igual. Los cuatro primeros valores coinciden con
    los especialistas de v4 (`maps`, `web`, `redes`, `sitio`), así que sus
    filas en `informes` siguen siendo válidas.
    """

    MAPS = "maps"
    """Mapas: Google Maps y OpenStreetMap."""
    WEB = "web"
    """Buscadores: SearXNG, Bing, DuckDuckGo, Google, Brave."""
    REDES = "redes"
    PLATAFORMAS = "plataformas"
    SITIO = "sitio"
    """Huella digital: sitio propio, Internet Archive, Common Crawl, dominio."""
    REGISTROS = "registros"
    """Registros oficiales: SAT, PSM, DOF."""
    FACHADA = "fachada"
    """Imágenes de la calle leídas por un modelo de visión."""


ESPECIALIDAD_DE: dict[str, str] = {
    Recolector.MAPS: Especialidad.MAPS,
    Recolector.OSM: Especialidad.MAPS,
    Recolector.WEB: Especialidad.WEB,
    Recolector.BING: Especialidad.WEB,
    Recolector.DUCKDUCKGO: Especialidad.WEB,
    Recolector.GOOGLE: Especialidad.WEB,
    Recolector.BRAVE: Especialidad.WEB,
    Recolector.REDES: Especialidad.REDES,
    Recolector.PLATAFORMAS: Especialidad.PLATAFORMAS,
    Recolector.SITIO: Especialidad.SITIO,
    Recolector.WAYBACK: Especialidad.SITIO,
    Recolector.COMMON_CRAWL: Especialidad.SITIO,
    Recolector.DOMINIO: Especialidad.SITIO,
    Recolector.SAT: Especialidad.REGISTROS,
    Recolector.PSM: Especialidad.REGISTROS,
    Recolector.DOF: Especialidad.REGISTROS,
    Recolector.STREET_VIEW: Especialidad.FACHADA,
    Recolector.MAPILLARY: Especialidad.FACHADA,
    Recolector.KARTAVIEW: Especialidad.FACHADA,
}
"""Qué especialista lee las piezas de cada fuente. Todo `Recolector` tiene uno."""

BUSCADORES: frozenset[str] = frozenset(
    {Recolector.WEB, Recolector.BING, Recolector.DUCKDUCKGO, Recolector.GOOGLE, Recolector.BRAVE}
)
"""Los recolectores que son buscadores: de sus resultados salen las redes y plataformas a abrir."""


class EstadoConsulta(StrEnum):
    """Cómo terminó una consulta del recolector.

    Se guarda también lo que falló: que Maps no tenga ficha o que Facebook
    pida sesión es información sobre la cobertura de cada fuente, y sin la
    fila no se distingue "no se consultó" de "se consultó y no había nada".
    """

    OK = "ok"
    SIN_RESULTADOS = "sin_resultados"
    ERROR = "error"


class Dictamen(StrEnum):
    """Lo que el juez concluye del veredicto que audita.

    `SOSTENIDO`: las piezas que el decisor citó dicen lo que les atribuye y
    alcanzan para ese estado con esa confianza. `NO_SOSTENIDO`: alguna cita
    no dice eso, es de un homónimo o de un directorio que copia al DENUE, o
    la confianza excede lo que las piezas permiten.
    """

    SOSTENIDO = "sostenido"
    NO_SOSTENIDO = "no_sostenido"


class Identificacion(StrEnum):
    """Si lo que trajo una fuente es de ESTE negocio, según su especialista.

    La identidad es nombre + lugar: una taquería homónima en otro municipio
    no dice nada de la del DENUE. Se separa de la señal porque son errores
    distintos --encontrar al negocio equivocado y leer mal al correcto-- y
    el análisis los cuenta por separado.
    """

    SI = "si"
    NO = "no"
    DUDOSO = "dudoso"
