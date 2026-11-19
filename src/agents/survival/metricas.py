"""El cruce: lo que gold afirmó contra lo que el agente encontró, por estrato.

Es la §6 de `verificacion_cierre.md`. Se toma el veredicto MÁS RECIENTE de
cada negocio para el agente pedido (o para todos), y se cuenta la tabla de
confusión contra `cerrado_gold`, que es la hipótesis congelada al muestrear.

La muestra iguala los estratos (100 cada uno) y la población no, así que los
números globales se **reponderan** por el tamaño real de cada estrato en gold
(`muestreo.tamanos_poblacion`). Sin eso, la precisión global sería la de una
población imaginaria con tantos cerrados como activos.

Con `solo_sostenidos` se cuentan únicamente los veredictos que el juez
sostuvo; los demás pasan a "sin veredicto". Es la versión conservadora de
la medida: menos negocios, pero cada uno con dos lecturas que coinciden en
que la evidencia alcanza.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.engine import Connection

from src.agents.survival import insumo
from src.agents.survival.muestreo import ESTRATOS, tamanos_poblacion


@dataclass
class FilaEstrato:
    """La tabla de confusión de un estrato, sobre el veredicto más reciente de cada negocio."""

    estrato: str
    n: int
    sin_veredicto: int
    indeterminados: int
    vp: int  # gold cerrado, agente cerrado
    fp: int  # gold cerrado, agente activo
    fn: int  # gold activo,  agente cerrado
    vn: int  # gold activo,  agente activo
    poblacion: int

    @property
    def determinados(self) -> int:
        return self.vp + self.fp + self.fn + self.vn

    @property
    def precision_cerrado(self) -> float | None:
        """De los que gold declaró cerrados y se pudieron verificar, qué fracción cerró."""
        return self.vp / (self.vp + self.fp) if (self.vp + self.fp) else None

    @property
    def tasa_falsos_negativos(self) -> float | None:
        """De los que gold declaró activos y se pudieron verificar, qué fracción ya cerró."""
        return self.fn / (self.fn + self.vn) if (self.fn + self.vn) else None

    @property
    def tasa_indeterminados(self) -> float | None:
        verificados = self.n - self.sin_veredicto
        return self.indeterminados / verificados if verificados else None


_SQL = """
WITH ultimo AS (
    SELECT DISTINCT ON (muestra_id, clee) muestra_id, clee, estado
    FROM verificacion.veredictos v
    WHERE muestra_id = :id AND (CAST(:agente AS text) IS NULL OR agente = CAST(:agente AS text))
      AND (NOT CAST(:solo_sostenidos AS boolean) OR EXISTS (
              SELECT 1 FROM verificacion.auditorias a
              WHERE a.veredicto_id = v.veredicto_id AND a.dictamen = 'sostenido'))
    ORDER BY muestra_id, clee, verificado_en DESC
)
SELECT mn.estrato,
       count(*)                                                             AS n,
       count(*) FILTER (WHERE u.estado IS NULL)                             AS sin_veredicto,
       count(*) FILTER (WHERE u.estado = 'indeterminado')                   AS indeterminados,
       count(*) FILTER (WHERE mn.cerrado_gold     AND u.estado = 'cerrado') AS vp,
       count(*) FILTER (WHERE mn.cerrado_gold     AND u.estado = 'activo')  AS fp,
       count(*) FILTER (WHERE NOT mn.cerrado_gold AND u.estado = 'cerrado') AS fn,
       count(*) FILTER (WHERE NOT mn.cerrado_gold AND u.estado = 'activo')  AS vn
FROM verificacion.muestra_negocios mn
LEFT JOIN ultimo u USING (muestra_id, clee)
WHERE mn.muestra_id = :id
GROUP BY mn.estrato
"""


def calcular(
    conn: Connection, nombre: str, agente: str | None = None, *, solo_sostenidos: bool = False
) -> list[FilaEstrato]:
    """La tabla de confusión por estrato de una muestra.

    Args:
        conn: conexión de lectura.
        nombre: la muestra.
        agente: `modelo/version` cuyo veredicto se cuenta; None = el más
            reciente de cualquiera.
        solo_sostenidos: contar solo los veredictos que algún juez sostuvo.

    Returns:
        list[FilaEstrato]: una por estrato, en el orden de `ESTRATOS`.
    """
    muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
    poblacion = tamanos_poblacion(conn)
    filas = {
        f["estrato"]: f
        for f in conn.execute(
            text(_SQL), {"id": muestra_id, "agente": agente, "solo_sostenidos": solo_sostenidos}
        )
        .mappings()
        .all()
    }
    salida = []
    for estrato, _ in ESTRATOS:
        f = filas.get(estrato)
        salida.append(
            FilaEstrato(
                estrato=estrato,
                n=int(f["n"]) if f else 0,
                sin_veredicto=int(f["sin_veredicto"]) if f else 0,
                indeterminados=int(f["indeterminados"]) if f else 0,
                vp=int(f["vp"]) if f else 0,
                fp=int(f["fp"]) if f else 0,
                fn=int(f["fn"]) if f else 0,
                vn=int(f["vn"]) if f else 0,
                poblacion=poblacion.get(estrato, 0),
            )
        )
    return salida


def precision_cerrado_reponderada(filas: list[FilaEstrato]) -> float | None:
    """Precisión de `cerrado` en la POBLACIÓN: media de los estratos cerrados, pesada por su tamaño real.

    Solo entran los estratos con al menos un veredicto determinado sobre
    cerrados; si ninguno lo tiene, no hay número.
    """
    pesos = [(f.poblacion, f.precision_cerrado) for f in filas if f.precision_cerrado is not None]
    total = sum(p for p, _ in pesos)
    if not total:
        return None
    return sum(p * v for p, v in pesos) / total


def tasa_falsos_negativos_reponderada(filas: list[FilaEstrato]) -> float | None:
    """Fracción de los `activos` de gold que ya cerraron, en la población."""
    pesos = [
        (f.poblacion, f.tasa_falsos_negativos) for f in filas if f.tasa_falsos_negativos is not None
    ]
    total = sum(p for p, _ in pesos)
    if not total:
        return None
    return sum(p * v for p, v in pesos) / total


@dataclass
class FilaAuditoria:
    """Lo que dijo el juez de los veredictos de un estrato."""

    estrato: str
    auditados: int
    sostenidos: int
    acuerdo: int

    @property
    def tasa_sostenidos(self) -> float | None:
        """Fracción de veredictos cuyas citas el juez consideró suficientes."""
        return self.sostenidos / self.auditados if self.auditados else None

    @property
    def tasa_acuerdo(self) -> float | None:
        """Fracción en que el juez, a ciegas, llegó al mismo estado que el equipo."""
        return self.acuerdo / self.auditados if self.auditados else None


_SQL_AUDITORIA = """
SELECT mn.estrato,
       count(*)                                        AS auditados,
       count(*) FILTER (WHERE a.dictamen = 'sostenido') AS sostenidos,
       count(*) FILTER (WHERE a.estado_ciego = v.estado) AS acuerdo
FROM verificacion.auditorias a
JOIN verificacion.veredictos v USING (veredicto_id)
JOIN verificacion.muestra_negocios mn USING (muestra_id, clee)
WHERE v.muestra_id = :id
  AND (CAST(:agente AS text) IS NULL OR v.agente = CAST(:agente AS text))
GROUP BY mn.estrato
"""


def auditoria(conn: Connection, nombre: str, agente: str | None = None) -> list[FilaAuditoria]:
    """Dictámenes y acuerdo a ciegas del juez, por estrato.

    Args:
        conn: conexión de lectura.
        nombre: la muestra.
        agente: `modelo/version` del equipo cuyos veredictos se cuentan; None = todos.

    Returns:
        list[FilaAuditoria]: una por estrato, en el orden de `ESTRATOS`.
    """
    muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
    filas = {
        f["estrato"]: f
        for f in conn.execute(text(_SQL_AUDITORIA), {"id": muestra_id, "agente": agente})
        .mappings()
        .all()
    }
    return [
        FilaAuditoria(
            estrato=estrato,
            auditados=int(filas[estrato]["auditados"]) if estrato in filas else 0,
            sostenidos=int(filas[estrato]["sostenidos"]) if estrato in filas else 0,
            acuerdo=int(filas[estrato]["acuerdo"]) if estrato in filas else 0,
        )
        for estrato, _ in ESTRATOS
    ]
