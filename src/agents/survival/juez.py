"""El juez: audita un veredicto contra el expediente, en dos pasos.

Está fuera del equipo a propósito. No ve los informes de los especialistas
ni el razonamiento del decisor: relee el expediente CRUDO, así que también
atrapa errores de los especialistas (un homónimo identificado como el
negocio, una cita que no dice lo que se le atribuye).

1. **A ciegas** (`estimar`): con el negocio y el expediente, qué concluiría
   él. `estado_ciego = veredictos.estado` es el acuerdo entre dos lecturas
   independientes del mismo expediente: lo que la tesis puede reportar
   como confiabilidad del método.
2. **Dictamen** (`dictaminar`): ya viendo el estado y las citas del equipo,
   si esas piezas sostienen ese estado con esa confianza.

El juez lee lo GUARDADO (`verificacion.consultas`, `evidencias`), no lo que
el equipo tenía en memoria: audita exactamente lo que quedó registrado, y
por eso `auditar` puede correr después, con otro modelo como juez.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.engine import Connection, Engine

from src.agents.survival import insumo
from src.agents.survival.citas import Cita
from src.agents.survival.equipo import MIN_TEXTO
from src.agents.survival.insumo import Negocio
from src.agents.survival.llamada import pedir, tool
from src.agents.survival.prompts import (
    HERRAMIENTA_DICTAMINAR,
    HERRAMIENTA_ESTIMAR,
    SISTEMA_JUEZ,
    formatear_insumo,
)
from src.agents.survival.recoleccion import Pieza, formatear_expediente
from src.meta.tracker import MetadataTracker
from src.shared.db.models.verificacion import Auditoria
from src.shared.enums import (
    Confianza,
    Dictamen,
    EstadoVerificado,
    JobStatus,
    TaskStatus,
    TriggerType,
)
from src.shared.llm import OllamaNoDisponible
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

JOB_NAME_AUDITAR = "agente_supervivencia_auditar"
"""Nombre de la corrida de auditoría en `meta.job_runs`."""


class JuezSinDictamen(RuntimeError):
    """El juez no produjo una salida válida en alguno de sus dos pasos."""


@dataclass
class VeredictoAuditado:
    """Lo que el juez ve del veredicto en el paso 2: estado, confianza y citas, sin razonamiento."""

    veredicto_id: str
    estado: str
    confianza: str
    fecha_cierre_estimada: date | None = None
    evidencias: list[Cita] = field(default_factory=list)


@dataclass
class ResultadoAuditoria:
    """Lo que el juez dijo de un veredicto."""

    juez: str
    estado_ciego: str
    confianza_ciega: str
    razonamiento_ciego: str
    dictamen: str
    motivo: str
    piezas_objetadas: list[int]
    duracion_s: float = 0.0


class Juez:
    """El auditor: otra llamada al modelo, sin herramientas de búsqueda."""

    def __init__(
        self, cliente: Any, *, version: str, max_intentos: int, presupuesto: int | None = None
    ):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.modelo` y `.chat(...)`.
                Puede ser otro modelo que el del equipo.
            version: `AGENTE_VERSION`; va en `auditorias.juez` junto al modelo.
            max_intentos: vueltas por paso para producir una salida válida.
            presupuesto: caracteres de expediente que caben en su contexto
                (`recoleccion.presupuesto_de`); None = sin tope. Con veinte
                fuentes el expediente completo puede no caber.
        """
        self.cliente = cliente
        self.juez = f"{cliente.modelo}/{version}"
        self.max_intentos = max_intentos
        self.presupuesto = presupuesto

    def auditar(
        self, n: Negocio, expediente: list[Pieza], v: VeredictoAuditado
    ) -> ResultadoAuditoria:
        """Los dos pasos sobre un veredicto.

        Raises:
            JuezSinDictamen: si alguno de los pasos no produce una salida válida.
        """
        inicio = time.monotonic()
        mensajes = [
            {"role": "system", "content": SISTEMA_JUEZ},
            {
                "role": "user",
                "content": "\n".join(
                    [
                        formatear_insumo(n),
                        "",
                        "EXPEDIENTE",
                        "",
                        formatear_expediente(expediente, presupuesto=self.presupuesto),
                        "",
                        "PASO 1: sin conocer el veredicto del equipo, llama a `estimar`.",
                    ]
                ),
            },
        ]
        ciego = pedir(
            self.cliente,
            mensajes,
            [HERRAMIENTA_ESTIMAR],
            {"estimar"},
            _validar_estimacion,
            max_vueltas=self.max_intentos,
            rol="juez paso 1 (a ciegas)",
        )
        if ciego is None:
            raise JuezSinDictamen(f"el juez no estimó en {self.max_intentos} intentos")
        estado_ciego, confianza_ciega, razonamiento_ciego = ciego.valor

        citadas = {c.pieza for c in v.evidencias}
        # La llamada a `estimar` queda contestada antes del paso 2: una tool_call
        # sin su resultado deja la conversacion mal formada para el servidor.
        mensajes.append(tool("estimar", "Registrado."))
        mensajes.append({"role": "user", "content": _paso_2(v)})
        dictamen = pedir(
            self.cliente,
            mensajes,
            [HERRAMIENTA_DICTAMINAR],
            {"dictaminar"},
            lambda _n, args: _validar_dictamen(args, citadas),
            max_vueltas=self.max_intentos,
            rol="juez paso 2 (dictamen)",
        )
        if dictamen is None:
            raise JuezSinDictamen(f"el juez no dictaminó en {self.max_intentos} intentos")
        veredicto, motivo, objetadas = dictamen.valor
        return ResultadoAuditoria(
            juez=self.juez,
            estado_ciego=estado_ciego,
            confianza_ciega=confianza_ciega,
            razonamiento_ciego=razonamiento_ciego,
            dictamen=veredicto,
            motivo=motivo,
            piezas_objetadas=objetadas,
            duracion_s=time.monotonic() - inicio,
        )


def _paso_2(v: VeredictoAuditado) -> str:
    """El veredicto del equipo, como lo ve el juez: estado, confianza y citas, sin el porqué."""
    lineas = [
        "PASO 2. El veredicto del equipo:",
        f"estado: {v.estado} · confianza: {v.confianza}"
        + (
            f" · fecha de cierre estimada: {v.fecha_cierre_estimada:%Y-%m}"
            if v.fecha_cierre_estimada
            else ""
        ),
        "piezas citadas:",
    ]
    lineas += [f"  {c.como_texto()}" for c in v.evidencias] or ["  (ninguna)"]
    lineas += ["", "Contrástalas con el expediente y llama a `dictaminar`."]
    return "\n".join(lineas)


def _validar_estimacion(_nombre: str, args: dict) -> tuple[tuple[str, str, str], list[str]]:
    errores = []
    estado = str(args.get("estado", "")).strip().lower()
    confianza = str(args.get("confianza", "")).strip().lower()
    razonamiento = str(args.get("razonamiento", "")).strip()
    if estado not in {e.value for e in EstadoVerificado}:
        errores.append(f"estado {estado!r} no es uno de {[e.value for e in EstadoVerificado]}")
    if confianza not in {c.value for c in Confianza}:
        errores.append(f"confianza {confianza!r} no es una de {[c.value for c in Confianza]}")
    if len(razonamiento) < MIN_TEXTO:
        errores.append("razonamiento vacío o demasiado corto")
    return (estado, confianza, razonamiento), errores


def _validar_dictamen(
    args: dict, citadas: set[int]
) -> tuple[tuple[str, str, list[int]], list[str]]:
    errores = []
    dictamen = str(args.get("dictamen", "")).strip().lower()
    motivo = str(args.get("motivo", "")).strip()
    if dictamen not in {d.value for d in Dictamen}:
        errores.append(f"dictamen {dictamen!r} no es uno de {[d.value for d in Dictamen]}")
    if len(motivo) < MIN_TEXTO:
        errores.append("motivo vacío o demasiado corto")
    crudas = args.get("piezas_objetadas") or []
    objetadas: list[int] = []
    if not isinstance(crudas, list):
        errores.append("piezas_objetadas tiene que ser una lista de números")
    else:
        for x in crudas:
            try:
                objetadas.append(int(x))
            except (TypeError, ValueError):
                errores.append(f"piezas_objetadas: {x!r} no es un número de pieza")
        fuera = sorted(set(objetadas) - citadas)
        if fuera:
            errores.append(f"piezas_objetadas {fuera} no están entre las citadas {sorted(citadas)}")
    return (dictamen, motivo, sorted(set(objetadas))), errores


# ----- lo guardado -----


def cargar_expediente(conn: Connection, veredicto_id: str) -> list[Pieza]:
    """El expediente de un veredicto, en el orden en que lo vio el equipo."""
    filas = conn.execute(
        text("""
            SELECT recolector, contenido, estado, consulta, url, titulo, fecha_publicacion, ampliada
            FROM verificacion.consultas WHERE veredicto_id = :v ORDER BY orden
        """),
        {"v": veredicto_id},
    ).mappings()
    return [
        Pieza(
            recolector=f["recolector"],
            contenido=f["contenido"],
            estado=f["estado"],
            consulta=f["consulta"],
            url=f["url"],
            titulo=f["titulo"],
            fecha=f["fecha_publicacion"],
            ampliada=f["ampliada"],
        )
        for f in filas
    ]


def cargar_veredicto(conn: Connection, veredicto_id: str) -> VeredictoAuditado:
    """El veredicto como lo ve el juez: sin razonamiento, con las citas por número de pieza."""
    v = conn.execute(
        text("""
            SELECT estado, confianza, fecha_cierre_estimada
            FROM verificacion.veredictos WHERE veredicto_id = :v
        """),
        {"v": veredicto_id},
    ).one()
    evidencias = conn.execute(
        text("""
            SELECT c.orden, e.extracto, e.senal, e.fecha_publicacion
            FROM verificacion.evidencias e
            JOIN verificacion.consultas c USING (pieza_id)
            WHERE e.veredicto_id = :v
            ORDER BY c.orden
        """),
        {"v": veredicto_id},
    ).all()
    return VeredictoAuditado(
        veredicto_id=veredicto_id,
        estado=v.estado,
        confianza=v.confianza,
        fecha_cierre_estimada=v.fecha_cierre_estimada,
        evidencias=[Cita(int(o), x, s, f) for o, x, s, f in evidencias],
    )


def guardar_auditoria(conn: Connection, veredicto_id: str, r: ResultadoAuditoria) -> None:
    """Escribe la auditoría de un veredicto."""
    conn.execute(
        insert(Auditoria.__table__).values(
            auditoria_id=str(uuid.uuid4()),
            veredicto_id=veredicto_id,
            juez=r.juez,
            estado_ciego=r.estado_ciego,
            confianza_ciega=r.confianza_ciega,
            razonamiento_ciego=r.razonamiento_ciego,
            dictamen=r.dictamen,
            motivo=r.motivo,
            piezas_objetadas=r.piezas_objetadas,
            duracion_s=round(r.duracion_s, 1),
        )
    )


def auditar_veredicto(
    engine: Engine, juez: Juez, veredicto_id: str, negocio: Negocio
) -> ResultadoAuditoria:
    """Carga lo guardado, corre al juez fuera de toda transacción y escribe su auditoría."""
    with engine.connect() as conn:
        expediente = cargar_expediente(conn, veredicto_id)
        veredicto = cargar_veredicto(conn, veredicto_id)
    resultado = juez.auditar(negocio, expediente, veredicto)
    with engine.begin() as conn:
        guardar_auditoria(conn, veredicto_id, resultado)
    acuerdo = "=" if resultado.estado_ciego == veredicto.estado else "≠"
    logger.info(
        f"      ⚖  {resultado.dictamen} · a ciegas {resultado.estado_ciego}/"
        f"{resultado.confianza_ciega} ({acuerdo} {veredicto.estado}) · "
        f"objetadas {resultado.piezas_objetadas or '—'} · {resultado.duracion_s / 60:.1f} min"
    )
    logger.info(f"      📝 juez a ciegas: {resultado.razonamiento_ciego}")
    logger.info(f"      📝 juez dictamen: {resultado.motivo}")
    return resultado


def pendientes_auditoria(
    conn: Connection, muestra_id: str, juez: str, agente: str | None = None
) -> list[tuple[str, str]]:
    """Veredictos de la muestra con expediente y sin auditoría de ESTE juez, en orden.

    Los veredictos v1-v3 no tienen expediente (buscaban con herramientas y no
    guardaban lo visto), así que no se pueden auditar y no entran.
    """
    return [
        (veredicto_id, clee)
        for veredicto_id, clee in conn.execute(
            text("""
                SELECT v.veredicto_id, v.clee
                FROM verificacion.veredictos v
                JOIN verificacion.muestra_negocios mn USING (muestra_id, clee)
                WHERE v.muestra_id = :id
                  AND (CAST(:agente AS text) IS NULL OR v.agente = CAST(:agente AS text))
                  AND EXISTS (SELECT 1 FROM verificacion.consultas c
                              WHERE c.veredicto_id = v.veredicto_id)
                  AND NOT EXISTS (SELECT 1 FROM verificacion.auditorias a
                                  WHERE a.veredicto_id = v.veredicto_id AND a.juez = :juez)
                ORDER BY mn.orden, v.verificado_en
            """),
            {"id": muestra_id, "juez": juez, "agente": agente},
        ).all()
    ]


def auditar_muestra(
    engine: Engine,
    tracker: MetadataTracker,
    juez: Juez,
    nombre: str,
    *,
    agente: str | None = None,
    limite: int | None = None,
) -> dict[str, int]:
    """Audita los veredictos pendientes de una muestra y registra la corrida en `meta`.

    Reanudable: los pendientes son los que ESTE juez no ha auditado. Un
    veredicto que falla se registra y se sigue; si Ollama se cae, se detiene.

    Args:
        engine: engine con permisos de escritura en `verificacion`.
        tracker: registro de `meta`.
        juez: el juez ya armado.
        nombre: la muestra.
        agente: auditar solo los veredictos de este `modelo/version`.
        limite: cuántos auditar como máximo.

    Returns:
        dict[str, int]: auditados, fallidos, por dictamen y acuerdos a ciegas.
    """
    with engine.connect() as conn:
        muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
        cola = pendientes_auditoria(conn, muestra_id, juez.juez, agente)
    if limite is not None:
        cola = cola[:limite]

    run_id = tracker.create_run(
        job_name=JOB_NAME_AUDITAR,
        job_type="VERIFICACION",
        trigger_type=TriggerType.MANUAL.value,
        config={"muestra": nombre, "juez": juez.juez, "agente": agente, "pendientes": len(cola)},
    )
    task_id = tracker.create_process_task(
        run_id=run_id,
        task_name=nombre,
        source_path="verificacion.veredictos + consultas + evidencias",
        target_table="verificacion.auditorias",
    )
    tracker.start_process_task(task_id)
    logger.info(f"   ▶ juez {juez.juez} sobre {nombre!r}: {len(cola)} veredictos pendientes")

    conteos = {"auditados": 0, "fallidos": 0, "acuerdo": 0}
    conteos.update({d.value: 0 for d in Dictamen})
    try:
        for i, (veredicto_id, clee) in enumerate(cola, start=1):
            with engine.connect() as conn:
                negocio = insumo.cargar(conn, muestra_id, clee)
                estado = conn.execute(
                    text("SELECT estado FROM verificacion.veredictos WHERE veredicto_id = :v"),
                    {"v": veredicto_id},
                ).scalar_one()
            logger.info(f"   [{i}/{len(cola)}] {clee} · {negocio.nombre!r} · {estado}")
            try:
                r = auditar_veredicto(engine, juez, veredicto_id, negocio)
            except OllamaNoDisponible:
                raise
            except Exception as e:  # noqa: BLE001  (un veredicto no tumba la auditoria)
                conteos["fallidos"] += 1
                logger.error(f"      ✖ {clee}: {e.__class__.__name__}: {e}")
                continue
            conteos["auditados"] += 1
            conteos[r.dictamen] += 1
            conteos["acuerdo"] += int(r.estado_ciego == estado)
    except Exception as e:
        tracker.complete_process_task(
            task_id, TaskStatus.FAILED, rows_loaded=conteos["auditados"], error_message=str(e)
        )
        tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
        raise

    tracker.complete_process_task(
        task_id,
        TaskStatus.SUCCESS if not conteos["fallidos"] else TaskStatus.FAILED,
        rows_loaded=conteos["auditados"],
        rows_rejected=conteos["fallidos"] or None,
        error_message=f"{conteos['fallidos']} veredictos fallaron" if conteos["fallidos"] else None,
    )
    tracker.complete_run(
        run_id,
        JobStatus.SUCCESS
        if not conteos["fallidos"]
        else (JobStatus.PARTIAL if conteos["auditados"] else JobStatus.FAILED),
    )
    logger.info(f"   ✅ {conteos}")
    return conteos
