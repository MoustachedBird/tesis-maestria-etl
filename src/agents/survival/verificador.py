"""El bucle del agente: un negocio entra, un veredicto con evidencias sale.

Es un bucle propio de ~150 líneas y no un framework, como manda
`crear_agentes.md` §9.5 para arrancar: tres herramientas no justifican una
dependencia, y así cada vuelta queda legible en el log. Si el bucle se
vuelve el cuello, se cambia este módulo y nada más.

Lo que el bucle garantiza, porque el modelo por sí solo no lo garantiza:

- **Ciego a la hipótesis.** `insumo.py` no carga `cerrado_gold`; aquí no se
  toca. El veredicto se compara después, en `metricas.py`.
- **Ninguna URL inventada.** Una evidencia solo puede citar una URL que
  apareció en una búsqueda o que el modelo leyó. Es el modo de fallo más
  barato de un LLM y el que más daño haría a la auditoría.
- **Vocabulario cerrado.** `estado`, `confianza` y `senal` se validan contra
  los enums antes de escribir; si el modelo se sale, se le devuelve el error
  y vuelve a intentar, no se corrige por él.
- **Presupuesto.** Topes de búsquedas, páginas y vueltas. Agotado uno, la
  herramienta responde que se agotó y pide concluir. Si aun así no concluye,
  el veredicto es `indeterminado` con un razonamiento que lo dice: eso
  también es un dato sobre el agente.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from sqlalchemy import insert, text
from sqlalchemy.engine import Connection, Engine

from src.agents.survival import insumo
from src.agents.survival.prompts import HERRAMIENTAS, SISTEMA, formatear_insumo
from src.agents.survival.sources.busqueda import Buscador, clasificar_fuente
from src.meta.tracker import MetadataTracker
from src.shared.db.models.verificacion import Evidencia, Veredicto
from src.shared.enums import (
    Confianza,
    EstadoVerificado,
    JobStatus,
    SenalEvidencia,
    TaskStatus,
    TriggerType,
)
from src.shared.llm import OllamaNoDisponible
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/agente.log"))

JOB_NAME_VERIFICAR = "agente_supervivencia_verificar"
"""Nombre de la corrida de verificación en `meta.job_runs`."""

_MIN_RAZONAMIENTO = 20
"""Caracteres mínimos del razonamiento: menos no es un porqué."""


@dataclass
class EvidenciaDecidida:
    """Una pieza que el modelo citó, ya validada."""

    url: str
    extracto: str
    senal: str
    fuente: str
    titulo: str | None = None
    fecha_publicacion: date | None = None


@dataclass
class Resultado:
    """Lo que sale del bucle para un negocio: el veredicto y su costo."""

    muestra_id: str
    clee: str
    estado: str
    confianza: str
    razonamiento: str
    agente: str
    fecha_cierre_estimada: date | None = None
    evidencias: list[EvidenciaDecidida] = field(default_factory=list)
    n_busquedas: int = 0
    n_paginas: int = 0
    vueltas: int = 0
    duracion_s: float = 0.0


class Verificador:
    """El agente sobre un modelo y unas fuentes; `verificar()` por negocio."""

    def __init__(
        self,
        cliente: Any,
        buscador: Buscador,
        leer_pagina: Callable[[str], str],
        *,
        version: str,
        max_busquedas: int,
        max_paginas: int,
        max_vueltas: int,
    ):
        """
        Args:
            cliente: `OllamaCliente` o cualquier objeto con `.modelo` y
                `.chat(mensajes, herramientas)`.
            buscador: de dónde salen los resultados web.
            leer_pagina: función `url -> texto`.
            version: `AGENTE_VERSION`; va en `veredictos.agente` junto al modelo.
            max_busquedas: tope de `buscar_web` por negocio.
            max_paginas: tope de `leer_pagina` por negocio.
            max_vueltas: tope de llamadas al modelo por negocio.
        """
        self.cliente = cliente
        self.buscador = buscador
        self.leer_pagina = leer_pagina
        self.agente = f"{cliente.modelo}/{version}"
        self.max_busquedas = max_busquedas
        self.max_paginas = max_paginas
        self.max_vueltas = max_vueltas

    # ----- el bucle -----

    def verificar(self, negocio: insumo.Negocio) -> Resultado:
        """Busca el negocio en línea hasta que el modelo concluye o se agota el presupuesto."""
        inicio = time.monotonic()
        mensajes: list[dict] = [
            {"role": "system", "content": SISTEMA},
            {"role": "user", "content": formatear_insumo(negocio)},
        ]
        vistas: set[str] = set()
        n_busquedas = n_paginas = 0

        for vuelta in range(1, self.max_vueltas + 1):
            respuesta = self.cliente.chat(mensajes, HERRAMIENTAS)
            mensajes.append({k: v for k, v in respuesta.items() if not k.startswith("_")})
            llamadas = respuesta.get("tool_calls") or []
            if not llamadas:
                # Respondio en prosa: el protocolo exige terminar por `concluir`.
                mensajes.append(
                    {
                        "role": "user",
                        "content": "Responde únicamente llamando a la herramienta `concluir`.",
                    }
                )
                continue

            for llamada in llamadas:
                nombre, args = _desempacar(llamada)
                if nombre == "concluir":
                    conclusion, errores = self._validar_conclusion(args, vistas)
                    if errores:
                        mensajes.append(_tool("concluir", "RECHAZADO:\n- " + "\n- ".join(errores)))
                        continue
                    return self._resultado(
                        negocio, conclusion, n_busquedas, n_paginas, vuelta, inicio
                    )
                if nombre == "buscar_web":
                    if n_busquedas >= self.max_busquedas:
                        mensajes.append(
                            _tool(
                                nombre,
                                "PRESUPUESTO AGOTADO: no hay más búsquedas. Concluye con lo que tienes.",
                            )
                        )
                        continue
                    n_busquedas += 1
                    consulta = str(args.get("consulta", "")).strip()
                    try:
                        resultados = self.buscador.buscar(consulta)
                    except Exception as e:  # noqa: BLE001  (el buscador falla, el veredicto sigue)
                        mensajes.append(
                            _tool(nombre, f"ERROR del buscador: {e.__class__.__name__}: {e}")
                        )
                        continue
                    vistas.update(_normalizar(r.url) for r in resultados)
                    texto = "\n".join(r.como_texto() for r in resultados) or "(sin resultados)"
                    logger.info(f"      🔎 {consulta!r}: {len(resultados)} resultados")
                    mensajes.append(_tool(nombre, texto))
                    continue
                if nombre == "leer_pagina":
                    if n_paginas >= self.max_paginas:
                        mensajes.append(
                            _tool(
                                nombre,
                                "PRESUPUESTO AGOTADO: no hay más lecturas. Concluye con lo que tienes.",
                            )
                        )
                        continue
                    n_paginas += 1
                    url = str(args.get("url", "")).strip()
                    texto = self.leer_pagina(url)
                    if not texto.startswith("ERROR"):
                        vistas.add(_normalizar(url))
                    logger.info(f"      📄 {url} ({len(texto)} caracteres)")
                    mensajes.append(_tool(nombre, texto))
                    continue
                mensajes.append(_tool(nombre, f"ERROR: no existe la herramienta {nombre!r}"))

        # Se acabaron las vueltas sin concluir: una ultima oportunidad, y si
        # no, el veredicto dice que el agente no concluyo. Eso tambien es dato.
        mensajes.append(
            {
                "role": "user",
                "content": "Agotaste las vueltas. Llama a `concluir` AHORA con lo que tengas.",
            }
        )
        respuesta = self.cliente.chat(mensajes, HERRAMIENTAS)
        for llamada in respuesta.get("tool_calls") or []:
            nombre, args = _desempacar(llamada)
            if nombre == "concluir":
                conclusion, errores = self._validar_conclusion(args, vistas)
                if not errores:
                    return self._resultado(
                        negocio, conclusion, n_busquedas, n_paginas, self.max_vueltas + 1, inicio
                    )
        return Resultado(
            muestra_id=negocio.muestra_id,
            clee=negocio.clee,
            estado=EstadoVerificado.INDETERMINADO.value,
            confianza=Confianza.BAJA.value,
            razonamiento=(
                f"El agente no concluyó dentro del presupuesto ({self.max_vueltas} vueltas, "
                f"{n_busquedas} búsquedas, {n_paginas} páginas). Se registra como indeterminado."
            ),
            agente=self.agente,
            n_busquedas=n_busquedas,
            n_paginas=n_paginas,
            vueltas=self.max_vueltas + 1,
            duracion_s=time.monotonic() - inicio,
        )

    # ----- validacion -----

    def _validar_conclusion(self, args: dict, vistas: set[str]) -> tuple[dict, list[str]]:
        """Revisa la conclusión del modelo contra el vocabulario y las URLs vistas.

        Returns:
            tuple[dict, list[str]]: la conclusión normalizada y los errores;
            con errores, la conclusión no vale.
        """
        errores: list[str] = []
        estado = str(args.get("estado", "")).strip().lower()
        confianza = str(args.get("confianza", "")).strip().lower()
        razonamiento = str(args.get("razonamiento", "")).strip()
        if estado not in {e.value for e in EstadoVerificado}:
            errores.append(f"estado {estado!r} no es uno de {[e.value for e in EstadoVerificado]}")
        if confianza not in {c.value for c in Confianza}:
            errores.append(f"confianza {confianza!r} no es una de {[c.value for c in Confianza]}")
        if len(razonamiento) < _MIN_RAZONAMIENTO:
            errores.append("razonamiento vacío o demasiado corto: di por qué, con fuentes y fechas")

        fecha_cierre, error_fecha = _fecha(args.get("fecha_cierre_estimada"))
        if error_fecha:
            errores.append(f"fecha_cierre_estimada: {error_fecha}")

        evidencias: list[EvidenciaDecidida] = []
        crudas = args.get("evidencias") or []
        if not isinstance(crudas, list):
            errores.append("evidencias tiene que ser una lista")
            crudas = []
        for i, ev in enumerate(crudas, start=1):
            if not isinstance(ev, dict):
                errores.append(f"evidencia {i}: no es un objeto")
                continue
            url = str(ev.get("url", "")).strip()
            extracto = str(ev.get("extracto", "")).strip()
            senal = str(ev.get("senal", "")).strip().lower()
            if _normalizar(url) not in vistas:
                errores.append(
                    f"evidencia {i}: la URL {url!r} no apareció en ninguna búsqueda ni la leíste; "
                    "cita solo URLs que viste"
                )
            if not extracto:
                errores.append(f"evidencia {i}: falta el extracto citado")
            if senal not in {s.value for s in SenalEvidencia}:
                errores.append(
                    f"evidencia {i}: senal {senal!r} no es una de {[s.value for s in SenalEvidencia]}"
                )
            fecha_pub, error_pub = _fecha(ev.get("fecha_publicacion"))
            if error_pub:
                errores.append(f"evidencia {i}: fecha_publicacion: {error_pub}")
            evidencias.append(
                EvidenciaDecidida(
                    url=url,
                    extracto=extracto,
                    senal=senal,
                    fuente=clasificar_fuente(url),
                    titulo=(str(ev["titulo"]).strip() or None) if ev.get("titulo") else None,
                    fecha_publicacion=fecha_pub,
                )
            )
        if (
            estado in (EstadoVerificado.ACTIVO.value, EstadoVerificado.CERRADO.value)
            and not evidencias
        ):
            errores.append(f"un veredicto {estado!r} necesita al menos una evidencia con URL vista")

        return (
            {
                "estado": estado,
                "confianza": confianza,
                "razonamiento": razonamiento,
                "fecha_cierre_estimada": fecha_cierre,
                "evidencias": evidencias,
            },
            errores,
        )

    def _resultado(
        self,
        negocio: insumo.Negocio,
        c: dict,
        n_busquedas: int,
        n_paginas: int,
        vueltas: int,
        inicio: float,
    ) -> Resultado:
        return Resultado(
            muestra_id=negocio.muestra_id,
            clee=negocio.clee,
            estado=c["estado"],
            confianza=c["confianza"],
            razonamiento=c["razonamiento"],
            agente=self.agente,
            fecha_cierre_estimada=c["fecha_cierre_estimada"],
            evidencias=c["evidencias"],
            n_busquedas=n_busquedas,
            n_paginas=n_paginas,
            vueltas=vueltas,
            duracion_s=time.monotonic() - inicio,
        )


# ----- persistencia -----


def guardar(conn: Connection, r: Resultado) -> str:
    """Escribe el veredicto y sus evidencias; devuelve el `veredicto_id`."""
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
            n_busquedas=r.n_busquedas,
            duracion_s=round(r.duracion_s, 1),
        )
    )
    if r.evidencias:
        conn.execute(
            insert(Evidencia.__table__),
            [
                {
                    "evidencia_id": str(uuid.uuid4()),
                    "veredicto_id": veredicto_id,
                    "fuente": e.fuente,
                    "url": e.url,
                    "titulo": e.titulo,
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
    verificador: Verificador,
    nombre: str,
    *,
    limite: int | None = None,
    solo_clee: str | None = None,
) -> dict[str, int]:
    """Verifica los negocios pendientes de una muestra y registra la corrida en `meta`.

    Reanudable por construcción: los pendientes son los que ESTE agente
    (modelo/version) no ha verificado. Un negocio que falla por una
    excepción se registra y se sigue con el siguiente; si Ollama se cae, la
    corrida se detiene, porque todo lo que sigue fallaría igual.

    Args:
        engine: engine con permisos de escritura en `verificacion`.
        tracker: registro de `meta`.
        verificador: el agente ya armado.
        nombre: la muestra.
        limite: cuántos verificar como máximo (para probar con 1 o 10).
        solo_clee: verificar únicamente este negocio.

    Returns:
        dict[str, int]: conteos de la corrida (verificados, fallidos, por estado).
    """
    with engine.connect() as conn:
        muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
        cola = insumo.pendientes(conn, muestra_id, verificador.agente)
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
            "agente": verificador.agente,
            "pendientes": len(cola),
            "limite": limite,
            "max_busquedas": verificador.max_busquedas,
            "max_paginas": verificador.max_paginas,
            "max_vueltas": verificador.max_vueltas,
        },
    )
    task_id = tracker.create_process_task(
        run_id=run_id,
        task_name=nombre,
        source_path="verificacion.muestra_negocios + gold.negocios_clee_oficial + silver.establecimientos",
        target_table="verificacion.veredictos",
    )
    tracker.start_process_task(task_id)
    logger.info(f"   ▶ {verificador.agente} sobre {nombre!r}: {len(cola)} negocios pendientes")

    conteos = {"verificados": 0, "fallidos": 0}
    por_estado = {e.value: 0 for e in EstadoVerificado}
    try:
        for i, (orden, clee) in enumerate(cola, start=1):
            with engine.connect() as conn:
                negocio = insumo.cargar(conn, muestra_id, clee)
            logger.info(
                f"   [{i}/{len(cola)}] #{orden} {clee} · {negocio.nombre!r} · {negocio.municipio}"
            )
            try:
                resultado = verificador.verificar(negocio)
            except OllamaNoDisponible:
                raise
            except Exception as e:  # noqa: BLE001  (un negocio no tumba la muestra)
                conteos["fallidos"] += 1
                logger.error(f"      ✖ {clee}: {e.__class__.__name__}: {e}")
                continue
            with engine.begin() as conn:
                guardar(conn, resultado)
            conteos["verificados"] += 1
            por_estado[resultado.estado] += 1
            logger.info(
                f"      ✔ {resultado.estado}/{resultado.confianza} · {len(resultado.evidencias)} evidencias · "
                f"{resultado.n_busquedas} búsquedas · {resultado.vueltas} vueltas · {resultado.duracion_s / 60:.1f} min"
            )
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
    logger.info(
        f"   ✅ {conteos['verificados']} verificados, {conteos['fallidos']} fallidos · {por_estado}"
    )
    return {**conteos, **por_estado}


def resumen_muestra(conn: Connection, nombre: str, agente: str) -> dict[str, int]:
    """Cuántos negocios tiene la muestra y cuántos ya verificó este agente."""
    muestra_id, _ = insumo.muestra_por_nombre(conn, nombre)
    total = conn.execute(
        text("SELECT count(*) FROM verificacion.muestra_negocios WHERE muestra_id = :id"),
        {"id": muestra_id},
    ).scalar_one()
    return {"total": int(total), "pendientes": len(insumo.pendientes(conn, muestra_id, agente))}


# ----- utilidades -----


def _tool(nombre: str, contenido: str) -> dict:
    """Un mensaje de resultado de herramienta, como lo espera Ollama."""
    return {"role": "tool", "tool_name": nombre, "content": contenido}


def _desempacar(llamada: dict) -> tuple[str, dict]:
    """Nombre y argumentos de una `tool_call`; los argumentos pueden venir como JSON en texto."""
    funcion = llamada.get("function", llamada)
    args = funcion.get("arguments") or {}
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    return str(funcion.get("name", "")), dict(args) if isinstance(args, dict) else {}


def _normalizar(url: str) -> str:
    """Forma canónica para comparar URLs: sin esquema, sin `www.`, sin barra final."""
    u = url.strip().lower()
    for prefijo in ("https://", "http://"):
        u = u.removeprefix(prefijo)
    return u.removeprefix("www.").rstrip("/")


def _fecha(valor: Any) -> tuple[date | None, str | None]:
    """`AAAA-MM-DD` o `AAAA-MM` a `date`; (None, None) si viene vacío; (None, error) si no parsea."""
    if valor in (None, "", "null"):
        return None, None
    texto = str(valor).strip()
    for formato in ("%Y-%m-%d", "%Y-%m"):
        try:
            from datetime import datetime

            return datetime.strptime(texto, formato).date(), None
        except ValueError:
            continue
    return None, f"{texto!r} no es AAAA-MM-DD ni AAAA-MM"
