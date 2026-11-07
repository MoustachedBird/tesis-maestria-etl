# TODO — Normalización de datos para `gold.establecimientos`

**Estado:** propuesto, nada implementado. Escrito el 2026-11-06 antes de
tocar código, igual que `crear_agentes.md`.

**Qué entrega:** las reglas con que una fila de `gold.establecimientos`
—un negocio, su ventana de vida y sus atributos canónicos— se construye a
partir de `silver.permanencia_<metodo>`, `silver.clee_<metodo>` y los
hechos; la tabla `gold.variantes` con los alias de cada negocio (§6); y el
diseño de un reconciliador LLM que adjudica **solo** donde los métodos
radio y H3 no coinciden.

**La tesis del documento, en una línea:** el 95% de esto son reglas
deterministas, el agente adjudica una franja que **todavía no está medida**,
y el paso 1 es medirla.

---

## 0. Lo que ya está decidido en otros documentos

No se re-litiga aquí; se cita.

| Decisión | Dónde |
|---|---|
| `gold.establecimientos` = una fila por **negocio** (~10.45M) frente a 82.9M observaciones en `silver`. Mismo nombre, grano distinto, con tres salvaguardas contra confundir la N | `arquitectura.md` §2.3.1 |
| `silver.permanencia_<m>` no guarda el valor de ningún corte: agregados de trayectoria + banderas `tiene_*`, y el valor sale del join | `AGENTS.md` §N3, migración `b0007` |
| El contrato de todo agente (`Decision`, `Evidencia`, `meta.agent_decisions` como caché/auditoría/reproducibilidad) y la estructura `src/agents/` | `crear_agentes.md` §9 |
| La regla de cierre es **preliminar** y se aplica en la consulta, no en el esquema de Silver | `AGENTS.md` §N3 |
| El API lee `gold` con el rol `api_ro`, que no tiene `GRANT` en `silver` | `arquitectura.md` §2.3 |

### 0.1 Gold sí materializa valores resueltos, y no contradice `b0007`

`b0007` sacó de Silver el valor "del corte más reciente" porque esas tablas
son **derivadas y reconstruibles**: cualquier valor congelado ahí es ancho
desperdiciado que el join entrega mejor.

Gold es lo contrario por definición: es la **capa citable**. Una fila de
`gold.establecimientos` es el artefacto que la tesis reporta, y tiene que
ser estable aunque la identidad se re-resuelva después. Por eso aquí sí se
materializa un valor por campo —y por eso cada fila lleva fecha, versión de
reglas y de qué método salió.

La frontera queda así:

```
silver   reconstruible a voluntad   →  agregados + cobertura, sin snapshots
gold     congelado y fechado        →  un valor canónico por campo, con procedencia
```

---

## 1. PASO 1, y bloquea todo lo demás: medir el desacuerdo

El reconciliador LLM existe para resolver casos como el que motivó este
documento:

```
H3    ligó 3 observaciones:  "tacos don paco", "tacos paco", "farmacia paco"
RADIO ligó 4 observaciones:  "tacos paco", "taqueria don paco",
                             "taquitos paquito", "tacos don paco"
```

**Pero hoy no se sabe si eso pasa, ni cuántas veces.** `AGENTS.md` afirma
que los dos métodos "por construcción DEBEN dar asignaciones idénticas
(mismo radio de 500m, misma cascada)" y `tests/unit/test_h3_candidates.py`
verifica paridad de candidatos con 0 diferencias. Si la paridad se sostiene
a nivel de *partición*, el desacuerdo es ~0 y **el reconciliador no tiene
trabajo que hacer**.

`scripts/clee_propagation_stats.py` ya reporta `tasa_acuerdo_semantico`
(idénticas + ambas sintéticas), pero eso compara **valores de identidad**
fila a fila. No es la pregunta: lo que importa es si las dos **particiones**
del conjunto de observaciones coinciden.

### 1.1 La consulta que falta

**Precondición:** las dos tablas tienen que estar resueltas. `clee_h3` es el
track de comparación y cuesta otra noche; si solo se corrió `clee-radio`, no
hay nada que comparar y esto no aplica todavía.

```sql
-- 0 particiones en una de las dos = ese metodo no se ha resuelto
SELECT parent.relname AS tabla, count(*) AS particiones
FROM pg_inherits i
JOIN pg_class child  ON child.oid  = i.inhrelid
JOIN pg_class parent ON parent.oid = i.inhparent
WHERE parent.relname IN ('clee_radio', 'clee_h3')
GROUP BY 1;
```

La consulta completa vive en **`scripts/sql/desacuerdo_particiones.sql`**
y se corre redirigiendo desde el host (no pegándola: SQL largo en terminal
se corrompe):

```bash
docker compose exec -T postgres psql -U etl_rw -d mipymes \
  < scripts/sql/desacuerdo_particiones.sql 2>&1 | tee desacuerdo.log
```

La pasada cara va **una sola vez** a una temporal; de ahí salen todas las
preguntas sin volver a tocar los 82.9M:

```sql
-- SIN paralelismo, a proposito: la memoria compartida de una consulta
-- paralela es ~ workers x work_mem y tiene que caber en PG_SHM_SIZE. En 0
-- workers, work_mem es memoria PRIVADA y no toca la shm. Ver el encabezado
-- del .sql para la aritmetica y el incidente que lo motivo.
SET max_parallel_workers_per_gather = 0;
SET work_mem = '2GB';

CREATE TEMP TABLE pares AS
SELECT r.clee_resuelto AS c_radio,
       h.clee_resuelto AS c_h3,
       count(*)        AS obs
FROM silver.clee_radio r
JOIN silver.clee_h3 h USING (id, period)
GROUP BY 1, 2;

CREATE INDEX ON pares (c_radio);
CREATE INDEX ON pares (c_h3);
ANALYZE pares;
```

El join es **partition-wise** —las dos tablas están particionadas por
`period` y `period` está en las dos PK— así que son 20 joins de ~4M y no uno
de 82.9M × 82.9M (`docs/separacion_ingest_clee.md` §3).

```sql
-- El veredicto. Un cluster de radio que mapea a >1 de h3 = radio unio lo
-- que h3 separo, y al reves.
WITH grados AS (
    SELECT c_radio, c_h3, obs,
           count(*) OVER (PARTITION BY c_radio) AS h3_por_radio,
           count(*) OVER (PARTITION BY c_h3)    AS radio_por_h3
    FROM pares
)
SELECT
    sum(obs)                                                 AS obs_comparadas,
    count(DISTINCT c_radio)                                  AS clusters_radio,
    count(DISTINCT c_h3)                                     AS clusters_h3,
    count(DISTINCT c_radio) FILTER (WHERE h3_por_radio > 1)  AS radio_unio_mas,
    count(DISTINCT c_h3)    FILTER (WHERE radio_por_h3 > 1)  AS h3_unio_mas,
    sum(obs) FILTER (WHERE h3_por_radio > 1
                        OR radio_por_h3 > 1)                 AS obs_en_disputa,
    round(100.0 * sum(obs) FILTER (WHERE h3_por_radio > 1
                                      OR radio_por_h3 > 1)
          / nullif(sum(obs), 0), 4)                          AS pct_disputa
FROM grados;
```

`count(DISTINCT c_radio)` y no `count(*)`: un cluster de radio partido en
tres de h3 aporta tres filas a `pares`, y contar filas lo contaría tres
veces.

**Ojo con el `JOIN` interno**: una observación que está en una tabla y no en
la otra desaparece sin aviso, y eso también es desacuerdo. `obs_comparadas`
tiene que dar 82,896,801; si da menos, falta resolver algún corte en uno de
los dos métodos y hay que arreglar eso antes de interpretar el resto.

Y los casos concretos, que es lo que vuelve el número inspeccionable:

```sql
-- Los clusters en disputa mas grandes, con los nombres que contienen
WITH disputados AS (
    SELECT c_radio FROM pares GROUP BY 1 HAVING count(*) > 1
    ORDER BY sum(obs) DESC LIMIT 10
)
SELECT r.clee_resuelto AS c_radio, h.clee_resuelto AS c_h3,
       e.period, e.nom_estab, e.codigo_act, e.latitud, e.longitud
FROM silver.clee_radio r
JOIN silver.clee_h3 h USING (id, period)
JOIN silver.establecimientos e ON e.id = r.id AND e.period = r.period
WHERE r.clee_resuelto IN (SELECT c_radio FROM disputados)
ORDER BY r.clee_resuelto, e.period;
```

Si eso devuelve el patrón del ejemplo —una taquería con tres nombres y una
farmacia colada— el reconciliador tiene caso. Si devuelve 0 filas, no lo
tiene.

Todo esto va a `scripts/` como subcomando (`clee_propagation_stats.py
particiones`), no como consulta suelta: es una métrica que hay que poder
repetir después de cada re-resolución.

### 1.2 El número decide si §3 se construye

| `obs_en_disputa` / 82.9M | Qué hacer |
|---|---|
| **< 0.1%** | El reconciliador **no se construye**. Se elige `radio` como método base, se documenta la cifra como evidencia de robustez, y Gold es 100% determinista. Es el resultado más probable dado el test de paridad |
| **0.1% – 5%** | Se construye, acotado a esa franja. La aritmética de §10 aplica |
| **> 5%** | **Hay un bug de paridad**, no un caso de uso para un LLM. Se investiga `find_h3_neighbors` vs `find_spatial_neighbors` antes de escribir una línea de agente |

Escribir el agente antes de este número es construir sobre una suposición.

---

## 2. El desacuerdo tiene tres formas, y solo una necesita LLM

Cuando dos clusters no se corresponden 1:1, la relación entre sus conjuntos
de observaciones es una de estas:

| Forma | Qué pasó | Cómo se resuelve |
|---|---|---|
| **Contención** (`A ⊃ B`) | Un método encontró un match que al otro se le escapó, típicamente por una frontera de blocking | **Regla**: gana el superconjunto si cada observación extra pasa la cascada con score ≥ el umbral de match directo. No hay ambigüedad: un método vio menos |
| **Disjuntos con empate** | Los dos partieron igual pero asignaron ids distintos (`NEW_*` de métodos distintos) | **Regla**: son el mismo cluster. Es contabilidad, no decisión |
| **Solapamiento parcial** (`A ∩ B ≠ ∅`, ninguno contiene al otro) | Los métodos discrepan sobre **qué negocio es cuál**. Es el caso del ejemplo | **LLM**: aquí sí hay que juzgar si "farmacia paco" pertenece |

El trabajo del agente es solo el tercer renglón, y la regla del segundo y
tercero hay que aplicarla **antes** de llamar al modelo. Sin esta cascada,
el agente recibiría cientos de miles de casos mecánicos y se pagaría
latencia por decisiones que un `WHERE` resuelve.

---

## 3. El reconciliador: qué decide y qué no

Es un **clasificador de un paso sin herramientas**, igual en forma al
adjudicador CLEE de `crear_agentes.md` §9.1: los datos van en el prompt, los
pone el código, y el determinismo es requisito. Reusa `src/shared/llm/`
(cliente, schema, caché, presupuesto) y el contrato `Decision`.

**No es el adjudicador CLEE.** Aquél decide, durante la resolución, cuál
*candidato* es el mismo negocio y escribe `silver.clee_llm` como un tercer
método. Éste opera aguas abajo, sobre *clusters ya formados*, y no escribe
en Silver. Si el adjudicador CLEE llega a existir, Gold tendrá **tres**
particiones que reconciliar en vez de dos y la decisión de §12.1 cambia.

### 3.1 Entrada

La unión de las observaciones de los dos clusters en disputa, cada una con
los campos que sostienen el juicio y nada más:

```
period      nom_estab              raz_social   codigo_act  lat/lon         cve_mun  dist_m
2019-11-01  TACOS DON PACO         —            722513      20.670,-103.35  14039    0
2020-04-01  TACOS PACO             —            722513      20.671,-103.35  14039    110
2020-11-01  TAQUERIA DON PACO      —            722513      20.670,-103.35  14039    15
2021-05-01  TAQUITOS PAQUITO       —            722513      20.672,-103.35  14039    190
2021-05-01  FARMACIA PACO          —            464111      20.669,-103.35  14039    95
```

`dist_m` respecto a la primera observación, y el `codigo_act` con su
etiqueta SCIAN resuelta. Un cluster en disputa son unidades, no miles: el
prompt cabe de sobra.

### 3.2 Salida

```python
class Reconciliacion(BaseModel):
    """Veredicto del reconciliador sobre un cluster en disputa."""
    miembros: list[tuple[str, date]]   # (id, period) que SÍ son el mismo negocio
    excluidos: list[tuple[str, date]]  # los que no, y van a su propio negocio
    nombre_canonico: str
    codigo_act: str                    # el SCIAN mas apropiado del conjunto
    confianza: float
    justificacion: str                 # una linea, para la auditoria
```

El ejemplo tiene que resolverse así, y es el primer caso de prueba:
`FARMACIA PACO` queda en `excluidos` —mismo apellido, actividad
incompatible (venta al menudeo de farmacia vs preparación de alimentos) y
el nombre comercial no comparte el núcleo— y las otras cuatro quedan como
un negocio llamado, digamos, `TACOS DON PACO`.

### 3.3 Lo que el agente NO decide

Nada que una regla pueda decidir. El modelo no ve ni opina sobre
`fecha_alta` (es un `MIN`), ni sobre el estrato de personal (es el último),
ni sobre las coordenadas. Dos razones: cada campo que el LLM toca es un
campo que deja de ser reproducible por inspección, y el costo por decisión
se multiplica por el número de campos.

---

## 4. Normalización campo por campo

El **corte de referencia** es el último en que se observó al negocio (el que
`permanencia.idx_ultimo` identifica). Varias reglas apuntan ahí.

| Campo en `gold` | Regla | Por qué | ¿LLM? |
|---|---|---|---|
| `clee` | La identidad del método base; si hubo reconciliación, la del cluster ganador | Es la llave del negocio | — |
| `nombre` | Canónico a partir de todas las variantes, que quedan listadas en `gold.variantes` (§6) | Ninguna variante suelta representa al negocio, y el canónico sin sus fuentes al lado no es auditable | **Sí**, con baseline obligatorio |
| `razon_social` | La más reciente no vacía; las anteriores a `gold.variantes` | Dato legal: no se promedia ni se inventa. Si cambió, cambió — y las dos sirven para buscarlo en fuentes oficiales | — |
| `codigo_act` | **Ancestro común más específico** en la jerarquía SCIAN; si eso cae por encima de `rama`, el LLM elige | `denue_actividades_economicas` ya trae `sector`/`subsector`/`rama`: dos actividades de la misma rama tienen una respuesta determinista y correcta. El LLM solo hace falta cuando la discrepancia cruza ramas, que es justo cuando hay que juzgar | Solo si cruza rama |
| `per_ocu`, `categoria_tamano`, `empleados_min/max` | Del corte de referencia | Pedido explícito: el último dato. Es el tamaño con que el negocio terminó, que es lo que el protocolo mide | — |
| `fecha_alta` | `MIN` sobre todos los cortes | Pedido explícito. Ya está en `permanencia.fecha_alta_date` | — |
| `latitud`, `longitud`, `h3_cell_9`, `h3_cell_5` | Del corte de referencia. **Pero** si `dispersion_max_m` supera el umbral y `desplazamiento_neto_m` no, usar la **mediana** | Pedido explícito el último dato, y es el correcto para quien se mudó. La excepción es el jitter de GPS, que `permanencia` ya sabe detectar (neto ≈ 0 con dispersión grande): ahí el último dato es una coordenada mala y la mediana es el local | — |
| `cve_ent`, `cve_mun`, `cve_loc`, `ageb`, `cod_postal`, `tipo_asent`, domicilio | **Del mismo corte que las coordenadas** | Ver §5: es coherencia, no una regla por campo | — |
| `tipounieco` | Del corte de referencia | Fijo / semifijo / ambulante. Si varió, `cambio_tipounieco` lo dice | — |
| `telefono`, `correoelec`, `www` | El más reciente no vacío de cada uno, **independientemente** | Para verificar un cierre sirve cualquiera que haya publicado; que vengan de cortes distintos no es inconsistencia, es cobertura | — |
| Ventana y censura (`primer_periodo`, `duracion_periodos`, `cortes_tras_ultimo`, `huecos`, `presente_desde_el_inicio`, `cortes_idx`) | Copia de `permanencia_<metodo>` | Ya están calculados y son el insumo del modelo | — |
| Trayectoria (`cambio_municipio`, `cambio_actividad`, `delta_empleados_min`, `n_nombres`, `desplazamiento_neto_m`, `dispersion_max_m`) | Copia de `permanencia_<metodo>` | Idem | — |
| Cobertura (`tiene_*`) | Copia de `permanencia_<metodo>` | Es el filtro que decide qué negocios entran a cada análisis | — |
| `es_sintetica`, `cortes_directo`, `cortes_propagado` | Copia | Confianza en la identidad: un negocio con 1 corte directo y 9 propagados no vale lo mismo que uno con 10 directos | — |
| `estatus` / `cerrado` | **No se materializa todavía** | La regla sigue siendo preliminar (`AGENTS.md` §N3). Gold la aplicará cuando esté validada, y ese es el único cambio que falta para que Gold sea "Silver + veredicto" | — |

**Si reconciliar cambia el cluster**, los campos de ventana y trayectoria
hay que **recalcularlos**, no copiarlos: una observación que se va o que
llega cambia `primer_periodo`, `huecos`, `n_nombres` y el rectángulo. Es la
razón por la que el reconciliador tiene que correr **antes** de construir
la fila de Gold, no después.

---

## 5. Coherencia: un domicilio no se arma por partes

Tomar `cod_postal` del corte más reciente que lo tenga, `tipo_asent` del más
reciente que lo tenga y las coordenadas del último corte produce un
domicilio **que nunca existió**: el CP de 2017 con la colonia de 2019 y el
punto de 2024.

Regla: **un solo corte de referencia para todo el bloque de ubicación**
(coordenadas, celdas H3, entidad, municipio, localidad, AGEB, CP, tipo de
asentamiento, vialidad, número). Si ese corte tiene el campo vacío, el campo
queda vacío — y `tiene_*` de `permanencia` ya dice si existió en algún otro,
que es la información honesta.

La excepción son los campos de contacto, y es deliberada: ahí no se está
describiendo un lugar, se está acumulando una vía de verificación.

---

## 6. El nombre canónico y la lista de alias

### 6.0 Sí: los alias se listan, y en una tabla aparte

Una lista de alias **no contradice `b0007`**: es un agregado sobre la vida
completa (`ARRAY_AGG(DISTINCT nom_estab)`), no el valor de un corte. Y es
justo el tipo de dato que Gold debe materializar, por tres razones
concretas:

1. **Es la evidencia del nombre canónico.** Si Gold dice que el negocio se
   llama `TACOS DON PACO`, quien lea la fila tiene que poder ver de qué
   variantes salió sin irse al join. Lo mismo que `meta.agent_decisions`
   hace con las decisiones del agente, pero para el 99% de las filas que
   ningún agente tocó.
2. **Es el mejor diagnóstico de un mal cluster que existe.** Un lector que
   ve `[TACOS PACO, FARMACIA PACO]` sospecha la fusión indebida al
   instante. `n_nombres = 2` no dice nada; los dos nombres lo dicen todo.
   Para una tesis cuyo riesgo principal es la calidad de la resolución de
   identidad, esa columna vale más que varias métricas.
3. **El verificador de supervivencia los necesita todos.** Buscar en Google
   Places si un negocio cerró funciona con `TAQUERIA DON PACO` y falla con
   `TACOS PACO`; la cascada de `crear_agentes.md` §11.6 debe probar cada
   alias, no solo el canónico.

**Pero no como columna `text[]`, sino como tabla de búsqueda:**

```sql
CREATE TABLE gold.variantes (
    clee            text  NOT NULL,   -- FK logica a gold.establecimientos
    campo           text  NOT NULL,   -- 'nom_estab' | 'raz_social' | ...
    valor           text  NOT NULL,
    primer_periodo  date  NOT NULL,   -- desde cuando uso este valor
    ultimo_periodo  date  NOT NULL,   -- hasta cuando
    cortes          integer NOT NULL, -- en cuantos cortes aparecio
    PRIMARY KEY (clee, campo, valor)
);
CREATE INDEX ix_variantes_valor ON gold.variantes USING gin (valor gin_trgm_ops);
```

Tres cosas que un array no da:

- **Búsqueda difusa.** Con el índice trigram, "¿qué negocio es *tacos
  paco*?" es una consulta. Es la pregunta que se hace al verificar un caso
  a mano o al cruzar con una fuente externa, y con `text[]` no tiene
  respuesta razonable.
- **La cronología del rebranding.** `primer_periodo`/`ultimo_periodo` por
  variante da la secuencia: `TACOS PACO (2019–2020) → TAQUERIA DON PACO
  (2021–)`. Sale del mismo `GROUP BY`, así que es gratis, y el rebranding
  como señal de supervivencia es una pregunta que la tesis puede hacerse.
- **Un solo lugar para todos los campos de variante**, con el discriminador
  `campo`, en vez de una columna de array por campo.

Es, materializada y buscable, la sucesora de las vistas
`variantes_<metodo>` que `b0006` eliminó: aquéllas eran una consulta
guardada sobre Silver sin consumidor; ésta es un artefacto congelado de la
capa citable, con índice.

### 6.1 Qué campos entran y cuáles no

> **Una variante entra a `gold.variantes` si es la evidencia de una decisión
> que Gold tomó, o el insumo de un consumidor que trabaja negocio por
> negocio. Lo demás se queda en el join.**

| Campo | ¿Entra? | Por qué |
|---|---|---|
| `nom_estab` | **Sí** | Evidencia del nombre canónico + lo que busca el verificador |
| `raz_social` | **Sí** | Identifica al negocio en fuentes oficiales (RFC, actas), y es otra cosa que el nombre comercial |
| `codigo_act` | **Sí** | Evidencia de la elección de SCIAN (§4). Son 1–2 variantes por negocio: barato |
| `telefono`, `correoelec`, `www` | **Sí** | La cascada de verificación prueba todos. `tiene_telefono` solo dice que existió alguno |
| domicilio por componentes | No | Ya hay `n_direcciones`; y el domicilio canónico sale del corte de referencia (§5) |
| coordenadas | No | El rectángulo envolvente, el neto y la dispersión ya responden lo que se pregunta de ellas |
| `per_ocu` / estrato | No | `cambio_tamano` y `delta_empleados_min` ya dan la trayectoria, que es la pregunta |
| `tipo_asent`, `cod_postal`, `cve_loc`, `ageb` | No | Variantes sin consumidor; es el crecimiento de GB que `b0007` evitó |

### 6.2 El tamaño se mide, no se estima

El número de filas de `gold.variantes` para los nombres es exactamente
`SUM(n_nombres)` sobre la tabla de permanencia, que ya está calculado:

```sql
SELECT sum(n_nombres)                        AS filas_nom_estab,
       round(avg(n_nombres), 2)              AS nombres_por_negocio,
       count(*) FILTER (WHERE n_nombres = 1) AS con_un_solo_nombre
FROM silver.permanencia_radio;
```

Si la mayoría de negocios tiene un solo nombre —lo esperable—, la tabla es
del orden de 12–16M filas angostas para los cuatro campos de §6.1, y la
decisión es trivial. Si `nombres_por_negocio` sale alto, es señal de otra
cosa: clusters demasiado grandes, y eso hay que mirarlo antes de
materializar nada.

### 6.3 Dependencia nueva: `pg_trgm`

El índice difuso necesita `CREATE EXTENSION IF NOT EXISTS pg_trgm`, que hoy
el proyecto no usa (no hay una sola extensión instalada). Viene en el
contrib de la imagen oficial `postgres:17`, así que es una línea en la
migración de Gold y ningún cambio de infraestructura — pero queda anotado
como la primera extensión del esquema.

### 6.4 El nombre canónico tiene que vencer a un baseline

"Un promedio de todos los nombres" es la parte más atractiva del agente y la
más fácil de no justificar. Hay dos baselines deterministas:

1. **Moda**, desempatada por el más reciente. Para `TACOS PACO` ×3 y
   `TAQUITOS PAQUITO` ×1 acierta sin modelo.
2. **Núcleo común**: los tokens presentes en ≥50% de las variantes, en el
   orden de la variante más larga. Para el ejemplo da `TACOS ... PACO`.

El LLM se adopta **solo si gana** en una muestra etiquetada a mano (§9.2).
Si empata, se queda el baseline: es gratis, instantáneo y reproducible.

Y una restricción de uso: **el nombre canónico es campo de presentación, no
covariable**. Nada del modelo de supervivencia puede leerlo. Eso acota el
daño de un nombre mal generado a la legibilidad de un reporte, y hay que
decirlo en el `COMMENT` de la columna para que no se filtre a un análisis.

---

## 7. Dónde vive el código

```
src/etl/loaders/gold.py      ← la parte determinista: §4, §5 y los invariantes
src/agents/gold/
    reconciliador.py         ← detrás de base.Agente (crear_agentes.md §9.4)
    prompts/gold_v1.txt
```

Fronteras, que ya están declaradas en `crear_agentes.md` §9.3 y hay que
hacer cumplir en `tests/unit/test_layer_boundaries.py`:

```
src.etl.loaders.gold  →  src.agents.gold      (el ETL invoca al agente)
src.agents.gold       →  src.shared, src.meta (y nada mas)
```

El reconciliador **no importa `src.etl`**: recibe el cluster como datos
planos. Quien lo llama ya hizo el join y la cascada determinista.

**No se reusa `loaders/permanencia.py`.** Comparten la forma (troceado por
bloque de identidad, reanudable, latido) pero no el SQL: Gold lee
`permanencia_<m>` —ya colapsada a 10M— más `clee_<m>` y los hechos para los
valores canónicos. Si el troceo se repite literal, se extrae a
`shared/db/troceado.py` cuando exista el segundo caso, no antes.

---

## 8. Procedencia: Gold es lo que la tesis cita

Cada fila tiene que poder contestar "¿cómo se decidió esto?" tres años
después. Columnas de la propia tabla:

| Columna | Qué guarda |
|---|---|
| `metodo_base` | `radio` \| `h3` \| `llm` — de qué partición salió |
| `origen_identidad` | `ACUERDO` (los dos métodos coincidían) \| `REGLA` (contención o empate, §2) \| `AGENTE` (adjudicado por el LLM) |
| `version_reglas` | Versión del conjunto de reglas de §4. Un cambio de regla cambia los números |
| `agente_decision_id` | FK lógica a `meta.agent_decisions` cuando `origen_identidad = AGENTE`; `NULL` si no |
| `construida_en` | Fecha de construcción de la fila |

`meta.agent_decisions` (`crear_agentes.md` §9.4) hace triple trabajo: caché
por `entrada_hash`, auditoría y **reproducibilidad** — re-correr con la
caché poblada da los mismos números, que es la única garantía real con un
LLM de por medio.

Consecuencia operativa: **la caché de decisiones es un artefacto de la
tesis**, no un detalle de rendimiento. Entra al respaldo y no se purga.

---

## 9. Validación

### 9.1 Invariantes duros (fallan la corrida, no son avisos)

- Ninguna observación `(id, period)` pertenece a dos negocios de Gold.
- La suma de observaciones sobre todos los negocios = 82,896,801. Si no
  cuadra, el reconciliador perdió o duplicó filas.
- `primer_periodo <= ultimo_periodo` y `cortes_observados <= duracion_periodos + 1`.
- Todo `agente_decision_id` no nulo existe en `meta.agent_decisions`.

### 9.2 Que el agente mejore algo, medido

El arnés ya existe: `scripts/evaluate_masked_clee.py` mide precision/recall
contra el `clee` oficial de INEGI como ground truth (97.72% F1 hoy). La
pregunta que decide si el reconciliador entra a la tesis es:

> ¿`reconciliado` tiene mejor F1 que `radio` solo, sobre la misma muestra
> enmascarada?

Si no mejora, el agente es complejidad sin resultado y se descarta. Ese es
el mismo criterio con que `crear_agentes.md` §1 acotó al adjudicador CLEE a
un techo de +0.98 puntos antes de escribirlo.

Para `nombre_canonico` y `codigo_act` no hay ground truth, así que hace
falta una **muestra etiquetada a mano** (~200 clusters, estratificada por
número de variantes) y reportar acuerdo con el etiquetado humano frente a
los dos baselines de §6.

---

## 10. Aritmética de costo

Con `d` = fracción de observaciones en disputa (§1) y ~10.45M negocios:

```
clusters a adjudicar   ≈  10.45M × d × (fracción de solapamiento parcial, §2)
latencia por decision  ≈  50–200 ms   (Ollama local, crear_agentes.md §5)
concurrencia util      ≈  8 llamadas  (RTX 5090)
```

| `d` | Clusters | Tiempo a 8× | Veredicto |
|---:|---:|---:|---|
| 0.1% | ~10k | ~4 min | Trivial |
| 1% | ~100k | ~40 min | Cómodo |
| 5% | ~500k | ~3.5 h | Aceptable, una vez |
| 20% | ~2M | ~14 h | Ya no es adjudicación, es un método nuevo |

El presupuesto por corrida (`shared/llm/budget.py`) es tope duro: si la
corrida excede las llamadas previstas, se detiene y reporta, en vez de
descubrirlo a las 14 horas.

---

## 11. Orden de construcción

1. **Medir** (§1). El número decide si los pasos 5 y 6 existen.
2. **`gold.establecimientos` determinista**, método base `radio`, valores
   canónicos por las reglas de §4 y §5, invariantes de §9.1. Sin una línea
   de LLM. **Entregable usable por sí solo**: con esto la tesis ya tiene su
   unidad de análisis.
   2b. **`gold.variantes`** (§6) en la misma pasada: sale del mismo
   `GROUP BY` que los canónicos, y es lo que hace auditable el paso 2.
3. **Reportar la N** y la distribución de `origen_identidad` (va a ser
   `ACUERDO` casi todo si §1 sale bajo).
4. **Cascada determinista de desacuerdo** (§2, contención y empate). Vuelve
   a medir cuánto queda.
5. **Reconciliador** (§3) para el solapamiento parcial, detrás del contrato
   de agente.
6. **Validar** (§9.2). Si no mejora el F1, se revierte el paso 5 y queda
   documentado como camino explorado — que también es resultado.

El orden no es negociable en un punto: **nunca se construye Gold por primera
vez con el agente dentro**. Si la primera versión de Gold depende de un LLM,
no hay línea base contra la cual medir si el LLM sirvió.

---

## 12. Riesgos

### 12.1 Reconciliar cambia la N del estudio

Si el agente divide clusters, hay más negocios con vidas más cortas; si los
une, menos negocios con vidas más largas. **Las dos cosas mueven todas las
tasas de supervivencia**, que son el resultado de la tesis. Hay que reportar
la N y la distribución de `duracion_periodos` antes y después, y tratar la
diferencia como un análisis de sensibilidad, no como una corrección.

### 12.2 El LLM como fuente de no-reproducibilidad

Mitigado por la caché, pero no eliminado: cambiar de modelo o de prompt
cambia los números en silencio. De ahí `version_reglas` y `prompt_version`
en cada fila, y de ahí que la caché sea artefacto y no optimización.

### 12.3 La tentación del nombre canónico

Un campo de texto generado por un LLM es exactamente el tipo de columna que
alguien acaba metiendo a un modelo. §6 lo prohíbe y el `COMMENT` de la
columna tiene que decirlo.

### 12.4 El desacuerdo puede ser un bug, no un caso de uso

Si §1 sale alto, la primera hipótesis no es "los métodos capturan cosas
distintas" sino "la paridad se rompió". Un LLM encima de un bug de blocking
produce resultados convincentes y equivocados.

---

## 13. Lo que este TODO no hace

- **No diseña el panel negocio×periodo ni el cruce con SESNSP**: eso es
  `docs/cruce_denue_sesnsp.md`, y consume `gold.establecimientos` cuando
  exista.
- **No decide la regla de cierre.** Sigue preliminar y sigue aplicándose en
  la consulta; Gold materializará el veredicto cuando esté validada.
- **No duplica `crear_agentes.md`.** La infraestructura LLM, el contrato de
  agente y `meta.agent_decisions` se dan por hechos; si algo difiere, manda
  la §9 de aquel documento.
- **No implementa nada.**
