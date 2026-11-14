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


_SQL_INSUMO = """
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
       v.nombres, v.razones_sociales, v.telefonos, v.correos, v.sitios, v.domicilios
FROM verificacion.muestra_negocios mn
JOIN verificacion.muestras m USING (muestra_id)
JOIN gold.negocios_clee_oficial g ON g.clee = mn.clee
LEFT JOIN LATERAL (
    SELECT array_agg(DISTINCT e.nom_estab)  FILTER (WHERE e.nom_estab  <> '') AS nombres,
           array_agg(DISTINCT e.raz_social) FILTER (WHERE e.raz_social <> '') AS razones_sociales,
           array_agg(DISTINCT e.telefono)   FILTER (WHERE e.telefono   <> '') AS telefonos,
           array_agg(DISTINCT e.correoelec) FILTER (WHERE e.correoelec <> '') AS correos,
           array_agg(DISTINCT e.www)        FILTER (WHERE e.www        <> '') AS sitios,
           array_agg(DISTINCT NULLIF(concat_ws(' ', NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
                                                    NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')), ''))
                                            FILTER (WHERE e.nom_vial <> '' OR e.nomb_asent <> '') AS domicilios
    FROM silver.establecimientos e
    WHERE e.clee = mn.clee
) v ON TRUE
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
        lat=fila["lat_final"],
        lon=fila["lon_final"],
        primer_periodo=fila["primer_periodo"],
        ultimo_periodo=fila["ultimo_periodo"],
        periodos=list(fila["periodos"] or []),
        ultimo_corte_ventana=fila["ultimo_corte_ventana"],
        nombres=list(fila["nombres"] or []),
        razones_sociales=list(fila["razones_sociales"] or []),
        telefonos=list(fila["telefonos"] or []),
        correos=list(fila["correos"] or []),
        sitios=list(fila["sitios"] or []),
        domicilios=list(fila["domicilios"] or []),
    )
