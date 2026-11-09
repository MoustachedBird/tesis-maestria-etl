"""Construye `gold.negocios_clee_oficial` y su tabla de variantes.

La cohorte es **solo los cortes en que INEGI publica el CLEE**. Ahí la
identidad es observada y no inferida: no hay `NEW_*` que consolidar, no hay
error de propagación que arrastrar, y los tres métodos de resolución
coinciden trivialmente. Medido el 2026-11-08 son 9 cortes (2021-05 → 2026-05)
con cobertura del 100%, frente a 11 al 0%. El alcance está en
`gold_cohorte_2021.md`; las reglas por campo en `normalizacion_gold.md` §§4-6.

**No lee ninguna `silver.clee_<metodo>`.** La identidad sale del `clee` de
los propios hechos, que es lo que "oficial" significa. Por eso este loader no
depende de la etapa de resolución y por eso su reloj de frescura no es
`max(resuelto_en)` sino **la ventana**: lo que invalida esta tabla es que un
corte entre o salga de la lista de oficiales, no que alguien re-resuelva
identidades que aquí no se usan.

**Esta capa sí materializa valores**, al contrario de Silver. `b0007` sacó de
`permanencia` el valor "del corte más reciente" porque esas tablas son
reconstruibles a voluntad y el join entrega mejor; Gold es lo contrario por
definición: es la capa citable, el artefacto que la tesis reporta, y cada
fila lleva ventana, método y versión de reglas para que siga significando lo
mismo dentro de un año.

**Todo lo que escribe es determinista.** El nombre canónico es la *moda*
desempatada por el más reciente, que es el baseline obligatorio de
`normalizacion_gold.md` §6.4 --el LLM se adopta solo si gana en una muestra
etiquetada, y si empata se queda esto, que es gratis y reproducible. El SCIAN
canónico es el ancestro común por prefijo; cuando la discrepancia cruza rama
queda **NULL**, que es la señal de "esto hay que juzgarlo" y no un valor
inventado.

El troceo, el reanudar, el latido y el registro en `meta` son de
`etl/utils/troceado.py`: aquí solo está el SQL de esta capa.
"""

from __future__ import annotations

import time
from datetime import date, datetime
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from src.etl.utils.troceado import Agregado, Paso, construir, expr_bloque, latido
from src.meta.tracker import MetadataTracker
from src.shared.config import Settings, get_settings
from src.shared.db.base import DEFAULT_SCHEMA
from src.shared.db.models.gold import CAMPOS_CON_VARIANTES, SCHEMA
from src.shared.logging import get_stage_logger

logger = get_stage_logger(__name__, Path("./data/etl_gold.log"))

TABLA = "negocios_clee_oficial"
"""La tabla madre: un negocio de la cohorte por fila."""

TABLA_VARIANTES = f"{TABLA}_variantes"
"""La hija: los valores distintos que tuvo en cada campo, con su ventana."""

JOB_NAME = "gold_negocios_clee_oficial"
"""Nombre de la corrida en `meta.job_runs`."""

JOB_TYPE = "GOLD"
"""Etapa propia, no `TRANSFORM` ni `PERMANENCIA`.

`/ops/health/pipeline` reporta la última corrida POR TIPO, así que mezclarla
con otra etapa haría que un éxito ajeno tapara un fallo de Gold.
"""

METODO_BASE = "inegi"
"""De dónde salió la identidad, para `metodo_base`.

No es `radio` ni `h3`: en esta cohorte la identidad **no se resolvió**, la
publicó INEGI en el campo `clee` de los hechos. Poner el nombre de un método
de propagación sería falso justamente en la columna cuyo trabajo es decir de
dónde vino la identidad.
"""

COLUMNA_CANONICA: dict[str, str] = {
    "nom_estab": "nombre",
    "raz_social": "razon_social",
    "codigo_act": "codigo_act",
    "telefono": "telefono",
    "correoelec": "correoelec",
    "www": "www",
}
"""Campo de la tabla de variantes -> columna canónica en la tabla madre.

Casi todos se llaman igual, y **dos no**: la variante `nom_estab` se publica
en `nombre` y la variante `raz_social` en `razon_social`. La diferencia es
deliberada --en la madre son valores canónicos, no el campo crudo del DENUE--
pero por lo mismo no se puede derivar con una regla.

Se escribe completo en vez de con una excepción calculada precisamente porque
derivarlo falló: una comprensión con un solo caso especial generaba
`n.raz_social`, que no existe. `test_el_mapa_apunta_a_columnas_que_existen`
lo valida contra el ORM, que es lo que vuelve imposible que vuelva a pasar.
"""

_EARTH_RADIUS_M = 6_371_000.0
"""Radio medio de la Tierra, para la diagonal del rectángulo envolvente."""

_SQL_VENTANA_OFICIAL = """
SELECT period
FROM {schema}.establecimientos
GROUP BY period
HAVING count(*) FILTER (WHERE clee IS NOT NULL AND clee <> '')
       >= {minima} * count(*)
ORDER BY period
"""
"""Qué cortes son oficiales, calculado y no supuesto.

Es el criterio de `gold_cohorte_2021.md` §1.1, el mismo 99.9% que ya usaba
`scripts/evaluate_masked_clee.py`. Se calcula al arrancar y queda en
`ventana_cortes` de cada fila: si mañana entra un corte, la N y todas las
tasas cambian, y eso es lo que lo explica.

Deliberadamente **no** usa `clee_resolver._get_periods_with_clee_coverage`,
que clasifica con un `EXISTS`: ahí bastaría UNA fila con `clee` para declarar
oficial a un corte con 10% de cobertura. Gold calcula la suya.
"""


class CohorteOficial(Agregado):
    """El SQL de `gold.negocios_clee_oficial`, para que `troceado` lo corra.

    **No copia `silver.permanencia_<metodo>`**, y no es una optimización
    perdida: esa tabla agrega sobre los 20 cortes y una fila suya mezcla los
    dos regímenes --un negocio visto en 2019 (identidad propagada) y en 2021
    (oficial) comparte `clee_resuelto`. Filtrar por `cortes_directo > 0`
    daría la cohorte correcta con la ventana contaminada. Y los `idx` se
    renumeran dentro de la ventana oficial: heredar los de la secuencia de 20
    rompería `presente_desde_el_inicio` y `duracion_periodos` sin un solo
    error visible (`gold_cohorte_2021.md` §4).
    """

    def __init__(self, schema: str = DEFAULT_SCHEMA):
        """
        Args:
            schema: schema de ORIGEN (los hechos). El destino es `gold`
                siempre: es la capa que lee el API con `api_ro`.
        """
        super().__init__(
            nombre=TABLA,
            schema=SCHEMA,
            tabla=TABLA,
            # Formalmente no hay tabla de identidad: la identidad viene en los
            # hechos. Se nombra `establecimientos` para que `source_path` de
            # `meta.process_tasks` no mienta sobre de donde salio la fila.
            identidad="establecimientos",
            logger=logger,
            job_name=JOB_NAME,
            job_type=JOB_TYPE,
            llave_destino="clee",
            origen_schema=schema,
        )
        self._ventana: list[date] | None = None

    # ----- lo que esta capa define distinto de Silver -----

    @property
    def tablas_destino(self) -> tuple[str, ...]:
        """La madre y su evidencia. El orden importa: la FK va de la hija a la madre."""
        return (TABLA, TABLA_VARIANTES)

    def cortes(self, conn: Connection) -> list[date]:
        """Los cortes OFICIALES, que son la ventana de esta cohorte.

        Se memoiza porque es un recorrido de los 82.9M hechos y la máquina la
        pide tres veces en una corrida (la ventana, la huella y el chequeo de
        obsolescencia). Una vez por proceso es suficiente: dentro de una
        corrida la ventana no puede cambiar.

        Args:
            conn: conexión de lectura.

        Returns:
            list[date]: cortes oficiales, del más viejo al más nuevo.
        """
        if self._ventana is None:
            minima = get_settings().GOLD_COBERTURA_CLEE_MINIMA
            # Recorre los 82.9M hechos contando `clee` por corte, asi que
            # tarda y hay que decir que esta pasando: sin esta linea el
            # arranque parece un cuelgue.
            self.logger.info(
                f"   ▶ midiendo la ventana oficial sobre {self.origen_schema}."
                f"establecimientos (cobertura minima de clee: {minima:.1%})..."
            )
            inicio = time.monotonic()
            with latido(conn, "ventana oficial", self.logger):
                self._ventana = list(
                    conn.execute(
                        text(_SQL_VENTANA_OFICIAL.format(schema=self.origen_schema, minima=minima))
                    )
                    .scalars()
                    .all()
                )
            segundos = time.monotonic() - inicio
            if self._ventana:
                self.logger.info(
                    f"   ✔ ventana oficial: {len(self._ventana)} cortes "
                    f"({self._ventana[0]} .. {self._ventana[-1]}) en {segundos:.1f}s"
                )
                self.logger.info("   cortes: " + ", ".join(c.isoformat() for c in self._ventana))
            else:
                self.logger.warning(
                    f"   ⚠️ ningun corte alcanza el {minima:.1%} de cobertura de clee "
                    f"(revisado en {segundos:.1f}s)"
                )
        return self._ventana

    def huella_origen(self, conn: Connection) -> str:
        """La ventana oficial, que es lo que define a esta cohorte.

        No `max(resuelto_en)`: Gold no lee ninguna tabla de identidad. Y una
        firma en vez de una fecha porque el cambio relevante va en los dos
        sentidos --un corte puede ENTRAR a la ventana, y también salir si
        INEGI corrige una entrega-- y un `>` sobre timestamps solo vería uno.
        """
        return "ventana=" + ",".join(c.isoformat() for c in self.cortes(conn))

    def obsoleto(self, conn: Connection, construido_en: datetime) -> bool:
        """Si la tabla vigente se construyó con otra ventana.

        La comparación es contra `ventana_cortes` de la propia tabla, que es
        exactamente para esto: la fila dice con qué cortes nació, así que
        decidir si sigue vigente no depende de ningún registro externo.

        `construido_en` no se usa --y el parámetro se queda porque es el
        contrato de la máquina--: aquí la pregunta no es *cuándo* se
        construyó sino *con qué*. Una tabla construida hace un año con la
        ventana vigente sigue siendo correcta; una de ayer con otra ventana,
        no.
        """
        guardada = conn.execute(
            text(f"SELECT ventana_cortes FROM {self.schema}.{self.tabla} LIMIT 1")
        ).scalar()
        if guardada is None:
            # Tabla vacia: no hay nada obsoleto que rehacer. La maquina ya
            # distingue este caso por el conteo de filas.
            return False
        return list(guardada) != self.cortes(conn)

    def contexto_meta(self, conn: Connection) -> dict:
        """La ventana y las reglas, que es lo que vuelve interpretable un conteo.

        `gold_cohorte_2021.md` §1.1 lo pide explícitamente: la ventana se
        calcula al arrancar y **se registra en el log y en `meta`**. Sin ella
        en `meta`, "¿por qué la corrida de marzo dio 5.1M y la de abril
        5.4M?" solo se contesta abriendo dos logs -- cuando la respuesta es
        que entró un corte.
        """
        ajustes = get_settings()
        ventana = self.cortes(conn)
        return {
            "ventana_cortes": [c.isoformat() for c in ventana],
            "n_cortes_oficiales": len(ventana),
            "cobertura_clee_minima": ajustes.GOLD_COBERTURA_CLEE_MINIMA,
            "metodo_base": METODO_BASE,
            "version_reglas": ajustes.GOLD_VERSION_REGLAS,
            "jitter_dispersion_m": ajustes.GOLD_JITTER_DISPERSION_M,
            "jitter_neto_m": ajustes.GOLD_JITTER_NETO_M,
        }

    @property
    def pista_vacio(self) -> str:
        """Lo que falta aquí es el ingest, no una resolución."""
        return (
            "Ningun corte alcanza la cobertura de clee exigida: corre el ingest "
            "de DENUE (`pipeline`) o revisa GOLD_COBERTURA_CLEE_MINIMA."
        )

    def validar(self, conn: Connection) -> list[str]:
        """Los invariantes de `gold_cohorte_2021.md` §5.1, contra los datos reales.

        Cada uno cubre una forma concreta de equivocarse en silencio, y
        ninguno levantaría un error por su cuenta: una tabla con una
        identidad sintética, o con dos canónicos para el mismo campo, se ve
        perfectamente bien hasta que alguien la cita.

        Uno de los siete de §5.1 **no se consulta**: "ninguna observación
        `(id, period)` contribuye a dos filas" está garantizado por
        construcción, porque `(id, period)` es la PK de
        `silver.establecimientos` --así que cada observación tiene UN `clee`--
        y el agregado es un `GROUP BY clee`. Una consulta ahí gastaría un
        recorrido de 51.5M filas para confirmar algo que el esquema ya impide.

        Args:
            conn: conexión de lectura, con las dos tablas ya escritas.

        Returns:
            list[str]: una línea por violación, con el conteo y un ejemplo.
        """
        madre = f"{self.schema}.{self.tabla}"
        hija = f"{self.schema}.{TABLA_VARIANTES}"
        n_cortes = len(self.cortes(conn))
        primer_corte = self.cortes(conn)[0]

        chequeos: list[tuple[str, str]] = [
            (
                "hay identidades sinteticas NEW_* en una cohorte que solo admite CLEE de INEGI",
                # STARTS_WITH y no LIKE: el `_` de `NEW_` es un comodin en LIKE.
                f"SELECT count(*), min(clee) FROM {madre} WHERE STARTS_WITH(clee, 'NEW_')",
            ),
            (
                f"hay negocios con mas cortes observados que los {n_cortes} de la ventana",
                f"SELECT count(*), min(clee) FROM {madre} WHERE cortes_observados > {n_cortes}",
            ),
            (
                "hay negocios cuya ventana_cortes no es la de esta corrida",
                f"SELECT count(*), min(clee) FROM {madre} "
                f"WHERE ventana_cortes <> {self._ventana_literal()}",
            ),
            (
                f"`presente_desde_el_inicio` no coincide con aparecer en {primer_corte}",
                f"SELECT count(*), min(clee) FROM {madre} "
                f"WHERE presente_desde_el_inicio <> (primer_periodo = DATE "
                f"'{primer_corte.isoformat()}')",
            ),
            (
                "hay variantes cuyo negocio no esta en la tabla madre",
                f"SELECT count(*), min(va.clee) FROM {hija} va "
                f"LEFT JOIN {madre} n USING (clee) WHERE n.clee IS NULL",
            ),
            (
                "hay (negocio, campo) con mas de un valor canonico",
                f"SELECT count(*), min(clee) FROM ("
                f"  SELECT clee, campo FROM {hija} WHERE es_canonico "
                f"  GROUP BY clee, campo HAVING count(*) > 1"
                f") d",
            ),
            (
                "la tabla madre publico un nombre que no esta marcado canonico en las variantes",
                f"SELECT count(*), min(n.clee) FROM {madre} n "
                f"WHERE n.nombre IS NOT NULL AND NOT EXISTS ("
                f"  SELECT 1 FROM {hija} va WHERE va.clee = n.clee "
                f"  AND va.campo = 'nom_estab' AND va.es_canonico "
                f"  AND va.valor = n.nombre)",
            ),
        ]

        violaciones = []
        for descripcion, sql in chequeos:
            cuantos, ejemplo = conn.execute(text(sql)).one()
            if cuantos:
                violaciones.append(f"{descripcion}: {cuantos:,} filas (p. ej. {ejemplo})")
        return violaciones

    # ----- las tres piezas que la maquina pide -----

    def sql_crear_obs(self) -> str:
        """DDL de la tabla de trabajo: los hechos de los cortes oficiales.

        Lleva los catálogos ya resueltos a texto y las coordenadas ya
        enriquecidas, para que la fase de agregados no vuelva a unir nada.

        `UNLOGGED` y no `TEMP` porque el troceo commitea cada corte y una
        temporal muere en el primer `COMMIT`. Vive en `gold` porque es suya y
        es transitoria; la máquina la borra al terminar.

        **No lleva columna `origen`**, al contrario de la de permanencia: en
        esta cohorte toda identidad es directa, así que la columna sería una
        constante en 51.5M filas.
        """
        return f"""
CREATE UNLOGGED TABLE IF NOT EXISTS {self.obs_schema}.{self.obs} (
    clee             text     NOT NULL,
    bloque           smallint NOT NULL,
    idx              integer  NOT NULL,
    period           date     NOT NULL,
    id               text     NOT NULL,
    nom_estab        text,
    raz_social       text,
    codigo_act       text,
    cve_ent          text,
    cve_mun          text,
    cve_loc          text,
    ageb             text,
    cod_postal       text,
    tipo_asent       text,
    latitud          double precision,
    longitud         double precision,
    h3_cell_9        bigint,
    fecha_alta_date  date,
    telefono         text,
    correoelec       text,
    www              text,
    nom_cencom       text,
    per_ocu          text,
    categoria_tamano text,
    empleados_min    integer,
    empleados_max    integer,
    tipounieco       text,
    direccion        text
)
"""

    def sql_llenar_obs(self, corte: date, idx: int, bloques: int) -> str:
        """Carga las observaciones OFICIALES de un corte.

        Dos filtros, y el segundo es de la cohorte y no una optimización: una
        fila sin `clee` dentro de un corte oficial (ese ≤0.1% que el umbral
        tolera) **no tiene identidad observada**, así que no entra. El negocio
        sí entra si aparece con `clee` en otro corte oficial
        (`gold_cohorte_2021.md` §2).

        El `idx` es la posición dentro de la VENTANA OFICIAL, no en la
        secuencia de 20 cortes. Es la línea que hace que `idx_primer = 0`
        signifique "estaba en el primer corte con CLEE".

        Args:
            corte: el corte a cargar.
            idx: su posición dentro de la ventana oficial.
            bloques: cuántos bloques repartir.

        Returns:
            str: `INSERT ... SELECT` de un corte.
        """
        schema = self.origen_schema
        literal = f"DATE '{corte.isoformat()}'"
        return f"""
INSERT INTO {self.obs_schema}.{self.obs} (
    clee, bloque, idx, period, id,
    nom_estab, raz_social, codigo_act,
    cve_ent, cve_mun, cve_loc, ageb, cod_postal, tipo_asent,
    latitud, longitud, h3_cell_9,
    fecha_alta_date, telefono, correoelec, www, nom_cencom,
    per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
    direccion
)
SELECT
    e.clee,
    {expr_bloque("e.clee", bloques)},
    {idx},
    e.period, e.id,
    e.nom_estab, e.raz_social, e.codigo_act,
    e.cve_ent, e.cve_mun, e.cve_loc, e.ageb, e.cod_postal, ta.tipo_asent,
    e.latitud, e.longitud, e.h3_cell_9,
    e.fecha_alta_date, e.telefono, e.correoelec, e.www, e.nom_cencom,
    ep.per_ocu, ep.categoria, ep.empleados_min, ep.empleados_max,
    tu.tipounieco,
    -- El domicilio como UNA cadena: arrays paralelos por componente
    -- perderian que numero va con que calle.
    NULLIF(CONCAT_WS(' | ',
        NULLIF(e.nom_vial, ''), NULLIF(e.numero_ext, ''),
        NULLIF(e.nomb_asent, ''), NULLIF(e.cod_postal, '')
    ), '') AS direccion
FROM {schema}.establecimientos e
LEFT JOIN {schema}.denue_tipos_asentamiento ta ON ta.id = e.tipo_asent_id
LEFT JOIN {schema}.denue_estratos_personal ep ON ep.id = e.per_ocu_id
LEFT JOIN {schema}.denue_tipos_unidad_economica tu ON tu.id = e.tipounieco_id
WHERE e.period = {literal}
  AND e.clee IS NOT NULL AND e.clee <> ''
"""

    def pasos_bloque(self, bloque: int, ultimo_idx: int) -> list[Paso]:
        """Los pasos que escriben un bloque de negocios y su evidencia.

        Cinco, y el orden no es negociable en dos puntos: `variantes` tiene
        que estar antes del nombre canónico (sale de ahí, por moda), y la
        tabla madre antes de la hija (la FK lo exige). Los cinco van en la
        misma transacción, así que un negocio y sus variantes quedan juntos o
        no quedan.

        Args:
            bloque: qué bloque procesar.
            ultimo_idx: posición del último corte oficial, para la censura.

        Returns:
            list[Paso]: pasos del bloque, en orden.
        """
        n_cortes = ultimo_idx + 1
        ventana_sql = self._ventana_literal()
        ajustes = get_settings()

        return [
            self._paso_ventana(bloque),
            self._paso_primera(bloque),
            self._paso_referencia(bloque),
            self._paso_variantes(bloque),
            self._paso_insert(ultimo_idx, n_cortes, ventana_sql, ajustes),
            self._paso_insert_variantes(),
        ]

    def _ventana_literal(self) -> str:
        """La ventana como literal SQL, para grabarla en cada fila.

        Sale del cache que llenó `cortes()`: cuando la máquina llega a los
        bloques ya la calculó, así que aquí no hace falta una conexión.
        """
        if not self._ventana:
            raise RuntimeError("la ventana oficial no se ha calculado; falta llamar a cortes()")
        fechas = ", ".join(f"DATE '{c.isoformat()}'" for c in self._ventana)
        return f"ARRAY[{fechas}]::date[]"

    def _paso_ventana(self, bloque: int) -> Paso:
        """Un solo `GROUP BY` con todo lo que es agregado sobre la vida.

        Incluye los "más reciente no vacío" de razón social y contacto, con
        `(ARRAY_AGG(... ORDER BY idx DESC) FILTER (...))[1]`: el array tiene a
        lo más tantos elementos como cortes (nueve), así que cuesta menos que
        un `DISTINCT ON` por campo, y el `FILTER` es lo que vuelve la regla
        "el más reciente **no vacío**" en vez de "el del último corte".
        """
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "ventana",
            "colapsando a una fila por negocio",
            f"""
CREATE TEMP TABLE ventana ON COMMIT DROP AS
SELECT
    clee,
    MIN(period) AS primer_periodo,
    MAX(period) AS ultimo_periodo,
    MIN(idx) AS idx_primer,
    MAX(idx) AS idx_ultimo,
    COUNT(DISTINCT idx) AS cortes_observados,
    MIN(fecha_alta_date) AS fecha_alta_date,
    ARRAY_AGG(DISTINCT idx ORDER BY idx) AS cortes_idx,
    -- Cuantas variantes hubo. Los valores estan en la tabla hija.
    COUNT(DISTINCT nom_estab) FILTER (
        WHERE nom_estab IS NOT NULL AND nom_estab <> '') AS n_nombres,
    COUNT(DISTINCT codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS n_actividades,
    COUNT(DISTINCT cve_ent || cve_mun) FILTER (
        WHERE cve_ent IS NOT NULL AND cve_mun IS NOT NULL) AS n_municipios,
    COUNT(DISTINCT direccion) FILTER (WHERE direccion IS NOT NULL) AS n_direcciones,
    COUNT(DISTINCT per_ocu) FILTER (
        WHERE per_ocu IS NOT NULL AND per_ocu <> '') AS n_estratos,
    -- Ancestro comun del SCIAN, por los extremos: el prefijo comun de un
    -- conjunto ordenado de cadenas es el prefijo comun del minimo y el
    -- maximo, asi que dos escalares bastan y no hay que materializar el
    -- conjunto. La decision de que nivel usar esta en el INSERT.
    MIN(codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS act_min,
    MAX(codigo_act) FILTER (
        WHERE codigo_act IS NOT NULL AND codigo_act <> '') AS act_max,
    -- El mas reciente NO VACIO de cada uno, independientemente. Que vengan
    -- de cortes distintos no es inconsistencia: aqui no se describe un
    -- lugar, se acumula una via de verificacion.
    (ARRAY_AGG(raz_social ORDER BY idx DESC) FILTER (
        WHERE raz_social IS NOT NULL AND raz_social <> ''))[1] AS raz_social,
    (ARRAY_AGG(telefono ORDER BY idx DESC) FILTER (
        WHERE telefono IS NOT NULL AND telefono <> ''))[1] AS telefono,
    (ARRAY_AGG(correoelec ORDER BY idx DESC) FILTER (
        WHERE correoelec IS NOT NULL AND correoelec <> ''))[1] AS correoelec,
    (ARRAY_AGG(www ORDER BY idx DESC) FILTER (
        WHERE www IS NOT NULL AND www <> ''))[1] AS www,
    -- Cobertura del dato: UNION sobre toda la vida, no el corte de
    -- referencia. Dice de que negocios se puede afirmar algo sobre cada
    -- campo, que es lo que decide si entran a un analisis que dependa de el.
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
    BOOL_OR(telefono IS NOT NULL AND telefono <> '') AS tiene_telefono,
    BOOL_OR(correoelec IS NOT NULL AND correoelec <> '') AS tiene_correo,
    BOOL_OR(www IS NOT NULL AND www <> '') AS tiene_sitio,
    BOOL_OR(nom_cencom IS NOT NULL AND nom_cencom <> '') AS en_centro_comercial,
    -- Rectangulo envolvente: cota SUPERIOR del movimiento, ciega al orden.
    MIN(latitud) AS lat_min, MAX(latitud) AS lat_max,
    MIN(longitud) AS lon_min, MAX(longitud) AS lon_max,
    COUNT(DISTINCT (latitud, longitud)) FILTER (
        WHERE latitud IS NOT NULL AND longitud IS NOT NULL) AS n_posiciones,
    -- Mediana de la posicion, para el caso de jitter de GPS. Se calcula
    -- siempre porque cuesta lo mismo dentro de este GROUP BY; cual se usa lo
    -- decide el INSERT.
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY latitud) AS lat_mediana,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY longitud) AS lon_mediana
FROM {obs}
WHERE bloque = {bloque}
GROUP BY clee
""",
        )

    def _paso_primera(self, bloque: int) -> Paso:
        """El extremo inicial del recorrido, para el desplazamiento y el delta."""
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "primera",
            "tomando el estado con que empezo cada negocio",
            f"""
-- Solo lo que hace falta para las medidas de trayectoria. Ninguno de estos
-- valores se guarda: se consumen aqui y lo que persiste es el escalar.
CREATE TEMP TABLE primera ON COMMIT DROP AS
SELECT DISTINCT ON (clee)
       clee, latitud AS lat_primera, longitud AS lon_primera,
       empleados_min AS empleados_min_primera
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee, period ASC, id
""",
        )

    def _paso_referencia(self, bloque: int) -> Paso:
        """El corte de referencia COMPLETO: el bloque de ubicación coherente.

        Es la pieza que `normalizacion_gold.md` §5 exige. Tomar el CP del
        corte más reciente que lo tenga, la colonia de otro y las coordenadas
        del último produce un domicilio **que nunca existió**: el CP de 2017
        con la colonia de 2019 y el punto de 2024. Si el corte de referencia
        tiene el campo vacío, el campo queda vacío --y `tiene_*` ya dice si
        existió en algún otro, que es la información honesta.

        Por eso es `DISTINCT ON` de una sola fila y no un agregado por campo:
        la coherencia la da que todo salga de la MISMA observación.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        return Paso(
            "referencia",
            "fijando el corte de referencia de cada negocio",
            f"""
CREATE TEMP TABLE referencia ON COMMIT DROP AS
SELECT DISTINCT ON (clee)
       clee,
       period AS corte_referencia,
       cve_ent, cve_mun, cve_loc, ageb, cod_postal, tipo_asent,
       latitud, longitud, h3_cell_9,
       per_ocu, categoria_tamano, empleados_min, empleados_max,
       tipounieco
FROM {obs}
WHERE bloque = {bloque}
ORDER BY clee, period DESC, id
""",
        )

    def _paso_variantes(self, bloque: int) -> Paso:
        """Los valores distintos por campo, con su ventana y su conteo.

        Un solo `GROUP BY` sobre el despivoteado de los seis campos. Las tres
        columnas de ventana y conteo salen del mismo agregado, así que son
        gratis.

        De aquí sale también el nombre canónico: la **moda** desempatada por
        el más reciente (`normalizacion_gold.md` §6.4, baseline 1). Es el
        único lugar donde se puede calcular, porque necesita el conteo por
        valor --que es exactamente lo que esta tabla tiene.
        """
        obs = f"{self.obs_schema}.{self.obs}"
        unpivot = "\n    UNION ALL\n".join(
            f"""    SELECT clee, '{campo}' AS campo, {campo} AS valor, period, idx
    FROM {obs} WHERE bloque = {bloque} AND {campo} IS NOT NULL AND {campo} <> ''"""
            for campo in CAMPOS_CON_VARIANTES
        )
        return Paso(
            "variantes",
            f"listando variantes de {len(CAMPOS_CON_VARIANTES)} campos",
            f"""
CREATE TEMP TABLE variantes ON COMMIT DROP AS
WITH crudas AS (
{unpivot}
)
SELECT clee, campo, valor,
       MIN(period) AS primer_periodo,
       MAX(period) AS ultimo_periodo,
       COUNT(DISTINCT idx) AS n_cortes,
       MAX(idx) AS ultimo_idx
FROM crudas
GROUP BY clee, campo, valor
""",
        )

    def _paso_insert(
        self, ultimo_idx: int, n_cortes: int, ventana_sql: str, ajustes: Settings
    ) -> Paso:
        """La fila del negocio: ventana, trayectoria, cobertura y canónicos."""
        dispersion = ajustes.GOLD_JITTER_DISPERSION_M
        neto_max = ajustes.GOLD_JITTER_NETO_M
        version = ajustes.GOLD_VERSION_REGLAS

        # El desplazamiento neto se usa dos veces --como columna y como
        # criterio del jitter-- asi que se escribe una vez y se nombra.
        neto = f"""CASE WHEN p.lat_primera IS NULL OR r.latitud IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(r.latitud - p.lat_primera) / 2), 2)
            + COS(RADIANS(p.lat_primera)) * COS(RADIANS(r.latitud))
            * POWER(SIN(RADIANS(r.longitud - p.lon_primera) / 2), 2)
        ))
    END"""
        dispersion_expr = f"""CASE WHEN v.lat_min IS NULL THEN NULL ELSE
        2 * {_EARTH_RADIUS_M} * ASIN(SQRT(
            POWER(SIN(RADIANS(v.lat_max - v.lat_min) / 2), 2)
            + COS(RADIANS(v.lat_min)) * COS(RADIANS(v.lat_max))
            * POWER(SIN(RADIANS(v.lon_max - v.lon_min) / 2), 2)
        ))
    END"""

        return Paso(
            TABLA,
            f"escribiendo el bloque en {self.schema}.{TABLA}",
            f"""
INSERT INTO {self.schema}.{TABLA} (
    clee,
    primer_periodo, ultimo_periodo, idx_primer, idx_ultimo,
    cortes_observados, huecos, cortes_tras_ultimo,
    duracion_periodos, presente_desde_el_inicio, cortes_idx,
    fecha_alta_date, dias_alta_a_primera,
    cambio_tamano, delta_empleados_min,
    n_nombres, n_direcciones, cambio_actividad, cambio_municipio,
    n_posiciones, desplazamiento_neto_m,
    lat_min, lat_max, lon_min, lon_max, dispersion_max_m,
    tiene_nombre, tiene_razon_social, tiene_actividad, tiene_municipio,
    tiene_localidad, tiene_ageb, tiene_cod_postal, tiene_tipo_asent,
    tiene_coordenadas, tiene_estrato_personal, tiene_tipounieco,
    tiene_telefono, tiene_correo, tiene_sitio, en_centro_comercial,
    nombre, razon_social, codigo_act,
    corte_referencia, cve_ent, cve_mun, cve_loc, ageb, cod_postal, tipo_asent,
    latitud, longitud, h3_cell_9,
    per_ocu, categoria_tamano, empleados_min, empleados_max, tipounieco,
    telefono, correoelec, www,
    cluster_sospechoso,
    ventana_cortes, metodo_base, version_reglas
)
SELECT
    v.clee,
    v.primer_periodo, v.ultimo_periodo, v.idx_primer, v.idx_ultimo,
    v.cortes_observados,
    (v.idx_ultimo - v.idx_primer + 1) - v.cortes_observados AS huecos,
    {ultimo_idx} - v.idx_ultimo AS cortes_tras_ultimo,
    v.idx_ultimo - v.idx_primer AS duracion_periodos,
    -- Censura por la izquierda, RELATIVA A LA VENTANA OFICIAL: idx 0 es el
    -- primer corte con CLEE, no el primer corte del DENUE.
    v.idx_primer = 0 AS presente_desde_el_inicio,
    v.cortes_idx,
    v.fecha_alta_date,
    CASE WHEN v.fecha_alta_date IS NULL THEN NULL
         ELSE (v.primer_periodo - v.fecha_alta_date) END AS dias_alta_a_primera,
    v.n_estratos > 1 AS cambio_tamano,
    r.empleados_min - p.empleados_min_primera AS delta_empleados_min,
    v.n_nombres, v.n_direcciones,
    v.n_actividades > 1 AS cambio_actividad,
    v.n_municipios > 1 AS cambio_municipio,
    COALESCE(v.n_posiciones, 0) AS n_posiciones,
    {neto} AS desplazamiento_neto_m,
    v.lat_min, v.lat_max, v.lon_min, v.lon_max,
    {dispersion_expr} AS dispersion_max_m,
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
    -- NOMBRE CANONICO: la moda, desempatada por el mas reciente. Es el
    -- baseline obligatorio de normalizacion_gold.md §6.4; un LLM solo lo
    -- reemplaza si gana en una muestra etiquetada a mano.
    -- Los clusters sospechosos se quedan SIN nombre a proposito: con
    -- [TACOS PACO, FARMACIA PACO] cualquier canonico es una afirmacion
    -- falsa, y las variantes siguen ahi para mirarlas.
    CASE WHEN v.n_nombres > {n_cortes} THEN NULL ELSE nom.valor END AS nombre,
    v.raz_social,
    -- SCIAN CANONICO: el ancestro comun mas especifico, por prefijo. La
    -- jerarquia ES el codigo (sector=2, subsector=3, rama=4), asi que no
    -- hace falta unir el catalogo. Si la discrepancia cruza rama queda NULL:
    -- es una decision de juicio, y un valor inventado seria peor que la
    -- ausencia. `tiene_actividad` distingue ese NULL del de "nunca tuvo".
    CASE
        WHEN v.act_min IS NULL THEN NULL
        WHEN v.act_min = v.act_max THEN v.act_min
        WHEN LEFT(v.act_min, 5) = LEFT(v.act_max, 5) THEN LEFT(v.act_min, 5)
        WHEN LEFT(v.act_min, 4) = LEFT(v.act_max, 4) THEN LEFT(v.act_min, 4)
        ELSE NULL
    END AS codigo_act,
    -- BLOQUE DE UBICACION: todo del MISMO corte (§5). La unica excepcion son
    -- las coordenadas cuando hay jitter de GPS: dispersion grande con neto
    -- casi nulo significa que el ultimo punto es malo y la mediana es el
    -- local. Las llaves geograficas NO se tocan en ese caso -- cambiarlas
    -- rompería la coherencia que esta regla existe para proteger.
    r.corte_referencia,
    r.cve_ent, r.cve_mun, r.cve_loc, r.ageb, r.cod_postal, r.tipo_asent,
    CASE WHEN {dispersion_expr} > {dispersion}
          AND COALESCE({neto}, 0) < {neto_max}
         THEN v.lat_mediana ELSE r.latitud END AS latitud,
    CASE WHEN {dispersion_expr} > {dispersion}
          AND COALESCE({neto}, 0) < {neto_max}
         THEN v.lon_mediana ELSE r.longitud END AS longitud,
    -- La celda H3 se queda la del corte de referencia incluso con jitter: es
    -- un dato PRE-CALCULADO en los hechos y recalcularlo aqui costaria una
    -- llamada por fila. A res 9 (~175 m de arista) el jitter rara vez cruza
    -- de celda, y si cruza, lat/lon de la misma fila lo delatan.
    r.h3_cell_9,
    r.per_ocu, r.categoria_tamano, r.empleados_min, r.empleados_max,
    r.tipounieco,
    v.telefono, v.correoelec, v.www,
    -- IMPOSIBLE para un cluster correcto: un establecimiento aparece a lo
    -- mas una vez por corte, asi que mas nombres que cortes significa que la
    -- identidad agrupo mas de un establecimiento.
    v.n_nombres > {n_cortes} AS cluster_sospechoso,
    {ventana_sql}, '{METODO_BASE}', '{version}'
FROM ventana v
JOIN primera p USING (clee)
JOIN referencia r USING (clee)
-- La moda del nombre: el valor con mas cortes, desempatado por el mas
-- reciente. LEFT porque un negocio puede no haber tenido nunca nombre.
LEFT JOIN LATERAL (
    SELECT valor FROM variantes
    WHERE variantes.clee = v.clee AND campo = 'nom_estab'
    ORDER BY n_cortes DESC, ultimo_idx DESC, valor
    LIMIT 1
) nom ON TRUE
""",
        )

    def _paso_insert_variantes(self) -> Paso:
        """La evidencia, con `es_canonico` apuntando al valor que publicó la madre.

        `es_canonico` se decide comparando contra la fila ya escrita, no
        recalculando la regla: si se recalculara, un cambio en una de las dos
        copias dejaría la tabla afirmando que el canónico es un valor que la
        madre no publicó. Así, por construcción, no pueden discrepar.

        El índice único parcial `(clee, campo) WHERE es_canonico` es lo que
        vuelve estructural el "a lo más uno": si esta consulta marcara dos, el
        `INSERT` falla aquí en vez de dejar una tabla indefendible.
        """
        casos = "\n        ".join(
            f"WHEN va.campo = '{campo}' THEN va.valor IS NOT DISTINCT FROM n.{COLUMNA_CANONICA[campo]}"
            for campo in CAMPOS_CON_VARIANTES
        )
        return Paso(
            TABLA_VARIANTES,
            f"escribiendo la evidencia en {self.schema}.{TABLA_VARIANTES}",
            f"""
INSERT INTO {self.schema}.{TABLA_VARIANTES} (
    clee, campo, valor, primer_periodo, ultimo_periodo, n_cortes, es_canonico
)
SELECT va.clee, va.campo, va.valor,
       va.primer_periodo, va.ultimo_periodo, va.n_cortes,
       CASE
        {casos}
        ELSE FALSE
       END AS es_canonico
FROM variantes va
JOIN {self.schema}.{TABLA} n ON n.clee = va.clee
""",
        )


def construir_cohorte_oficial(
    engine: Engine,
    tracker: MetadataTracker,
    schema: str = DEFAULT_SCHEMA,
    force: bool = False,
) -> int:
    """Construye la cohorte oficial de Gold y la registra en `meta`.

    Punto de entrada del CLI. Qué decide la máquina --reanudar, saltarse el
    trabajo hecho, o reconstruir porque la ventana cambió-- está en
    `troceado.construir`.

    Lo que descarta al reconstruir es solo lo suyo: su tabla de trabajo y las
    dos tablas de `gold`. Los hechos de Silver, las `clee_<metodo>`, las
    `permanencia_<metodo>` y `meta` quedan intactos.

    Args:
        engine: engine con permisos de escritura; la máquina controla sus
            transacciones, una por vuelta.
        tracker: registro de `meta` donde queda la corrida.
        schema: schema de ORIGEN (los hechos); el destino es `gold`.
        force: si True, descarta lo que haya y reconstruye entero.

    Returns:
        int: negocios en la cohorte al terminar.
    """
    return construir(engine, CohorteOficial(schema), tracker, force)
