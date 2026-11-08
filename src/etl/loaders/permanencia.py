"""Construye `silver.permanencia_<metodo>` desde los hechos y la identidad.

Colapsa las 82.9M observaciones de `silver.establecimientos` a una fila por
establecimiento resuelto: su ventana de permanencia en el directorio, los
ingredientes de censura y un resumen de cuánto varió entre cortes.

Existe como tabla materializada y no como vista porque el `GROUP BY` sobre
82.9M filas unidas a la tabla de identidades cuesta minutos, y el modelo de
supervivencia la lee decenas de veces --cada partición de la validación
cruzada, cada iteración de variables, el cálculo de SHAP.

**Ningún valor de un corte concreto.** Lo que se materializa son agregados
sobre la vida completa (`n_nombres`, `cambio_municipio`,
`delta_empleados_min`, el rectángulo envolvente) y banderas de cobertura
(`tiene_*`: el campo vino lleno en al menos un corte). El valor mismo de
cualquier campo oficial --nombre, actividad, municipio, estrato, tipo de
unidad, coordenadas-- sale de unir `clee_resuelto` con la tabla de
identidad y de ahí a los hechos, que devuelve **todos** los cortes y no
solo el último; `AGENTS.md` §N3 trae la consulta.

Guardar "el valor del corte más reciente" era el peor de los dos mundos:
costaba ancho en 10M filas, congelaba un instante de algo que cambia entre
cortes, y de todas formas no contestaba "¿qué valores tuvo?". El join es
barato justo en el caso en que se necesita --un establecimiento, o una
muestra-- y la tabla queda angosta para el caso en que se leen los 10M.

**Tampoco guarda el veredicto de cierre**, que se deriva de
`cortes_tras_ultimo` con un `CASE` en la consulta que lo necesite. El
protocolo llama preliminar a la regla, así que el umbral vive donde se
pregunta y no en el esquema: cambiarlo no debe costar 10M de filas ni un
`CREATE OR REPLACE`.

**Este módulo es puro SQL de Silver.** Cómo se trocea el trabajo, cómo se
reanuda, el latido y el registro en `meta` los pone `etl/utils/troceado.py`,
que no sabe qué columnas tiene esta tabla ni qué calcula. Lo que `Permanencia`
declara son las tres piezas que la máquina le pide: la forma de la tabla de
trabajo, cómo se llena un corte, y los pasos que escriben un bloque.

**Cada construcción queda registrada en `meta`**: una corrida en
`meta.job_runs` y una tarea en `meta.process_tasks` por método, con filas
leídas, filas escritas, duración y error. Eso es lo que hace consultable
--sin abrir `data/etl_permanencia.log`-- cuánto tardó la última corrida y
si terminó bien, y es también el reloj con que se decide reconstruir: ver
`troceado.construir`.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from sqlalchemy.engine import Engine

from src.etl.utils.troceado import Agregado, Paso, construir, expr_bloque
from src.meta.tracker import MetadataTracker
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.enums import OrigenClee
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_permanencia.log"))

METODOS: dict[str, str] = {
    "radio": "clee_radio",
    "h3": "clee_h3",
}
"""Método -> tabla de identidad y, por convención, tabla destino.

El destino es `permanencia_<metodo>`. Agregar un método es una entrada aquí
más su tabla, sin tocar el SQL.
"""

JOB_NAME = "denue_permanencia_{metodo}"
"""Plantilla del nombre de la corrida en `meta.job_runs`, una por método.

El método va en el nombre --igual que `denue_clee_resolver_<metodo>`-- para
que `job_name` distinga las dos tablas sin tener que abrir el `config` de
la corrida.
"""

JOB_TYPE = "PERMANENCIA"
"""Etapa a la que pertenece la corrida en `meta.job_runs.job_type`.

Es su propia etapa y no `TRANSFORM`: `/ops/health/pipeline` reporta la
última corrida POR TIPO, así que mezclarla con el ingest haría que un
ingest exitoso tapara una permanencia fallida.
"""

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra, para la diagonal del rectángulo envolvente."""


def tabla_destino(metodo: str) -> str:
    """Nombre de la tabla de permanencia de un método, sin calificar."""
    return f"permanencia_{metodo}"


def tabla_obs(metodo: str) -> str:
    """Nombre de la tabla de trabajo de un método, sin calificar.

    Es por método y no una sola compartida: construir `radio` no debe pisar
    el avance de una corrida de `h3`.
    """
    return f"permanencia_obs_{metodo}"


class Permanencia(Agregado):
    """El SQL de `silver.permanencia_<metodo>`, para que `troceado` lo corra.

    No controla transacciones, no reanuda y no escribe en `meta`: eso es de
    la máquina. Aquí solo está lo que es específico de esta tabla.
    """

    def __init__(self, metodo: str, schema: str = DEFAULT_SCHEMA):
        """
        Args:
            metodo: clave de `METODOS` (`radio` o `h3`).
            schema: schema de origen y destino.

        Raises:
            ValueError: si el método no está registrado.
        """
        if metodo not in METODOS:
            raise ValueError(f"Metodo desconocido '{metodo}'. Opciones: {list(METODOS)}")

        destino = tabla_destino(metodo)
        super().__init__(
            nombre=destino,
            schema=schema,
            tabla=destino,
            identidad=METODOS[metodo],
            logger=logger,
            job_name=JOB_NAME.format(metodo=metodo),
            job_type=JOB_TYPE,
            llave_destino="clee_resuelto",
            origen_schema=schema,
        )
        self.metodo = metodo

    @property
    def obs(self) -> str:
        """`permanencia_obs_<metodo>`, no el `<tabla>_obs` del default.

        El nombre es anterior a la máquina y hay tablas de trabajo vivas en
        la workstation con él; cambiarlo volvería inalcanzable el avance de
        una corrida a medio terminar.
        """
        return tabla_obs(self.metodo)

    @property
    def pista_vacio(self) -> str:
        """El comando exacto, que es lo que uno quiere leer en el log."""
        return f"Corre primero `transform --only clee-{self.metodo}`."

    def sql_crear_obs(self) -> str:
        """DDL de la tabla de trabajo: los hechos unidos a la identidad.

        Es `UNLOGGED` y no `TEMP` por una razón concreta: una temporal muere
        en el primer `COMMIT`, y el troceo necesita commitear cada corte para
        que un fallo no se lleve lo ya hecho. `UNLOGGED` da lo que importaba
        de la temporal --no escribe WAL, así que cuesta casi lo mismo-- sin
        esa restricción. El precio es que hay que borrarla a mano al
        terminar; lo hace la máquina.

        Vive en `silver` con nombre explícito, como las tablas de staging del
        ingest: es transitoria, no parte del esquema.

        Returns:
            str: `CREATE UNLOGGED TABLE` completo.
        """
        return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {self.obs_schema}.{self.obs} (
    clee_resuelto   text    NOT NULL,
    bloque          smallint NOT NULL,
    idx             integer NOT NULL,
    period          date    NOT NULL,
    id              text    NOT NULL,
    nom_estab       text,
    raz_social      text,
    codigo_act      text,
    cve_ent         text,
    cve_mun         text,
    cod_postal      text,
    latitud         double precision,
    longitud        double precision,
    fecha_alta_date date,
    telefono        text,
    correoelec      text,
    www             text,
    tipo_asent      text,
    cve_loc         text,
    ageb            text,
    nom_cencom      text,
    origen          text,
    per_ocu         text,
    empleados_min   integer,
    tipounieco      text,
    direccion       text
)
"""

    def sql_llenar_obs(self, corte: date, idx: int, bloques: int) -> str:
        """Carga en la tabla de trabajo las observaciones de UN corte.

        Lee exactamente una partición de los hechos y una de la identidad
        --el `WHERE period` las poda-- en vez de recorrer los 82.9M.

        El índice del corte entra como literal: ya se calculó desde el
        catálogo, y es lo que convierte una fecha en la posición que usa el
        análisis.

        Args:
            corte: el corte a cargar.
            idx: su posición en la secuencia ordenada de cortes.
            bloques: cuántos bloques repartir.

        Returns:
            str: `INSERT ... SELECT` de un corte.
        """
        schema = self.origen_schema
        literal = f"DATE '{corte.isoformat()}'"
        return f"""
INSERT INTO {self.obs_schema}.{self.obs} (
    clee_resuelto, bloque, idx, period, id,
    nom_estab, raz_social, codigo_act, cve_ent, cve_mun, cod_postal,
    latitud, longitud, fecha_alta_date,
    telefono, correoelec, www, tipo_asent,
    cve_loc, ageb, nom_cencom, origen,
    per_ocu, empleados_min, tipounieco,
    direccion
)
SELECT
    i.clee_resuelto,
    {expr_bloque("i.clee_resuelto", bloques)},
    {idx},
    e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cod_postal,
    e.latitud, e.longitud, e.fecha_alta_date,
    e.telefono, e.correoelec, e.www, ta.tipo_asent,
    e.cve_loc, e.ageb, e.nom_cencom,
    i.origen,
    ep.per_ocu, ep.empleados_min,
    tu.tipounieco,
    -- El domicilio como UNA cadena: arrays paralelos por componente
    -- perderian que numero va con que calle.
    NULLIF(CONCAT_WS(' | ',
        NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
        NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')
    ), '') AS direccion
FROM {schema}.establecimientos e
JOIN {schema}.{self.identidad} i ON i.id = e.id AND i.period = e.period
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE e.period = {literal}
  AND i.period = {literal}
  AND i.clee_resuelto IS NOT NULL AND i.clee_resuelto <> ''
"""

    def pasos_bloque(self, bloque: int, ultimo_idx: int) -> list[Paso]:
        """Los pasos que escriben la permanencia de UN bloque de identidades.

        Las tres temporales sí son `TEMP ... ON COMMIT DROP`: viven dentro de
        la transacción del bloque y se van con ella, commitee o falle.

        El troceo por identidad no cambia un solo resultado --los agregados
        de un establecimiento dependen solo de sus propias filas, y todas
        están en el mismo bloque-- y suele ser más RÁPIDO que hacerlo de
        golpe: el `GROUP BY` completo ordena 10M de grupos con una decena de
        `COUNT(DISTINCT)` cada uno y se derrama a disco, mientras que los
        ~300k grupos de un bloque caben en `work_mem`. Evitar el derrame vale
        más que el costo de dar varias vueltas.

        Args:
            bloque: qué bloque procesar.
            ultimo_idx: posición del último corte publicado, para la censura.

        Returns:
            list[Paso]: pasos del bloque, en orden; el último es el `INSERT`.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        destino = self.tabla
        directo = OrigenClee.DIRECTO.value
        propagado = OrigenClee.PROPAGADO.value

        return [
            Paso(
                "ventana",
                "colapsando a una fila por identidad",
                f"""
CREATE TEMP TABLE ventana ON COMMIT DROP AS
SELECT
    clee_resuelto,
    MIN(period) AS primer_periodo,
    MAX(period) AS ultimo_periodo,
    MIN(idx) AS idx_primer,
    MAX(idx) AS idx_ultimo,
    COUNT(DISTINCT idx) AS cortes_observados,
    MIN(fecha_alta_date) AS fecha_alta_date,
    -- Resumen de variantes: CUANTAS hubo, no cuales. Los valores salen de
    -- este mismo join sin agregar, filtrando por clee_resuelto.
    -- El FILTER descarta nulos y cadenas vacias: un '' no es una
    -- variante, es un dato ausente.
    COUNT(DISTINCT nom_estab) FILTER (
        WHERE nom_estab IS NOT NULL AND nom_estab <> '') AS n_nombres,
    COUNT(DISTINCT codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS n_actividades,
    COUNT(DISTINCT cve_ent || cve_mun) FILTER (
        WHERE cve_ent IS NOT NULL AND cve_mun IS NOT NULL) AS n_municipios,
    COUNT(DISTINCT direccion) FILTER (WHERE direccion IS NOT NULL) AS n_direcciones,
    COUNT(DISTINCT per_ocu) FILTER (
        WHERE per_ocu IS NOT NULL AND per_ocu <> '') AS n_estratos,
    -- Patron de presencia completo: de aqui el analisis deriva cualquier
    -- medida de ausencia sin volver a los 82.9M de hechos. Es el unico
    -- array que se queda: reconstruirlo exigiria rehacer el mapeo
    -- periodo->indice, y son enteros de a lo mas veinte elementos.
    ARRAY_AGG(DISTINCT idx ORDER BY idx) AS cortes_idx,
    -- Cuanto de la identidad es observado frente a inferido.
    COUNT(DISTINCT idx) FILTER (WHERE origen = '{directo}') AS cortes_directo,
    COUNT(DISTINCT idx) FILTER (WHERE origen = '{propagado}') AS cortes_propagado,
    -- Cobertura del dato: UNION sobre todos los cortes, no el mas reciente.
    -- Dice de que establecimientos se puede afirmar algo sobre cada campo;
    -- el VALOR, corte por corte, sale del join a los hechos.
    BOOL_OR(nom_estab IS NOT NULL AND nom_estab <> '') AS tiene_nombre,
    BOOL_OR(raz_social IS NOT NULL AND raz_social <> '') AS tiene_razon_social,
    BOOL_OR(codigo_act IS NOT NULL AND codigo_act <> '') AS tiene_actividad,
    BOOL_OR(cve_ent IS NOT NULL AND cve_mun IS NOT NULL) AS tiene_municipio,
    BOOL_OR(cve_loc IS NOT NULL AND cve_loc <> '') AS tiene_localidad,
    BOOL_OR(ageb IS NOT NULL AND ageb <> '') AS tiene_ageb,
    BOOL_OR(cod_postal IS NOT NULL AND cod_postal <> '') AS tiene_cod_postal,
    BOOL_OR(tipo_asent IS NOT NULL AND tipo_asent <> '') AS tiene_tipo_asent,
    BOOL_OR(latitud IS NOT NULL AND longitud IS NOT NULL) AS tiene_coordenadas,
    BOOL_OR(per_ocu IS NOT NULL AND per_ocu <> '') AS tiene_estrato_personal,
    BOOL_OR(tipounieco IS NOT NULL AND tipounieco <> '') AS tiene_tipounieco,
    -- Para verificar un cierre sirve cualquier telefono que haya publicado,
    -- aunque fuera en 2016.
    BOOL_OR(telefono IS NOT NULL AND telefono <> '') AS tiene_telefono,
    BOOL_OR(correoelec IS NOT NULL AND correoelec <> '') AS tiene_correo,
    BOOL_OR(www IS NOT NULL AND www <> '') AS tiene_sitio,
    BOOL_OR(nom_cencom IS NOT NULL AND nom_cencom <> '') AS en_centro_comercial,
    -- Rectangulo envolvente: cota SUPERIOR del movimiento, ciega al
    -- orden. Se lee junto al desplazamiento neto.
    MIN(latitud) AS lat_min, MAX(latitud) AS lat_max,
    MIN(longitud) AS lon_min, MAX(longitud) AS lon_max,
    COUNT(DISTINCT (latitud, longitud)) FILTER (
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL) AS n_posiciones
FROM {obs}
WHERE bloque = {bloque}
GROUP BY clee_resuelto
""",
            ),
            Paso(
                "primera",
                "tomando el estado con que empezo cada identidad",
                f"""
-- Los dos extremos REALES del recorrido, y solo lo que hace falta para las
-- medidas de trayectoria: la posicion (para el desplazamiento neto) y la
-- cota de empleados (para el delta). Ninguno de estos valores se guarda:
-- se consumen aqui y lo que persiste es el escalar derivado.
CREATE TEMP TABLE primera ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, latitud AS lat_primera, longitud AS lon_primera,
       empleados_min AS empleados_min_primera
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee_resuelto, period ASC, id
""",
            ),
            Paso(
                "ultima",
                "tomando el estado con que termino cada identidad",
                f"""
-- El otro extremo: el corte mas reciente en que se observo, desempatado
-- por id para que el resultado sea determinista.
CREATE TEMP TABLE ultima ON COMMIT DROP AS
SELECT DISTINCT ON (clee_resuelto)
       clee_resuelto, latitud, longitud, empleados_min
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee_resuelto, period DESC, id
""",
            ),
            Paso(
                destino,
                f"escribiendo el bloque en {self.schema}.{destino}",
                f"""
INSERT INTO {self.schema}.{destino} (
    clee_resuelto, es_sintetica,
    primer_periodo, ultimo_periodo, idx_primer, idx_ultimo,
    cortes_observados, huecos, cortes_tras_ultimo,
    duracion_periodos, presente_desde_el_inicio, fecha_alta_date, dias_alta_a_primera,
    cambio_tamano, delta_empleados_min,
    cortes_idx, cortes_directo, cortes_propagado,
    n_nombres, cambio_actividad, cambio_municipio, n_direcciones,
    tiene_nombre, tiene_razon_social, tiene_actividad, tiene_municipio,
    tiene_localidad, tiene_ageb, tiene_cod_postal, tiene_tipo_asent,
    tiene_coordenadas, tiene_estrato_personal, tiene_tipounieco,
    tiene_telefono, tiene_correo, tiene_sitio, en_centro_comercial,
    n_posiciones, desplazamiento_neto_m,
    lat_min, lat_max, lon_min, lon_max, dispersion_max_m
)
SELECT
    v.clee_resuelto,
    STARTS_WITH(v.clee_resuelto, 'NEW_') AS es_sintetica,
    v.primer_periodo, v.ultimo_periodo, v.idx_primer, v.idx_ultimo,
    v.cortes_observados,
    (v.idx_ultimo - v.idx_primer + 1) - v.cortes_observados AS huecos,
    {ultimo_idx} - v.idx_ultimo AS cortes_tras_ultimo,
    v.idx_ultimo - v.idx_primer AS duracion_periodos,
    -- Censura por la izquierda: ya estaba en la primera edicion, asi que su
    -- antiguedad real es desconocida. El protocolo exige el indicador.
    v.idx_primer = 0 AS presente_desde_el_inicio,
    v.fecha_alta_date,
    CASE WHEN v.fecha_alta_date IS NULL THEN NULL
         ELSE (v.primer_periodo - v.fecha_alta_date) END AS dias_alta_a_primera,
    v.n_estratos > 1 AS cambio_tamano,
    -- Trayectoria de tamano: ultimo menos primero, en empleados. Respeta el
    -- orden, asi que distingue crecer de encogerse -- que un conteo de
    -- estratos distintos no puede.
    u.empleados_min - p.empleados_min_primera AS delta_empleados_min,
    v.cortes_idx,
    COALESCE(v.cortes_directo, 0) AS cortes_directo,
    COALESCE(v.cortes_propagado, 0) AS cortes_propagado,
    v.n_nombres,
    v.n_actividades > 1 AS cambio_actividad,
    v.n_municipios > 1 AS cambio_municipio,
    v.n_direcciones,
    COALESCE(v.tiene_nombre, FALSE) AS tiene_nombre,
    COALESCE(v.tiene_razon_social, FALSE) AS tiene_razon_social,
    COALESCE(v.tiene_actividad, FALSE) AS tiene_actividad,
    COALESCE(v.tiene_municipio, FALSE) AS tiene_municipio,
    COALESCE(v.tiene_localidad, FALSE) AS tiene_localidad,
    COALESCE(v.tiene_ageb, FALSE) AS tiene_ageb,
    COALESCE(v.tiene_cod_postal, FALSE) AS tiene_cod_postal,
    COALESCE(v.tiene_tipo_asent, FALSE) AS tiene_tipo_asent,
    COALESCE(v.tiene_coordenadas, FALSE) AS tiene_coordenadas,
    COALESCE(v.tiene_estrato_personal, FALSE) AS tiene_estrato_personal,
    COALESCE(v.tiene_tipounieco, FALSE) AS tiene_tipounieco,
    COALESCE(v.tiene_telefono, FALSE) AS tiene_telefono,
    COALESCE(v.tiene_correo, FALSE) AS tiene_correo,
    COALESCE(v.tiene_sitio, FALSE) AS tiene_sitio,
    COALESCE(v.en_centro_comercial, FALSE) AS en_centro_comercial,
    COALESCE(v.n_posiciones, 0) AS n_posiciones,
    -- Neto: entre dos puntos REALES, respetando el orden temporal. Los dos
    -- puntos se consumen aqui; lo que persiste es esta distancia.
    CASE WHEN p.lat_primera IS NULL OR u.latitud IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(u.latitud - p.lat_primera) / 2), 2)
            + COS(RADIANS(p.lat_primera)) * COS(RADIANS(u.latitud))
            * POWER(SIN(RADIANS(u.longitud - p.lon_primera) / 2), 2)
        ))
    END AS desplazamiento_neto_m,
    v.lat_min, v.lat_max, v.lon_min, v.lon_max,
    -- Dispersion: la diagonal del rectangulo. Cota superior, no recorrido.
    CASE WHEN v.lat_min IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(v.lat_max - v.lat_min) / 2), 2)
            + COS(RADIANS(v.lat_min)) * COS(RADIANS(v.lat_max))
            * POWER(SIN(RADIANS(v.lon_max - v.lon_min) / 2), 2)
        ))
    END AS dispersion_max_m
FROM ventana v
JOIN primera p USING (clee_resuelto)
JOIN ultima u USING (clee_resuelto)
""",
            ),
        ]


def construir_permanencia(
    engine: Engine,
    metodo: str,
    tracker: MetadataTracker,
    schema: str = DEFAULT_SCHEMA,
    force: bool = False,
) -> int:
    """Construye la tabla de permanencia de un método y lo registra en `meta`.

    Es el punto de entrada del CLI: valida el método y se lo entrega a la
    máquina. Qué decide la máquina --reanudar, saltarse el trabajo ya hecho,
    o reconstruir porque la identidad se re-resolvió-- está en
    `troceado.construir`.

    Lo que descarta al reconstruir es solo lo suyo: la tabla de trabajo y la
    destino del método. Los hechos, `clee_<metodo>` y `meta` quedan intactos.

    Args:
        engine: engine con permisos de escritura; la máquina controla sus
            transacciones, una por vuelta.
        metodo: clave de `METODOS` (`radio` o `h3`).
        tracker: registro de `meta` donde queda la corrida.
        schema: schema de origen y destino.
        force: si True, descarta lo que haya y reconstruye entero.

    Returns:
        int: filas en la tabla destino al terminar.

    Raises:
        ValueError: si el método no está registrado.
        RuntimeError: si hay avance previo a medio construir pero la
            identidad cambió; ahí sí hace falta `--force`, porque lo que se
            descartaría son horas de trabajo de esta corrida.
    """
    return construir(engine, Permanencia(metodo, schema), tracker, force)
