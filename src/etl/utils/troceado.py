"""Maquinaria para construir un agregado por identidad, troceado y reanudable.

Existe porque dos tablas la necesitan idéntica y ninguna de las dos la
necesita distinta:

    silver.permanencia_<metodo>   una fila por negocio, ventana de 20 cortes
    gold.negocios_clee_oficial    una fila por negocio, ventana oficial

Las dos agregan 82.9M observaciones a una fila por identidad, y las dos
tienen el mismo problema: el `GROUP BY` completo no cabe en memoria, una
corrida de horas no puede perderse si muere a la mitad, y desde afuera no
hay forma de saber si sigue viva. Lo que **no** comparten es el SQL: los
agregados que calculan son distintos y Gold además materializa valores
canónicos que Silver no tiene (`b0007`).

Así que aquí vive la forma y allá el contenido:

    AQUI        el troceo en dos fases, el reanudar, el latido, la
                transacción por vuelta, el registro en `meta`, el reloj de
                frescura contra la identidad
    EL DESTINO  qué columnas lleva la tabla de trabajo, cómo se llena, y
                qué pasos producen sus filas

**Está en `utils/` y no en `loaders/` porque no carga nada**: es la máquina
que los loaders usan. Y el que una la usen Silver y otra Gold no las acopla
--ninguna de las dos importa a la otra, las dos importan esto-- igual que
`matching.py` lo usan varios transformers sin volverlos uno. Lo compartido
es plomería; el SQL de cada capa sigue siendo suyo.

Si algún día un destino necesita que la máquina se doble --otra forma de
tabla de trabajo, un paso que no es SQL-- lo correcto es partirla, no
agregarle banderas: una abstracción con excepciones cuesta más que dos
archivos independientes.

El troceo va en dos fases porque cada una admite una llave distinta:

1. **Por corte** se llena la tabla de trabajo, que une los hechos con la
   identidad. Ahí el grano es la observación —cada fila pertenece a un solo
   corte— así que partir por corte no parte nada, y cada vuelta lee una sola
   partición de los hechos y una de la identidad.
2. **Por bloque de identidad** se agrega y se escribe. Ahí el grano es el
   establecimiento, que **cruza** los cortes: trocear por año daría
   agregados calculados sobre un fragmento de su vida.

**Cada vuelta es su propia transacción y commitea.** Eso es el punto: un
fallo en la vuelta 14 no se lleva las 13 anteriores. El precio es que se
pierde el reemplazo todo-o-nada —mientras corre, la tabla destino está
parcialmente llena— y se acepta porque son tablas derivadas que solo lee el
análisis entre corridas.
"""

from __future__ import annotations

import logging
import threading
import time
from abc import ABC, abstractmethod
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import NamedTuple

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.meta.tracker import MetadataTracker
from src.shared.config import get_settings
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.partitions import existing_partitions
from src.shared.enums import JobStatus, TaskStatus, TriggerType

SET_WORK_MEM = "SET LOCAL work_mem = '4GB'"
"""Memoria de trabajo para esta consulta y solo para ella.

El agregado produce ~10M grupos, cada uno con una decena de `COUNT(DISTINCT
...)` que mantienen su propio conjunto mientras dura el grupo. Con el
`work_mem` global (256 MB, modesto a propósito porque es POR NODO y POR
CONEXIÓN) el hash aggregate se derrama a disco y tarda mucho más. Subirlo
por sesión en las cargas masivas es el patrón que documenta `.env.example`.

**`SET LOCAL`, no `SET`**: muere con la transacción de la vuelta, así que no
contamina la siguiente ni deja la conexión del pool con 4 GB de presupuesto.
"""

_SQL_LATIDO = """
SELECT a.state,
       a.wait_event_type,
       a.wait_event,
       EXTRACT(EPOCH FROM (now() - a.query_start)) AS segundos,
       d.temp_bytes
FROM pg_stat_activity a
JOIN pg_stat_database d ON d.datname = a.datname
WHERE a.pid = :pid
"""
"""Qué está haciendo el backend que ejecuta la sentencia.

`wait_event` es el dato útil: dice si el servidor está leyendo los hechos
(`DataFileRead`), derramando el agregado a disco (`BufFileWrite`) o
esperando un lock. `temp_bytes` es de la base entera y no de la consulta
--PostgreSQL no lo expone por sentencia-- así que sirve como proxy del
derrame mientras nada más escriba temporales.

**No se puede reportar "filas escritas hasta ahora":** la sentencia vive en
una transacción sin commitear, así que otra conexión ve la tabla destino
vacía hasta el final. De ahí que el progreso se mida por fase y no por
avance.
"""


class Paso(NamedTuple):
    """Un paso del llenado, con qué decir de él mientras corre.

    La descripción no es adorno: es lo que el log muestra durante las horas
    que el paso tarda, y lo último que queda escrito si el proceso muere.

    Attributes:
        nombre: tabla temporal que produce (o la tabla destino, en el
            último), para identificarlo en el log y en `pg_stat_activity`.
        descripcion: qué está haciendo, en una línea.
        sql: la sentencia; `CREATE TEMP TABLE ... AS` salvo el `INSERT`.
    """

    nombre: str
    descripcion: str
    sql: str


def expr_bloque(columna: str, bloques: int) -> str:
    """Expresión SQL que asigna una identidad a un bloque.

    Es el primer byte del `md5` de la identidad, módulo el número de
    bloques. Dos propiedades la hacen la llave correcta para trocear:

    - **Es función de la identidad**, así que todas las observaciones de un
      establecimiento caen en el mismo bloque y ningún agregado se parte.
      Trocear por año o por `cve_ent` no cumple esto: la identidad cruza los
      cortes, y los negocios se mudan de municipio --que es justo una de las
      señales que estas tablas miden.
    - **Es estable entre versiones de PostgreSQL.** `hashtext()` sería más
      directo pero es una función interna sin garantía de estabilidad; si
      cambiara en un upgrade mayor, una reanudación reasignaría los bloques
      y recalcularía trabajo ya hecho. `md5`, `decode` y `get_byte` están
      documentadas y son inmutables.

    Con `bloques` divisor de 256 el reparto es exactamente uniforme; con
    cualquier otro valor queda un sesgo de a lo más un bloque.

    Args:
        columna: expresión que da la identidad.
        bloques: cuántos bloques repartir.

    Returns:
        str: expresión entera en `[0, bloques)`.
    """
    return f"(get_byte(decode(md5({columna}), 'hex'), 0) % {bloques})"


def filas(conn: Connection, tabla: str, schema: str) -> int:
    """Cuántas filas tiene una tabla del schema.

    Args:
        conn: conexión de lectura.
        tabla: nombre sin calificar.
        schema: schema de la tabla.

    Returns:
        int: conteo exacto.
    """
    return conn.execute(text(f"SELECT count(*) FROM {schema}.{tabla}")).scalar_one()


def cortes_ordenados(conn: Connection, schema: str = DEFAULT_SCHEMA) -> list[date]:
    """Los cortes presentes en Silver, del más antiguo al más nuevo.

    Salen del catálogo de PostgreSQL --cada corte ES una partición de
    `establecimientos` y su nombre lleva el `YYYY_MM`-- y no de un
    `SELECT DISTINCT period`, que recorrería 82.9M filas para enterarse de
    veinte fechas.

    Args:
        conn: conexión de lectura.
        schema: schema de la tabla de hechos.

    Returns:
        list[date]: cortes ordenados.
    """
    from src.shared.db.partitions import PARTITIONED_TABLE

    prefijo = len(f"{PARTITIONED_TABLE}_")
    cortes = []
    for nombre in existing_partitions(conn, schema=schema):
        ano, mes = nombre[prefijo:].split("_")
        cortes.append(date(int(ano), int(mes), 1))
    return sorted(cortes)


def _vigilar(
    pid: int,
    destino: str,
    intervalo: int,
    detener: threading.Event,
    log: logging.Logger,
) -> None:
    """Emite un latido cada `intervalo` segundos hasta que se le pida parar.

    Corre en su propio hilo y con su propia conexión: la de la sentencia
    está bloqueada mientras el servidor trabaja, así que preguntarle a ella
    es imposible por construcción.

    Un fallo del monitoreo no debe tumbar la carga: si la consulta de
    diagnóstico truena, lo registra y sigue intentando en el siguiente
    ciclo.

    Args:
        pid: backend de PostgreSQL que ejecuta la sentencia.
        destino: qué se está llenando, para el mensaje.
        intervalo: segundos entre latidos.
        detener: se activa cuando la sentencia terminó.
        log: logger del stage que está corriendo.
    """
    from src.shared.db.session import get_engine

    engine = get_engine()
    inicio = time.monotonic()
    temp_inicial: int | None = None

    while not detener.wait(intervalo):
        try:
            with engine.connect() as vigia:
                fila = vigia.execute(text(_SQL_LATIDO), {"pid": pid}).mappings().first()
        except Exception as e:  # noqa: BLE001  (el monitoreo no tumba la carga)
            log.warning(f"   latido no disponible: {e}")
            continue

        transcurrido = time.monotonic() - inicio
        if fila is None:
            # El backend ya no existe: o termino (y `detener` esta por
            # activarse) o lo mataron por fuera. Lo segundo importa saberlo.
            log.warning(
                f"   ⏳ {destino}: el backend {pid} ya no aparece en "
                f"pg_stat_activity tras {transcurrido / 60:.1f} min"
            )
            continue

        if temp_inicial is None:
            temp_inicial = fila["temp_bytes"] or 0
        derramado = (fila["temp_bytes"] or 0) - temp_inicial
        espera = (
            f"{fila['wait_event_type']}:{fila['wait_event']}"
            if fila["wait_event_type"]
            else "ejecutando (sin espera)"
        )
        log.info(
            f"   ⏳ {destino}: {transcurrido / 60:.1f} min | estado={fila['state']} "
            f"| {espera} | temp +{derramado / 1024**3:.1f} GiB"
        )


@contextmanager
def latido(conn: Connection, destino: str, log: logging.Logger) -> Generator[None]:
    """Reporta avance mientras el bloque que envuelve ejecuta su sentencia.

    Existe por la misma razón que el latido de `shared/db/copy.py`: una
    escritura masiva que no emite nada es indistinguible de un cuelgue desde
    afuera, y la única forma de saberlo era abrir `psql` y mirar
    `pg_stat_activity`. Aquí el costo es mayor que allá --la sentencia es
    UNA de horas, no un bucle de bloques-- así que el latido no puede
    colgarse del flujo: lo emite un hilo aparte.

    El hilo es `daemon`, así que no puede dejar el proceso colgado si algo
    sale mal, y el intervalo sale de `PERMANENCIA_LATIDO_SEGUNDOS`; en 0 ni
    se arranca.

    Args:
        conn: la conexión que va a ejecutar la sentencia; de ella se toma el
            backend pid que el hilo vigila.
        destino: qué se está llenando, para el mensaje.
        log: logger del stage que está corriendo.

    Yields:
        None: el bloque corre con el latido activo.
    """
    intervalo = get_settings().PERMANENCIA_LATIDO_SEGUNDOS
    if intervalo <= 0:
        yield
        return

    pid = conn.execute(text("SELECT pg_backend_pid()")).scalar_one()
    detener = threading.Event()
    hilo = threading.Thread(
        target=_vigilar,
        args=(pid, destino, intervalo, detener, log),
        daemon=True,
        name=f"latido-{destino}",
    )
    hilo.start()
    try:
        yield
    finally:
        detener.set()
        hilo.join(timeout=5)


def ejecutar(conn: Connection, paso: Paso, etiqueta: str, log: logging.Logger) -> int:
    """Corre un paso con latido y lo registra al terminar.

    Args:
        conn: conexión dentro de una transacción.
        paso: qué ejecutar.
        etiqueta: prefijo del log, p. ej. `bloque 7/32`.
        log: logger del stage.

    Returns:
        int: filas que produjo.
    """
    inicio = time.monotonic()
    with latido(conn, f"{etiqueta} {paso.nombre}", log):
        resultado = conn.execute(text(paso.sql))
    segundos = time.monotonic() - inicio
    log.info(f"      {etiqueta} {paso.nombre}: {resultado.rowcount:,} filas en {segundos:.1f}s")
    return resultado.rowcount


class Agregado(ABC):
    """Lo que un destino declara para que la maquinaria lo construya.

    Tres piezas de SQL y los nombres de las tablas que toca. La maquinaria
    no sabe qué columnas tiene el destino ni qué calcula: solo que se llena
    corte por corte en una tabla de trabajo y se agrega bloque por bloque.

    El `logger` es por destino, no global: cada agregado escribe en su
    archivo de stage, y la maquinaria usa el que recibe. Así no hay que
    inyectar nada ni hay riesgo de que dos destinos se pisen el log.
    """

    def __init__(
        self,
        *,
        nombre: str,
        schema: str,
        tabla: str,
        identidad: str,
        logger: logging.Logger,
        job_name: str,
        job_type: str,
        llave_destino: str,
        origen_schema: str = DEFAULT_SCHEMA,
    ):
        """
        Args:
            nombre: cómo se llama este agregado en los logs (`permanencia_radio`).
            schema: schema del DESTINO (`silver` o `gold`).
            tabla: tabla destino, sin calificar.
            identidad: tabla de identidad de la que depende
                (`clee_radio`...), en `origen_schema`. Es el reloj de
                frescura: si se re-resolvió después de construir, hay que
                reconstruir.
            logger: logger del stage; la maquinaria escribe ahí.
            job_name: nombre de la corrida en `meta.job_runs`.
            job_type: etapa en `meta.job_runs.job_type`.
            llave_destino: columna de identidad en la tabla DESTINO. No se
                llama igual en las dos: `clee_resuelto` en Silver y `clee` en
                Gold, donde la identidad es la que publico INEGI y no hay
                nada "resuelto". La maquinaria la necesita para saber que
                bloques ya estan escritos.
            origen_schema: schema de los hechos y de la identidad.
        """
        self.nombre = nombre
        self.schema = schema
        self.tabla = tabla
        self.identidad = identidad
        self.logger = logger
        self.job_name = job_name
        self.job_type = job_type
        self.llave_destino = llave_destino
        self.origen_schema = origen_schema

    @property
    def obs(self) -> str:
        """Tabla de trabajo donde se juntan los hechos con la identidad.

        Se deriva del destino y no del método: dos destinos distintos no
        pueden compartirla, porque una construcción de Gold pisaría el
        avance de una de Silver corriendo en paralelo.
        """
        return f"{self.tabla}_obs"

    @property
    def obs_schema(self) -> str:
        """Schema de la tabla de trabajo.

        Vive en el schema del DESTINO: es transitoria y suya, igual que las
        tablas de staging del ingest viven en `silver`.
        """
        return self.schema

    @property
    def pista_vacio(self) -> str:
        """Qué correr si la tabla de trabajo quedó vacía, en una línea.

        El default nombra la tabla de identidad, que es lo que la maquinaria
        sabe; un destino que conozca el comando exacto lo dice mejor.
        """
        return f"Corre primero lo que puebla {self.origen_schema}.{self.identidad}."

    def cortes(self, conn: Connection) -> list[date]:
        """Los cortes que entran al agregado, del más viejo al más nuevo.

        El default son todos los de Silver. Un destino que agrega sobre un
        subconjunto lo filtra aquí, y con eso los `idx` se renumeran solos
        dentro de la ventana --que es la parte que no se puede equivocar:
        heredar los índices de la secuencia completa rompería
        `presente_desde_el_inicio` y `duracion_periodos` sin un solo error
        visible.

        Args:
            conn: conexión de lectura.

        Returns:
            list[date]: cortes ordenados.
        """
        return cortes_ordenados(conn, self.origen_schema)

    @abstractmethod
    def sql_crear_obs(self) -> str:
        """DDL de la tabla de trabajo: las columnas que el agregado necesita."""

    @abstractmethod
    def sql_llenar_obs(self, corte: date, idx: int, bloques: int) -> str:
        """`INSERT` que carga en la tabla de trabajo las observaciones de UN corte.

        Args:
            corte: el corte a cargar.
            idx: su posición DENTRO de la ventana de este agregado.
            bloques: cuántos bloques repartir (para `expr_bloque`).
        """

    @abstractmethod
    def pasos_bloque(self, bloque: int, ultimo_idx: int) -> list[Paso]:
        """Los pasos que escriben las filas de UN bloque de identidades.

        El último tiene que ser el `INSERT` en la tabla destino.

        Args:
            bloque: qué bloque procesar.
            ultimo_idx: posición del último corte de la ventana, para la
                censura por la derecha.
        """


def _obs_existe(conn: Connection, ag: Agregado) -> bool:
    """Si la tabla de trabajo del agregado ya está creada."""
    return bool(
        conn.execute(
            text("SELECT to_regclass(:nombre) IS NOT NULL"),
            {"nombre": f"{ag.obs_schema}.{ag.obs}"},
        ).scalar()
    )


def _marcar_obs(conn: Connection, ag: Agregado) -> None:
    """Deja fechada la tabla de trabajo, para validar reanudaciones.

    La marca va en el COMMENT de la tabla y no en una columna: una columna
    de fecha serían 8 bytes por cada una de las 82.9M filas para guardar un
    solo dato, y el comentario se va solo cuando la tabla se borra.
    """
    conn.execute(
        text(
            f"COMMENT ON TABLE {ag.obs_schema}.{ag.obs} IS "
            f"'armado_en={datetime.now(UTC).isoformat()}'"
        )
    )


def identidad_resuelta_en(conn: Connection, ag: Agregado) -> datetime | None:
    """Cuándo se resolvió por última vez la identidad de la que depende.

    `resuelto_en` es la columna que la resolución CLEE escribe justamente
    para poder fechar un resultado, y es el insumo de los dos chequeos de
    frescura: el de la tabla de trabajo a medio llenar y el de la tabla ya
    construida.

    Returns:
        datetime | None: el instante más reciente, o None si la tabla de
        identidad está vacía.
    """
    return conn.execute(
        text(f"SELECT max(resuelto_en) FROM {ag.origen_schema}.{ag.identidad}")
    ).scalar()


def _identidad_mas_nueva_que_obs(conn: Connection, ag: Agregado) -> bool:
    """Si la resolución CLEE se re-corrió después de armar la tabla de trabajo.

    Reanudar en ese caso sería **incorrecto**, no solo inútil: los bloques
    ya escritos vendrían de una resolución y los pendientes de otra, y la
    tabla final mezclaría dos conjuntos de identidades sin que nada lo
    delate. Las `NEW_*` ni siquiera son estables entre corridas, así que la
    misma fila puede aparecer con dos identidades distintas.

    El reloj de este lado es el `COMMENT` que dejó `_marcar_obs` y no
    `meta.process_tasks`: lo que hay que fechar es la tabla de trabajo, un
    estado a medio construir que puede sobrevivir a varias corridas y que
    muere cuando ella se borra. Lo que `meta` fecha es la tabla terminada.
    """
    marca = conn.execute(
        text(f"SELECT obj_description('{ag.obs_schema}.{ag.obs}'::regclass, 'pg_class')")
    ).scalar()
    if not marca or not marca.startswith("armado_en="):
        # Sin marca no se puede afirmar que la identidad siga siendo la
        # misma, y el riesgo de mezclarlas no se corre a la ligera.
        return True

    resuelta = identidad_resuelta_en(conn, ag)
    return resuelta is not None and resuelta > datetime.fromisoformat(
        marca.removeprefix("armado_en=")
    )


def _cortes_pendientes(
    conn: Connection, ag: Agregado, cortes: list[date]
) -> list[tuple[int, date]]:
    """Los cortes que aún no están en la tabla de trabajo, con su índice.

    El avance se deriva de la propia tabla en vez de un registro aparte: un
    corte que falló a la mitad hizo rollback y no dejó ni una fila, así que
    "tiene filas" equivale a "está completo".

    Returns:
        list[tuple[int, date]]: pares (índice en la ventana, corte) por cargar.
    """
    hechos = set(
        conn.execute(text(f"SELECT DISTINCT period FROM {ag.obs_schema}.{ag.obs}")).scalars().all()
    )
    return [(i, c) for i, c in enumerate(cortes) if c not in hechos]


def _bloques_pendientes(conn: Connection, ag: Agregado, bloques: int, llave: str) -> list[int]:
    """Los bloques que aún no están en la tabla destino.

    Mismo criterio que con los cortes: el `INSERT` de un bloque es una
    transacción, así que o escribió todas sus identidades o ninguna.

    Args:
        conn: conexión de lectura.
        ag: el agregado.
        bloques: cuántos bloques en total.
        llave: columna de identidad en la tabla DESTINO, que no siempre se
            llama igual (`clee_resuelto` en Silver, `clee` en Gold).

    Returns:
        list[int]: bloques por procesar, en orden.
    """
    escritos = set(
        conn.execute(
            text(f"""
            SELECT DISTINCT {expr_bloque(llave, bloques)}
            FROM {ag.schema}.{ag.tabla}
            """)
        )
        .scalars()
        .all()
    )
    return [b for b in range(bloques) if b not in escritos]


def _ultimo_exito(tracker: MetadataTracker, nombre: str) -> datetime | None:
    """Cuándo terminó la última construcción exitosa de este agregado.

    Sale de `meta.process_tasks` y no de la tabla misma: con el troceo, una
    tabla con filas puede estar a medio llenar, así que "tiene filas" no
    equivale a "está completa". Una tarea en `SUCCESS` sí: se escribe una
    sola vez y al final.
    """
    # La unidad de trabajo ES la tabla entera --no hay archivo por corte que
    # nombrar-- asi que el nombre de la tarea y el del destino coinciden.
    tarea = tracker.get_last_successful_process_task(task_name=nombre, target_table=nombre)
    return tarea["end_time"] if tarea is not None else None


def _cerrar(
    tracker: MetadataTracker,
    run_id: str,
    task_id: str,
    status: TaskStatus,
    *,
    rows_extracted: int | None = None,
    rows_loaded: int | None = None,
    error: str | None = None,
) -> None:
    """Cierra la tarea y su corrida con el mismo veredicto.

    Van juntas porque la corrida existe solo para colgar esta tarea --una
    construcción por destino-- así que dos estados distintos no significarían
    nada. `SKIPPED` cierra la corrida en `SUCCESS`: saltarse trabajo que ya
    estaba hecho es un resultado bueno, no un fallo.
    """
    tracker.complete_process_task(
        task_id,
        status,
        rows_extracted=rows_extracted,
        rows_loaded=rows_loaded,
        error_message=error,
    )
    tracker.complete_run(
        run_id,
        JobStatus.FAILED if status is TaskStatus.FAILED else JobStatus.SUCCESS,
        error_summary=error,
    )


def _llenar(engine: Engine, ag: Agregado, bloques: int, rehacer: bool) -> tuple[int, int]:
    """Las dos fases del troceo. No llamar directo: usar `construir`.

    Lo único que borra es lo suyo: la tabla de trabajo y la destino del
    agregado. Los hechos, la identidad y `meta` no se tocan.

    Returns:
        tuple[int, int]: filas en la tabla destino al terminar, y
        observaciones que la tabla de trabajo llegó a tener (0 si no había
        nada que construir).

    Raises:
        RuntimeError: si hay avance previo pero la identidad cambió.
    """
    log = ag.logger

    with engine.begin() as conn:
        cortes = ag.cortes(conn)
        if not cortes:
            log.warning(f"   {ag.nombre}: sin cortes que agregar, nada que construir")
            return 0, 0

        if rehacer:
            conn.execute(text(f"DROP TABLE IF EXISTS {ag.obs_schema}.{ag.obs}"))
            conn.execute(text(f"TRUNCATE {ag.schema}.{ag.tabla}"))
        elif _obs_existe(conn, ag) and _identidad_mas_nueva_que_obs(conn, ag):
            raise RuntimeError(
                f"{ag.origen_schema}.{ag.identidad} se re-resolvio despues de armar "
                f"{ag.obs_schema}.{ag.obs}: reanudar mezclaria identidades de dos "
                f"corridas. Corre con --force."
            )

        nueva = not _obs_existe(conn, ag)
        conn.execute(text(ag.sql_crear_obs()))
        if nueva:
            _marcar_obs(conn, ag)
        pendientes = _cortes_pendientes(conn, ag, cortes)

    hechos = len(cortes) - len(pendientes)
    log.info(
        f"   {ag.nombre} -> {ag.schema}.{ag.tabla}: {len(cortes)} cortes "
        f"({cortes[0]} .. {cortes[-1]}) en {bloques} bloques"
    )
    if hechos:
        log.info(f"   reanudando: {hechos}/{len(cortes)} cortes ya estaban en {ag.obs}")

    # ---- Fase 1: los hechos unidos a la identidad, un corte por vuelta ----
    inicio = time.monotonic()
    for n, (idx, corte) in enumerate(pendientes, start=hechos + 1):
        etiqueta = f"obs {n}/{len(cortes)}"
        log.info(f"   ▶ {etiqueta}: cargando el corte {corte}...")
        with engine.begin() as conn:
            conn.execute(text(SET_WORK_MEM))
            with latido(conn, etiqueta, log):
                cargadas = conn.execute(text(ag.sql_llenar_obs(corte, idx, bloques))).rowcount
        log.info(f"   ✔ {etiqueta}: {corte} -> {cargadas:,} observaciones")

    with engine.begin() as conn:
        total_obs = filas(conn, ag.obs, ag.obs_schema)
        if total_obs == 0:
            log.warning(
                f"   ⚠️ {ag.tabla} vacia: {ag.origen_schema}.{ag.identidad} sin poblar "
                f"para estos cortes. {ag.pista_vacio}"
            )
            conn.execute(text(f"DROP TABLE IF EXISTS {ag.obs_schema}.{ag.obs}"))
            return 0, 0
        # Indice y estadisticas DESPUES de llenar: construirlo antes
        # costaria mantenerlo en cada INSERT, y una tabla sin ANALYZE deja al
        # planificador armando los bloques a ciegas (el autovacuum no toca
        # las UNLOGGED recien creadas a tiempo).
        log.info(f"   ▶ indice y estadisticas sobre {total_obs:,} observaciones...")
        conn.execute(
            text(
                f"CREATE INDEX IF NOT EXISTS ix_{ag.obs}_bloque "
                f"ON {ag.obs_schema}.{ag.obs} (bloque)"
            )
        )
        conn.execute(text(f"ANALYZE {ag.obs_schema}.{ag.obs}"))
        faltan = _bloques_pendientes(conn, ag, bloques, ag.llave_destino)

    if len(faltan) < bloques:
        log.info(f"   reanudando: {bloques - len(faltan)}/{bloques} bloques ya escritos")

    # ---- Fase 2: los agregados, un bloque de identidades por vuelta ----
    ultimo_idx = len(cortes) - 1
    for bloque in faltan:
        etiqueta = f"bloque {bloque + 1}/{bloques}"
        inicio_bloque = time.monotonic()
        with engine.begin() as conn:
            conn.execute(text(SET_WORK_MEM))
            escritas = 0
            for paso in ag.pasos_bloque(bloque, ultimo_idx):
                escritas = ejecutar(conn, paso, etiqueta, log)
        log.info(
            f"   ✔ {etiqueta}: {escritas:,} identidades en "
            f"{(time.monotonic() - inicio_bloque) / 60:.1f} min"
        )

    with engine.begin() as conn:
        total = filas(conn, ag.tabla, ag.schema)
        conn.execute(text(f"DROP TABLE IF EXISTS {ag.obs_schema}.{ag.obs}"))

    minutos = (time.monotonic() - inicio) / 60
    log.info(f"   ✅ {ag.schema}.{ag.tabla}: {total:,} identidades en {minutos:.1f} min")
    return total, total_obs


def construir(engine: Engine, ag: Agregado, tracker: MetadataTracker, force: bool = False) -> int:
    """Construye el agregado y lo registra en `meta`.

    Deja una corrida en `meta.job_runs` y una tarea en `meta.process_tasks`
    --filas leídas, filas escritas, duración, error-- de modo que "cuánto
    tardó" y "terminó bien" se contesten con SQL y no leyendo un log.

    **Decide sola si hay algo que hacer**, comparando el `end_time` de la
    última tarea exitosa contra `max(resuelto_en)` de la identidad:

    - **Nunca se construyó, o la última corrida murió a la mitad**: reanuda.
      El avance se deriva de las propias tablas --una vuelta que falló hizo
      rollback y no dejó filas-- y no del registro en `meta`, que es más
      grueso que una vuelta.
    - **Construida y la identidad no cambió**: `SKIPPED`. Sin esto, volver a
      pedirla rellenaría la tabla de trabajo entera para acabar descubriendo
      que todos los bloques ya estaban escritos.
    - **Construida pero la identidad se re-resolvió después**: reconstruye
      desde cero. La tabla vigente quedó stale --sus agregados son de otro
      conjunto de identidades-- y pedir `--force` a mano haría que el
      pipeline semanal sirviera datos viejos en silencio.
    - **Historial de éxito pero la tabla vacía**: una migración la recreó, y
      "ya construida" no puede ser cierto sin filas. Se construye.

    Args:
        engine: engine con permisos de escritura; esta función controla sus
            transacciones, una por vuelta.
        ag: el agregado a construir.
        tracker: registro de `meta` donde queda la corrida.
        force: si True, descarta lo que haya y reconstruye entero.

    Returns:
        int: filas en la tabla destino al terminar.

    Raises:
        RuntimeError: si hay avance previo a medio construir pero la
            identidad cambió; ahí sí hace falta `--force`, porque lo que se
            descartaría son horas de trabajo de esta corrida.
    """
    log = ag.logger
    bloques = max(get_settings().PERMANENCIA_BLOQUES, 1)

    run_id = tracker.create_run(
        job_name=ag.job_name,
        job_type=ag.job_type,
        trigger_type=TriggerType.MANUAL.value,
        config={"agregado": ag.nombre, "bloques": bloques, "force": force},
    )
    task_id = tracker.create_process_task(
        run_id=run_id,
        task_name=ag.nombre,
        # El origen no es un archivo sino dos tablas, y es lo que
        # `source_path` tiene que decir para que la fila valga como linaje.
        source_path=f"{ag.origen_schema}.establecimientos + {ag.origen_schema}.{ag.identidad}",
        target_table=ag.nombre,
    )
    tracker.start_process_task(task_id)

    try:
        with engine.connect() as conn:
            construida_en = _ultimo_exito(tracker, ag.nombre)
            resuelta_en = identidad_resuelta_en(conn, ag)
            desactualizada = (
                construida_en is not None
                and resuelta_en is not None
                and resuelta_en > construida_en
            )
            if construida_en is not None and not desactualizada and not force:
                total = filas(conn, ag.tabla, ag.schema)
                if total > 0:
                    log.info(
                        f"   ✨ {ag.tabla}: ya construida con la identidad vigente "
                        f"({total:,} identidades). Nada que hacer; --force la rehace."
                    )
                    _cerrar(tracker, run_id, task_id, TaskStatus.SKIPPED, rows_loaded=total)
                    return total
                # Historial de exito pero la tabla esta VACIA: una migracion
                # la recreo y el registro en `meta` quedo hablando de un
                # esquema que ya no existe.
                log.info(
                    f"   {ag.tabla}: hay una corrida exitosa en meta pero la tabla esta "
                    f"vacia (la recreo una migracion); se construye"
                )

        if force:
            log.info(f"   {ag.nombre}: --force, se descarta el avance previo")
        elif desactualizada:
            log.info(
                f"   {ag.nombre}: {ag.origen_schema}.{ag.identidad} se re-resolvio despues "
                f"de construir {ag.tabla}; se reconstruye desde cero"
            )

        total, observaciones = _llenar(engine, ag, bloques, rehacer=force or desactualizada)
    except Exception as e:
        _cerrar(tracker, run_id, task_id, TaskStatus.FAILED, error=str(e))
        raise

    _cerrar(
        tracker,
        run_id,
        task_id,
        TaskStatus.SUCCESS if observaciones else TaskStatus.SKIPPED,
        rows_extracted=observaciones,
        rows_loaded=total,
    )
    return total
