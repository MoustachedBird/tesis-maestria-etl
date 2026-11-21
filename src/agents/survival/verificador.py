"""La verificación de un negocio de punta a punta, y de una muestra entera.

Por cada negocio, en este orden:

    para cada grupo de fuentes (mapas, buscadores, redes, plataformas,
    huella digital, registros, fachada):
        recolección base en código  ->  piezas del expediente, fuente por fuente
        especialista del grupo      ->  informe (puede ampliar sus fuentes)
    decisor                         ->  veredicto con evidencias
    se guarda todo                  ->  veredictos, consultas, informes, evidencias
    juez                            ->  auditoría (a ciegas + dictamen)

El orden de los grupos importa: redes, plataformas y el archivo web abren
las URLs que encontraron los buscadores, así que van después.

Lo que el sistema garantiza, porque el modelo por sí solo no lo garantiza:

- **Ciego a la hipótesis.** `insumo.py` no carga `cerrado_gold`; aquí no se
  toca. El veredicto se compara después, en `metricas.py`.
- **Mismas fuentes para todos.** La recolección base no la decide ningún
  agente; lo que pidan los especialistas queda marcado `ampliada`.
- **Ninguna cita inventada.** Toda cita apunta a una pieza del expediente,
  su extracto está escrito ahí y su fecha también (`citas.py`). La URL de
  una evidencia la pone el código desde la pieza, no el modelo.
- **Vocabulario cerrado.** Estados, confianzas, señales y dictámenes se
  validan contra los enums antes de escribir; la base tiene el mismo CHECK.
- **Todo lo visto se guarda.** El expediente completo va a
  `verificacion.consultas`, citado o no: es contra lo que audita el juez.
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
from src.agents.survival.equipo import Decisor, Especialista, Informe, Planeacion, Planificador
from src.agents.survival.juez import Juez, auditar_veredicto
from src.agents.survival.recoleccion import Fuente, Pieza, agrupar
from src.meta.tracker import MetadataTracker
from src.shared.db.models.verificacion import Consulta, Evidencia, Veredicto
from src.shared.db.models.verificacion import Informe as InformeFila
from src.shared.enums import Dictamen, EstadoVerificado, JobStatus, TaskStatus, TriggerType
from src.shared.llm import OllamaNoDisponible
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

JOB_NAME_VERIFICAR = "agente_supervivencia_verificar"
"""Nombre de la corrida de verificación en `meta.job_runs`."""

_ICONOS = {
    "maps": "🗺 ",
    "web": "🔎",
    "redes": "👥",
    "plataformas": "⭐",
    "sitio": "🌐",
    "registros": "🏛 ",
    "fachada": "📷",
}
"""Un ícono por especialista en el log, para leerlo de un vistazo."""


@dataclass
class Resultado:
    """Lo que sale del equipo para un negocio: el veredicto, todo lo visto y su costo."""

    muestra_id: str
    clee: str
    estado: str
    confianza: str
    razonamiento: str
    agente: str
    fecha_cierre_estimada: date | None = None
    evidencias: list[Cita] = field(default_factory=list)
    expediente: list[Pieza] = field(default_factory=list)
    informes: list[Informe] = field(default_factory=list)
    planeacion: Planeacion | None = None
    """Con qué nombres se buscó y por qué (el planificador)."""
    duracion_s: float = 0.0

    @property
    def n_consultas(self) -> int:
        """Cuántas consultas costó: búsquedas, fichas de Maps y páginas leídas."""
        return len({(p.recolector, p.consulta or p.url) for p in self.expediente})


class Equipo:
    """El verificador: las fuentes, un especialista por grupo y el decisor."""

    def __init__(
        self,
        cliente: Any,
        fuentes: list[Fuente],
        *,
        version: str,
        max_ampliaciones: int,
        max_intentos: int,
        presupuesto: int | None = None,
        max_nombres: int = 3,
    ):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.modelo` y `.chat(...)`.
            fuentes: las que se consultan; `agrupar` las ordena por especialista.
            version: `AGENTE_VERSION`; va en `veredictos.agente` junto al modelo.
            max_ampliaciones: consultas extra por especialista.
            max_intentos: vueltas por rol para producir una salida válida.
            presupuesto: caracteres de piezas que caben en el contexto de un
                especialista (`recoleccion.presupuesto_de`); None = sin tope.
            max_nombres: cuántos nombres de búsqueda puede elegir el planificador.
        """
        self.fuentes = fuentes
        self.grupos = agrupar(fuentes)
        self.agente = f"{cliente.modelo}/{version}"
        self.especialista = Especialista(
            cliente,
            max_ampliaciones=max_ampliaciones,
            max_intentos=max_intentos,
            presupuesto=presupuesto,
        )
        self.decisor = Decisor(cliente, max_intentos=max_intentos)
        self.planificador = Planificador(
            cliente, max_nombres=max_nombres, max_intentos=max_intentos
        )

    def verificar(self, n: insumo.Negocio) -> Resultado:
        """Recolecta, informa por fuente y decide."""
        inicio = time.monotonic()
        logger.info("   ── 0 🏷  planificador: con qué nombres buscar")
        planeacion = self.planificador.elegir(n)
        n.nombres_busqueda = planeacion.nombres
        marca = " (automático)" if planeacion.automatica else ""
        logger.info(
            f"      🏷  nombres: {' | '.join(planeacion.nombres) or '—'}{marca} · "
            f"{planeacion.razonamiento[:200]}"
        )
        expediente: list[Pieza] = []
        informes: list[Informe] = []
        for i, grupo in enumerate(self.grupos, start=1):
            logger.info(
                f"   ── {i}/{len(self.grupos)} {_ICONOS.get(grupo.especialidad, '·')} "
                f"{grupo.especialidad}: {', '.join(f.recolector for f in grupo.fuentes)}"
            )
            t0 = time.monotonic()
            expediente.extend(grupo.recolectar(n, expediente))
            t_recoleccion = time.monotonic() - t0
            informe = self.especialista.informar(n, grupo, expediente)
            informes.append(informe)
            propias = [p for p in expediente if p.recolector in grupo.recolectores]
            por_fuente = ", ".join(
                f"{r} {sum(p.citable for p in propias if p.recolector == r)}/"
                f"{sum(p.recolector == r for p in propias)}"
                for r in (f.recolector for f in grupo.fuentes)
            )
            marca = " (automático)" if informe.automatico else ""
            logger.info(
                f"      {_ICONOS.get(grupo.especialidad, '·')} {grupo.especialidad} [{por_fuente}] · "
                f"{informe.identificado}/{informe.senal} · {len(informe.citas)} citas{marca} · "
                f"recolección {t_recoleccion:.0f}s, modelo {informe.duracion_s:.0f}s"
            )
        decision = self.decisor.decidir(n, informes, expediente)
        return Resultado(
            muestra_id=n.muestra_id,
            clee=n.clee,
            estado=decision.estado,
            confianza=decision.confianza,
            razonamiento=decision.razonamiento,
            agente=self.agente,
            fecha_cierre_estimada=decision.fecha_cierre_estimada,
            evidencias=decision.evidencias,
            expediente=expediente,
            informes=informes,
            planeacion=planeacion,
            duracion_s=time.monotonic() - inicio,
        )


# ----- persistencia -----


def guardar(conn: Connection, r: Resultado) -> str:
    """Escribe el veredicto, su expediente, los informes y las evidencias; devuelve el `veredicto_id`.

    En el orden de las FK: el veredicto, el expediente (las evidencias
    apuntan a sus piezas), los informes y las evidencias.
    """
    veredicto_id = str(uuid.uuid4())
    conn.execute(
        insert(Veredicto.__table__).values(
            veredicto_id=veredicto_id,
            muestra_id=r.muestra_id,
            clee=r.clee,
            estado=r.estado,
            confianza=r.confianza,
            fecha_cierre_estimada=r.fecha_cierre_estimada,
            razonamiento=r.razonamiento,
            agente=r.agente,
            n_busquedas=r.n_consultas,
            duracion_s=round(r.duracion_s, 1),
            nombres_buscados=r.planeacion.como_dict() if r.planeacion else None,
        )
    )
    if r.planeacion is not None and r.planeacion.nombres:
        conn.execute(
            text("""
                UPDATE verificacion.muestra_negocios
                SET nombres_elegidos = :nombres, nombres_elegidos_por = :agente
                WHERE muestra_id = :id AND clee = :clee
            """),
            {
                "nombres": r.planeacion.nombres,
                "agente": r.agente,
                "id": r.muestra_id,
                "clee": r.clee,
            },
        )
    ids = [str(uuid.uuid4()) for _ in r.expediente]
    if r.expediente:
        conn.execute(
            insert(Consulta.__table__),
            [
                {
                    "pieza_id": ids[i],
                    "veredicto_id": veredicto_id,
                    "orden": i + 1,
                    "recolector": p.recolector,
                    "fuente": p.fuente,
                    "consulta": p.consulta,
                    "url": p.url,
                    "titulo": p.titulo,
                    "fecha_publicacion": p.fecha,
                    "contenido": p.contenido,
                    "estado": p.estado,
                    "ampliada": p.ampliada,
                }
                for i, p in enumerate(r.expediente)
            ],
        )
    if r.informes:
        conn.execute(
            insert(InformeFila.__table__),
            [
                {
                    "informe_id": str(uuid.uuid4()),
                    "veredicto_id": veredicto_id,
                    "especialista": i.especialista,
                    "identificado": i.identificado,
                    "senal": i.senal,
                    "fecha_mas_reciente": i.fecha_mas_reciente,
                    "resumen": i.resumen,
                    "citas": [c.como_dict() for c in i.citas],
                    "automatico": i.automatico,
                    "duracion_s": round(i.duracion_s, 1),
                }
                for i in r.informes
            ],
        )
    if r.evidencias:
        conn.execute(
            insert(Evidencia.__table__),
            [
                {
                    "evidencia_id": str(uuid.uuid4()),
                    "veredicto_id": veredicto_id,
                    "pieza_id": ids[e.pieza - 1],
                    "fuente": r.expediente[e.pieza - 1].fuente,
                    "url": r.expediente[e.pieza - 1].url,
                    "titulo": r.expediente[e.pieza - 1].titulo,
                    "fecha_publicacion": e.fecha_publicacion,
                    "extracto": e.extracto,
                    "senal": e.senal,
                }
                for e in r.evidencias
            ],
        )
    return veredicto_id


def verificar_muestra(
    engine: Engine,
    tracker: MetadataTracker,
    equipo: Any,
    nombre: str,
    *,
    limite: int | None = None,
    solo_clee: str | None = None,
    juez: Juez | None = None,
) -> dict[str, int]:
    """Verifica los negocios pendientes de una muestra y registra la corrida en `meta`.

    Reanudable por construcción: los pendientes son los que ESTE equipo
    (modelo/version) no ha verificado. Un negocio que falla se registra y se
    sigue con el siguiente; si Ollama se cae, la corrida se detiene, porque
    todo lo que sigue fallaría igual. Si el juez falla en un negocio, el
    veredicto se queda y la auditoría se repite después con `auditar`.

    Args:
        engine: engine con permisos de escritura en `verificacion`.
        tracker: registro de `meta`.
        equipo: `Equipo`, o cualquier objeto con `.agente` y `.verificar(negocio)`.
        nombre: la muestra.
        limite: cuántos verificar como máximo (para probar con 1 o 10).
        solo_clee: verificar únicamente este negocio.
        juez: si se pasa, audita cada veredicto en cuanto se guarda.

    Returns:
        dict[str, int]: conteos de la corrida (verificados, fallidos, por
        estado y, con juez, los de la auditoría).
    """
    with engine.connect() as conn:
        muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
        cola = insumo.pendientes(conn, muestra_id, equipo.agente)
    if solo_clee:
        cola = [(o, c) for o, c in cola if c == solo_clee]
    if limite is not None:
        cola = cola[:limite]

    run_id = tracker.create_run(
        job_name=JOB_NAME_VERIFICAR,
        job_type="VERIFICACION",
        trigger_type=TriggerType.MANUAL.value,
        config={
            "muestra": nombre,
            "agente": equipo.agente,
            "juez": juez.juez if juez else None,
            "fuentes": [f.recolector for f in getattr(equipo, "fuentes", [])],
            "pendientes": len(cola),
            "limite": limite,
        },
    )
    task_id = tracker.create_process_task(
        run_id=run_id,
        task_name=nombre,
        source_path="verificacion.muestra_negocios + gold.negocios_clee_oficial + silver.establecimientos",
        target_table="verificacion.veredictos",
    )
    tracker.start_process_task(task_id)
    logger.info(f"   ▶ {equipo.agente} sobre {nombre!r}: {len(cola)} negocios pendientes")

    conteos = {"verificados": 0, "fallidos": 0}
    conteos.update({e.value: 0 for e in EstadoVerificado})
    if juez:
        conteos.update({"auditados": 0, "sostenidos": 0, "acuerdo": 0, "auditorias_fallidas": 0})
    try:
        for i, (orden, clee) in enumerate(cola, start=1):
            with engine.connect() as conn:
                negocio = insumo.cargar(conn, muestra_id, clee)
            logger.info(
                f"   [{i}/{len(cola)}] #{orden} {clee} · {negocio.nombre!r} · {negocio.municipio}"
            )
            try:
                resultado = equipo.verificar(negocio)
            except OllamaNoDisponible:
                raise
            except Exception as e:  # noqa: BLE001  (un negocio no tumba la muestra)
                conteos["fallidos"] += 1
                logger.error(f"      ✖ {clee}: {e.__class__.__name__}: {e}")
                continue
            try:
                with engine.begin() as conn:
                    veredicto_id = guardar(conn, resultado)
            except Exception as e:  # noqa: BLE001  (un negocio que no se pudo guardar no tumba la muestra)
                conteos["fallidos"] += 1
                logger.error(
                    f"      ✖ {clee}: no se pudo guardar: {e.__class__.__name__}: {str(e)[:300]}"
                )
                continue
            conteos["verificados"] += 1
            conteos[resultado.estado] += 1
            logger.info(
                f"      ✔ {resultado.estado}/{resultado.confianza} · "
                f"{len(resultado.evidencias)} evidencias · {len(resultado.expediente)} piezas · "
                f"{resultado.duracion_s / 60:.1f} min"
            )
            if juez is None:
                continue
            logger.info(f"   ── ⚖  juez {juez.juez}: audita el veredicto")
            try:
                a = auditar_veredicto(engine, juez, veredicto_id, negocio)
            except OllamaNoDisponible:
                raise
            except Exception as e:  # noqa: BLE001  (el veredicto queda; se audita despues)
                conteos["auditorias_fallidas"] += 1
                logger.error(f"      ✖ juez en {clee}: {e.__class__.__name__}: {e}")
                continue
            conteos["auditados"] += 1
            conteos["sostenidos"] += int(a.dictamen == Dictamen.SOSTENIDO.value)
            conteos["acuerdo"] += int(a.estado_ciego == resultado.estado)
    except Exception as e:
        tracker.complete_process_task(
            task_id, TaskStatus.FAILED, rows_loaded=conteos["verificados"], error_message=str(e)
        )
        tracker.complete_run(run_id, JobStatus.FAILED, error_summary=str(e))
        raise

    estado = TaskStatus.SUCCESS if not conteos["fallidos"] else TaskStatus.FAILED
    tracker.complete_process_task(
        task_id,
        estado,
        rows_loaded=conteos["verificados"],
        rows_rejected=conteos["fallidos"] or None,
        error_message=f"{conteos['fallidos']} negocios fallaron" if conteos["fallidos"] else None,
    )
    tracker.complete_run(
        run_id,
        JobStatus.SUCCESS
        if not conteos["fallidos"]
        else (JobStatus.PARTIAL if conteos["verificados"] else JobStatus.FAILED),
    )
    logger.info(f"   ✅ {conteos}")
    return conteos


def resumen_muestra(conn: Connection, nombre: str, agente: str) -> dict[str, int]:
    """Cuántos negocios tiene la muestra y cuántos ya verificó este equipo."""
    muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
    total = conn.execute(
        text("SELECT count(*) FROM verificacion.muestra_negocios WHERE muestra_id = :id"),
        {"id": muestra_id},
    ).scalar_one()
    return {"total": int(total), "pendientes": len(insumo.pendientes(conn, muestra_id, agente))}
