"""Lo que el agente recibe de un negocio: todo lo que el DENUE publicó de él.

Los valores distintos salen de **Silver**, no de gold (no hay tabla de
variantes): nombres, razones sociales, teléfonos, sitios, domicilios de los
nueve cortes. Es lo que un verificador humano usaría para buscarlo.

**No lleva `cerrado_gold`, ni `meses_sin_aparecer`, ni `huecos`.** El
veredicto tiene que ser ciego a la hipótesis que se mide; si el agente
supiera lo que gold dice, lo confirmaría. Sí lleva `ultimo_periodo` y los
cortes en que apareció, porque son datos del DENUE que necesita para
preguntarse "¿hay actividad después de esa fecha?".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from sqlalchemy import text
from sqlalchemy.engine import Connection


@dataclass
class Negocio:
    """Un negocio de la muestra, con lo que el DENUE publicó de él en todos los cortes."""

    muestra_id: str
    clee: str
    orden: int
    estrato: str
    nombre: str | None
    codigo_act: str | None
    actividad: str | None
    tamano_final: str | None
    cve_geo_final: str | None
    entidad: str | None
    municipio: str | None
    lat: float | None
    lon: float | None
    primer_periodo: date
    ultimo_periodo: date
    periodos: list[date]
    ultimo_corte_ventana: date
    nombres: list[str] = field(default_factory=list)
    razones_sociales: list[str] = field(default_factory=list)
    telefonos: list[str] = field(default_factory=list)
    correos: list[str] = field(default_factory=list)
    sitios: list[str] = field(default_factory=list)
    domicilios: list[str] = field(default_factory=list)
    localidad: str | None = None
    """El nombre de la localidad del último corte (`cve_geo_final`). En zonas rurales es lo que
    ubica al negocio: un municipio tiene decenas de localidades y Maps busca por localidad."""
    variantes: dict[str, list[tuple[str, list[str]]]] = field(default_factory=dict)
    """Por campo de `CAMPOS_VARIABLES`: cada valor distinto con los cortes (AAAA-MM) en que apareció,
    del más reciente al más viejo.

    Es lo que el planificador necesita para razonar por repetición y por
    recencia: no es lo mismo un domicilio de siete cortes que uno de uno.
    Las listas sueltas (`nombres`, `domicilios`...) son los mismos valores,
    en ese mismo orden.
    """
    nombres_busqueda: list[str] = field(default_factory=list)
    """Los nombres con que se busca, elegidos por el planificador (`equipo.Planificador`).

    Vacío hasta que el planificador decide; las fuentes usan entonces el de
    gold (`recoleccion.nombres_de_busqueda`).
    """
    domicilios_busqueda: list[str] = field(default_factory=list)
    """Los domicilios con que se busca, escritos por el planificador para encontrarlo en línea
    (números en dígitos, sin abreviaturas); vacío = el más reciente del DENUE."""
    telefono_busqueda: str | None = None
    """El teléfono con que se busca (planificador); None = el más reciente del DENUE."""


def muestra_por_nombre(conn: Connection, nombre: str) -> tuple[str, list[date]]:
    """El id y la ventana de una muestra, por su nombre.

    Raises:
        ValueError: si no existe.
    """
    fila = conn.execute(
        text("SELECT muestra_id, ventana_cortes FROM verificacion.muestras WHERE nombre = :n"),
        {"n": nombre},
    ).first()
    if fila is None:
        raise ValueError(f"no existe la muestra {nombre!r}; `muestrear --nombre {nombre}` la crea")
    return fila.muestra_id, list(fila.ventana_cortes)


def pendientes(conn: Connection, muestra_id: str, agente: str) -> list[tuple[int, str]]:
    """Los negocios de la muestra que ESTE agente no ha verificado, en orden.

    Es lo que vuelve reanudable la verificación: otro agente (otro modelo u
    otra versión) tiene su propia lista de pendientes, porque sus veredictos
    son otras filas.
    """
    return [
        (int(orden), clee)
        for orden, clee in conn.execute(
            text("""
            SELECT mn.orden, mn.clee
            FROM verificacion.muestra_negocios mn
            WHERE mn.muestra_id = :id
              AND NOT EXISTS (
                  SELECT 1 FROM verificacion.veredictos v
                  WHERE v.muestra_id = mn.muestra_id AND v.clee = mn.clee AND v.agente = :agente
              )
            ORDER BY mn.orden
            """),
            {"id": muestra_id, "agente": agente},
        ).all()
    ]


FROM_HECHOS = """silver.establecimientos e
                  LEFT JOIN silver.denue_tipos_vialidad tv ON tv.id = e.tipo_vial_id
                  LEFT JOIN silver.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id"""
"""Los hechos con las etiquetas de vialidad y asentamiento: para escribir el domicilio completo."""


def _dato(columna: str) -> str:
    """Una columna de texto del DENUE sin los rellenos de la encuesta: vacío o "NINGUNO".

    El encuestador anota "NINGUNO" cuando no hay entre calle o tipo de
    vialidad; sin quitarlo, el planificador escribió "entre Calle Ninguno"
    (v6-piloto).
    """
    return f"NULLIF(NULLIF(NULLIF({columna}, ''), 'NINGUNO'), 'NINGUNA')"


_DOMICILIO = (
    f"concat_ws(' ', {_dato('tv.tipo_vial')}, {_dato('e.nom_vial')}, {_dato('e.numero_ext')}, "
    f"{_dato('ta.tipo_asent')}, {_dato('e.nomb_asent')}, {_dato('e.cod_postal')})"
)
"""El domicilio de una observación como una línea: tipo y nombre de vialidad, número,
tipo y nombre de asentamiento, CP ("CALLE FRANCISCO 20 COLONIA CENTRO 37630")."""

_ENTRE_CALLES = (
    f"concat_ws(' Y ', NULLIF(concat_ws(' ', {_dato('e.tipo_v_e_1')}, {_dato('e.nom_v_e_1')}), ''), "
    f"NULLIF(concat_ws(' ', {_dato('e.tipo_v_e_2')}, {_dato('e.nom_v_e_2')}), ''))"
)
"""Entre qué calles está: contexto para que el planificador escriba un domicilio buscable."""

CAMPOS_VARIABLES: dict[str, str] = {
    "nombre": "e.nom_estab",
    "domicilio": _DOMICILIO,
    "entre_calles": _ENTRE_CALLES,
    "telefono": "e.telefono",
    "razon_social": "e.raz_social",
    "sitio": "e.www",
    "correo": "e.correoelec",
}
"""Los campos del DENUE que cambian de un corte a otro, con la expresión que los lee.

Son datos de encuesta: el encuestador anota el nombre, el domicilio o el
teléfono como se lo dicen ese día, así que un mismo negocio aparece con
variantes. De cada uno se cargan TODAS las variantes con los cortes en que
aparecieron, de la más reciente a la más vieja.
"""


def sql_variantes(expresion: str, clee: str = "mn.clee") -> str:
    """Subconsulta: las variantes de un campo con sus cortes, JSON, la más reciente primero."""
    return f"""(SELECT jsonb_agg(jsonb_build_object('valor', x.v, 'cortes', x.cortes)
                         ORDER BY x.ultimo DESC, x.v)
          FROM (SELECT {expresion} AS v,
                       array_agg(to_char(e.period, 'YYYY-MM') ORDER BY e.period) AS cortes,
                       max(e.period) AS ultimo
                  FROM {FROM_HECHOS}
                 WHERE e.clee = {clee} AND {expresion} <> ''
                 GROUP BY 1) x)"""


_SQL_INSUMO = f"""
SELECT mn.muestra_id, mn.clee, mn.orden, mn.estrato,
       g.nombre, g.codigo_act, g.tamano_final, g.cve_geo_final,
       g.lat_final, g.lon_final, g.primer_periodo, g.ultimo_periodo, g.periodos,
       m.ventana_cortes[array_upper(m.ventana_cortes, 1)] AS ultimo_corte_ventana,
       (SELECT a.nombre_act FROM silver.denue_actividades_economicas a
         WHERE a.codigo_act = g.codigo_act
            OR (length(g.codigo_act) < 6 AND a.codigo_act LIKE g.codigo_act || '%')
         ORDER BY a.codigo_act LIMIT 1) AS actividad,
       (SELECT en.entidad FROM silver.denue_entidades en
         WHERE en.cve_ent = LEFT(g.cve_geo_final, 2)) AS entidad,
       (SELECT mu.municipio FROM silver.denue_municipios mu
         WHERE mu.cve_ent = LEFT(g.cve_geo_final, 2) AND mu.cve_mun = SUBSTR(g.cve_geo_final, 3, 3)) AS municipio,
       (SELECT lo.localidad FROM silver.denue_localidades lo
         WHERE length(g.cve_geo_final) >= 9
           AND lo.cve_ent = LEFT(g.cve_geo_final, 2) AND lo.cve_mun = SUBSTR(g.cve_geo_final, 3, 3)
           AND lo.cve_loc = SUBSTR(g.cve_geo_final, 6, 4)) AS localidad,
       {", ".join(f"{sql_variantes(expr)} AS v_{campo}" for campo, expr in CAMPOS_VARIABLES.items())}
FROM verificacion.muestra_negocios mn
JOIN verificacion.muestras m USING (muestra_id)
JOIN gold.negocios_clee_oficial g ON g.clee = mn.clee
WHERE mn.muestra_id = :id AND mn.clee = :clee
"""
"""El insumo de UN negocio. Deliberadamente sin `cerrado_gold` ni los meses."""


def cargar(conn: Connection, muestra_id: str, clee: str) -> Negocio:
    """El insumo completo de un negocio de la muestra.

    Raises:
        LookupError: si el negocio no está en la muestra o ya no está en gold
            (gold se reconstruyó con otra cohorte).
    """
    fila = conn.execute(text(_SQL_INSUMO), {"id": muestra_id, "clee": clee}).mappings().first()
    if fila is None:
        raise LookupError(f"{clee} no esta en la muestra {muestra_id} o ya no esta en gold")
    variantes = {
        campo: [
            (str(x["valor"]), [str(c) for c in x["cortes"]]) for x in (fila[f"v_{campo}"] or [])
        ]
        for campo in CAMPOS_VARIABLES
    }

    def valores(campo: str) -> list[str]:
        return [v for v, _ in variantes[campo]]

    return Negocio(
        muestra_id=fila["muestra_id"],
        clee=fila["clee"],
        orden=fila["orden"],
        estrato=fila["estrato"],
        nombre=fila["nombre"],
        codigo_act=fila["codigo_act"],
        actividad=fila["actividad"],
        tamano_final=fila["tamano_final"],
        cve_geo_final=fila["cve_geo_final"],
        entidad=fila["entidad"],
        municipio=fila["municipio"],
        localidad=fila["localidad"],
        lat=fila["lat_final"],
        lon=fila["lon_final"],
        primer_periodo=fila["primer_periodo"],
        ultimo_periodo=fila["ultimo_periodo"],
        periodos=list(fila["periodos"] or []),
        ultimo_corte_ventana=fila["ultimo_corte_ventana"],
        nombres=valores("nombre"),
        razones_sociales=valores("razon_social"),
        telefonos=valores("telefono"),
        correos=valores("correo"),
        sitios=valores("sitio"),
        domicilios=valores("domicilio"),
        variantes=variantes,
    )
