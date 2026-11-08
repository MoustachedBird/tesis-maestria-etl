# `gold.negocios_clee_oficial` — la cohorte con CLEE oficial (2021+)

**Estado:** propuesto, nada implementado. Escrito el 2026-11-07.

**Qué entrega:** el alcance de la capa de consumo, el criterio de corte
verificado contra el repo, por qué Gold **no** puede copiar
`silver.permanencia_<metodo>`, y el único lugar donde interviene un LLM.

**La decisión, en una línea:** Gold se construye solo sobre los cortes en
que INEGI publica el CLEE, así que su identidad es **observada y no
inferida** — y con eso desaparecen los `NEW_*`, el error de propagación y la
necesidad de un consenso entre métodos.

---

## 0. Qué elimina y qué cuesta

| Desaparece del camino crítico | Por qué |
|---|---|
| Identidades sintéticas `NEW_*` | Solo las crea la fase 2 (propagación). En los cortes oficiales la identidad viene del dato |
| El error de propagación | El 2.31% que midió el arnés es error *de propagación*. No aplica a lo que INEGI asignó |
| El ensemble y su consenso | `radio`, `h3` y `celda_h3_7` hacen la **misma copia determinística** en estos cortes. No hay nada que votar |
| El agente adjudicador | Sin desacuerdo no hay triángulos inconsistentes que arbitrar |
| `clee_prob`, `clee_ml`, `clee_nombre`, `clee_llm` (A, B, C, E) | Dejan de ser prerrequisito. Ver §7 |

| Cuesta | Magnitud (medido el 2026-11-08) |
|---|---|
| La ventana de observación | **9 cortes sobre 5 años** (2021-05 → 2026-05), de 20 cortes sobre 11 años |
| Las observaciones que quedan fuera | 31.4M de 82.9M. **La cohorte conserva el 62%** |
| Los negocios que nacieron y murieron antes de 2021 | Invisibles por construcción |
| La generalidad de la afirmación | Es una cohorte, no el universo. Ver §3 |

---

## 1. Desde cuándo hay CLEE oficial: verificado, no supuesto

Cuatro lugares del repo lo documentan y coinciden:

| Fuente | Qué dice |
|---|---|
| `AGENTS.md:311` | "INEGI assigns CLEE codes starting in 2021, but older periods (2015-2020) lack them" |
| `todo.md:279` | "Periodo objetivo principal: `05_2021` (el más viejo con CLEE oficial)" |
| `scripts/evaluate_masked_clee.py:67` | `DEFAULT_TARGET_PERIOD = "2021-05-01"` — "el mas viejo con CLEE oficial" |
| `docs/clee_propagation.md` | "INEGI publica el CLEE solo desde los cortes de 2021" |

**El corte frontera es `05_2021` (2021-05-01).**

### 1.1 El criterio NO se cablea: se detecta

Un año en el código es una bomba de tiempo —si INEGI cambia de política, o
si un corte sale con cobertura parcial, el filtro miente sin fallar—. El
arnés de evaluación ya resolvió esto con una definición operativa:

```python
CLEE_COVERAGE_MIN = 0.999   # scripts/evaluate_masked_clee.py:77
```

**Gold adopta ese mismo criterio**: un corte es *oficial* si ≥99.9% de sus
observaciones traen `clee` no vacío. La consulta que lo determina:

```sql
SELECT period,
       count(*)                                        AS obs,
       count(clee) FILTER (WHERE clee <> '')           AS con_clee,
       round(100.0 * count(clee) FILTER (WHERE clee <> '') / count(*), 3) AS pct
FROM silver.establecimientos
GROUP BY 1 ORDER BY 1;
```

Los cortes con `pct >= 99.9` son la ventana de Gold. El loader la calcula al
arrancar y la **registra en el log y en `meta`**: si un día entran o salen
cortes, queda constancia de con qué ventana se construyó esa versión de la
tabla.

#### Medido el 2026-11-08: la ventana son 9 cortes

```
 period     |   obs   | pct_clee        period     |   obs   | pct_clee
 2015-01-01 | 2683667 |   0.000         2021-05-01 | 5515863 | 100.000
 2016-01-01 | 2736201 |   0.000         2021-11-01 | 5529201 | 100.000
 2016-10-01 | 2734953 |   0.000         2022-05-01 | 5528695 | 100.000
 2017-03-01 | 2726730 |   0.000         2022-11-01 | 5530924 | 100.000
 2017-11-01 | 2729456 |   0.000         2023-11-01 | 5541077 | 100.000
 2018-03-01 | 2731413 |   0.000         2024-05-01 | 5564607 | 100.000
 2018-11-01 | 2736090 |   0.000         2024-11-01 | 6058534 | 100.000
 2019-04-01 | 2763135 |   0.000         2025-05-01 | 6097668 | 100.000
 2019-11-01 | 3143215 |   0.000         2026-05-01 | 6138074 | 100.000
 2020-04-01 | 3183180 |   0.000
 2020-11-01 | 3224124 |   0.000         9 cortes, 51,504,643 obs (62%)
```

La cobertura es **exactamente 0% o exactamente 100%**, sin cortes parciales.
El criterio de `>= 99.9%` no tiene que decidir nada hoy, y por eso mismo se
queda: es la guarda para el día en que un corte salga a medias.

**Falta el corte 2023-05**, así que los intervalos van 6, 6, 6, **12**, 6,
6, 6, **12** meses. Irregulares — que es por lo que el tiempo se mide en
`periodos` y no en días (`AGENTS.md` §N3).

### 1.2 Un matiz que importa: dos definiciones conviven en el repo

| Dónde | Criterio | Consecuencia |
|---|---|---|
| `clee_resolver._get_periods_with_clee_coverage` | `EXISTS` — **basta UNA fila** con `clee` | Un corte con 10% de cobertura se clasificaría como determinístico, y el 90% restante se quedaría **sin fila** en `clee_<metodo>` |
| `evaluate_masked_clee.py` | `>= 99.9%` | Estricto y correcto |

Gold usa el segundo. El primero es un filo que ya estaba anotado como
riesgo (`normalizacion_gold.md`, discusión del 2026-11-06) y que no se
arregla aquí: Gold no depende de esa clasificación, calcula la suya.

---

## 2. Qué entra en la cohorte

> Un negocio entra a `gold.negocios_clee_oficial` si tiene **al menos una
> observación en un corte oficial**, y su ventana de vida se calcula usando
> **solo** los cortes oficiales.

Las dos mitades de esa frase importan, y la segunda es la que hace el
trabajo. Ver §4.

Lo que queda fuera, y por qué está bien:

- **Negocios vistos solo antes de 2021.** Su identidad sería propagada o
  sintética; son precisamente los que esta capa no quiere.
- **Las observaciones pre-2021 de un negocio que sí entra.** Se quedan en
  Silver, alcanzables por el join, y la extensión de robustez (§7) las usa.
- **Filas sin `clee` dentro de un corte oficial** (ese ≤0.1%). No tienen
  identidad observada; entran como los demás solo si el negocio aparece con
  `clee` en otro corte oficial.

---

## 3. Dos discontinuidades, y la segunda es la que importa

### 3.0 En 2021 la cobertura de DENUE casi se duplica

| Corte | Observaciones |
|---|---|
| 2020-11 (último sin CLEE) | 3,224,124 |
| **2021-05** (primero con CLEE) | **5,515,863** |

**+71% en un solo corte, seis meses.** No es crecimiento económico: es un
cambio de metodología de INEGI, 2.3M de establecimientos que aparecen de
golpe. Tiene tres consecuencias:

1. **Refuerza la decisión de restringir a 2021+.** Los cortes pre-2021 no
   solo carecen de CLEE: cubren un universo **distinto y más chico**.
   Mezclarlos nunca fue solo un problema de identidad inferida.
2. **Es un caveat serio para la extensión de robustez (§7).** Comparar la
   ventana completa contra la oficial confundiría el cambio de cobertura con
   la dinámica de supervivencia: todo lo que "nace" en 2021-05 sería un
   artefacto. Si esa extensión se construye, 2021-05 se trata como una
   **discontinuidad**, no como un corte más.
3. **`presente_desde_el_inicio` no significa "ya existía"** para esos 2.3M:
   significa "INEGI empezó a verlo". El modelo no puede confundir las dos
   cosas, y de ahí que `fecha_alta_date` sea la covariable de antigüedad y
   no el primer corte observado.

### 3.1 El sesgo de supervivencia es de encuadre, y hay que escribirlo

Restringir a 2021+ hace que la cohorte sea "establecimientos que llegaron
vivos a 2021". Eso **no** es un defecto del dato, pero sí acota lo que se
puede afirmar:

| Afirmación | ¿Válida? |
|---|---|
| "De los establecimientos observados en los cortes 2021+, estas variables predicen su supervivencia en los años siguientes" | **Sí.** Es un estudio de cohorte bien definido |
| "Esta es la tasa de supervivencia de la MIPYME mexicana" | **No.** Mide la supervivencia de los sobrevivientes a 2021 |

Mitigante parcial: `fecha_alta_date` (fecha de alta en el RENEM) da un proxy
de antigüedad **independiente de la ventana de observación**, así que
`dias_alta_a_primera` distingue al negocio nuevo del que ya tenía quince
años cuando el directorio lo vio por primera vez. Es fecha de registro y no
de operación —el caveat ya está documentado en el modelo— pero es señal
real y hay que usarla como covariable de antigüedad.

La restricción va en el capítulo de limitaciones, con esta tabla.

---

## 4. Gold NO puede copiar `permanencia`: tiene que recalcular la ventana

Es el hallazgo técnico central de este documento.

`silver.permanencia_<metodo>` calcula la ventana sobre **los 20 cortes**:
`idx_primer`, `idx_ultimo`, `cortes_observados`, `huecos`,
`duracion_periodos` y `cortes_tras_ultimo` son todos relativos a la
secuencia completa. Y una fila de permanencia **mezcla los dos regímenes**:
un negocio visto en 2019 (identidad propagada) y en 2021 (identidad
oficial) comparte `clee_resuelto`, así que su fila tiene `cortes_directo > 0`
**y** `cortes_propagado > 0`.

Filtrar `WHERE cortes_directo > 0` daría la cohorte correcta pero con la
ventana contaminada: el error de propagación volvería a entrar por la
puerta de atrás, justo lo que esta capa quería eliminar.

**Entonces Gold agrega por su cuenta sobre los cortes oficiales.** Y los
`idx` hay que **renumerar dentro del subconjunto**: con 0 = primer corte
oficial. Si se heredaran los índices de la secuencia de 20,
`presente_desde_el_inicio` y `duracion_periodos` saldrían mal sin un solo
error visible.

### 4.1 Una sola implementación del agregado, dos salidas

El agregado es el mismo que ya existe: `loaders/permanencia.py` lo hace
troceado por corte y por bloque de identidad, reanudable, con latido y
registro en `meta`. Lo que cambia es **qué cortes entran y cómo se numeran**.

Propuesta: parametrizar `construir_permanencia` por la lista de cortes y el
destino, en vez de escribir un segundo agregado.

```
construir_permanencia(engine, metodo, tracker)                    -> silver.permanencia_<metodo>   (20 cortes)
construir_permanencia(engine, metodo, tracker, cortes=oficiales,  -> gold.negocios_clee_oficial
                      destino="gold.negocios_clee_oficial")
```

`_cortes_ordenados()` ya devuelve la lista y `_sql_llenar_obs()` ya recibe
el `idx` de cada corte como literal, así que el cambio es acotado. Dos
implementaciones del mismo `GROUP BY` sobre 82.9M filas sería la
duplicación más cara posible: la que se desincroniza en silencio.

---

## 5. Esquema y reglas por campo

Las reglas de valor canónico son las de `normalizacion_gold.md` §§4–5 y no
se duplican aquí: agregados de trayectoria, banderas `tiene_*`, un solo
corte de referencia para todo el bloque de ubicación. Lo que **cambia** en
esta cohorte:

| Campo | En Silver | En Gold 2021+ |
|---|---|---|
| `es_sintetica` | True para los `NEW_*` | **Siempre False** → no se materializa |
| `cortes_propagado` | Cuántos cortes con identidad inferida | **Siempre 0** → no se materializa |
| `cortes_directo` | Cuántos con identidad de INEGI | = `cortes_observados` → redundante, no se materializa |
| `origen` | DIRECTO / PROPAGADO / NUEVO | **Siempre DIRECTO** → no se materializa |

Que esas cuatro columnas se vuelvan constantes **es la propiedad que define
la capa**, y la forma de decirlo en el esquema es no tenerlas. En su lugar,
un `COMMENT ON TABLE` que declare el invariante: *toda identidad de esta
tabla la asignó INEGI*.

Lo que sí se agrega, propio de Gold (`normalizacion_gold.md` §8):

| Columna | Qué guarda |
|---|---|
| `ventana_cortes` | Los cortes oficiales con que se construyó esta versión, como `date[]`. Es lo que hace la fila citable: si mañana entra un corte nuevo, la N cambia y esto lo explica |
| `metodo_base` | De qué `clee_<metodo>` salió. En esta cohorte los tres coinciden, así que es trazabilidad, no elección |
| `version_reglas` | Versión del conjunto de reglas de §5 |
| `construida_en` | Fecha de construcción |

Y `gold.negocios_clee_oficial_variantes` para los alias, con su índice trigram
(`normalizacion_gold.md` §6). **También acotada a esta cohorte**: no
contiene identidades propagadas ni sintéticas, y si algún día existe la
extensión de robustez sus variantes van en `gold.negocios_clee_propagado_variantes`
— una tabla por régimen, nunca una columna `cohorte`, por el mismo motivo
que `clee_radio`/`clee_h3` son dos tablas (§5.2).

### 5.1 Invariantes que fallan la corrida

- Todo `clee` de la tabla aparece con `clee` no vacío en ≥1 corte oficial.
- **Ningún `clee` empieza con `NEW_`.** Redundante por construcción —solo la
  fase 2 crea sintéticos— y por eso mismo es la aserción que atrapa un error
  de alcance futuro.
- Ninguna observación `(id, period)` contribuye a dos filas.
- `cortes_observados <= len(ventana_cortes)`.
- `idx_primer = 0` ⟺ el negocio aparece en el primer corte oficial.
- Todo `clee` de `gold.negocios_clee_oficial_variantes` existe en esta tabla.
- Cada `(clee, campo)` de variantes tiene **exactamente una** fila con
  `es_canonico`, y su `valor` coincide con el que esta tabla publicó. Es lo
  que hace verificable la canonicalización sin re-derivarla.

### 5.2 Una tabla por régimen, nunca una columna `cohorte`

La identidad **observada** y la **inferida** no se mezclan en la misma
tabla. Si se construye la extensión de robustez (§7), va en tablas propias:

```
gold.negocios_clee_oficial     ←→  gold.negocios_clee_oficial_variantes
gold.negocios_clee_propagado   ←→  gold.negocios_clee_propagado_variantes
```

Es el criterio que el proyecto ya aplicó a `clee_radio`/`clee_h3` y a
`permanencia_radio`/`permanencia_h3`: con una columna discriminadora, un
`WHERE` olvidado mezcla los dos regímenes y los agregados salen
contaminados **en silencio y sin error**. Con dos tablas el régimen va en el
`FROM` y la consulta no puede equivocarse.

Y la columna de identidad se llama **`clee`**, no `clee_resuelto`: en esta
cohorte no se resolvió nada, es el identificador que publicó INEGI. La
tabla propagada sí llevaría `clee_resuelto`, y esa diferencia de nombre es
documentación — si ves `clee_resuelto`, estás en terreno inferido.

---

## 6. El único LLM: el nombre canónico

Es el único lugar donde interviene un modelo, y es el uso más seguro
posible: **el nombre canónico es campo de presentación, no covariable**
(`normalizacion_gold.md` §6.4 lo prohíbe explícitamente y el `COMMENT` de la
columna tiene que decirlo). Si el modelo se equivoca, se equivoca en la
legibilidad de un reporte, no en un resultado.

### 6.1 Medido el 2026-11-08: el lote real son ~393 mil, no 2.7 millones

Sobre los 20 cortes (la cota superior; en la ventana oficial será menor):

```
n_nombres = 1   ->  12,967,459   82.9%    nada que decidir
n_nombres = 0   ->       2,184             nunca publico nombre
n_nombres >= 2  ->   2,674,280   17.1%
```

Pero la distribución está muy sesgada, y eso **reduce el alcance del LLM**:

| | Negocios | ¿Necesita LLM? |
|---|---:|---|
| `n = 2` | 1,748,685 (65% de los multi-nombre) | **No.** Gana el que apareció en más cortes; si empatan, el más reciente. El baseline lo resuelve |
| `n = 3` | 532,194 (20%) | Marginal. La moda sigue decidiendo |
| `n >= 4` | **393,401** | **Sí.** Aquí hay un juicio real |

~393 mil decisiones son unas 2 horas a 8 llamadas concurrentes, no 14.

### 6.2 `n_nombres > 20` no se canonicaliza: se marca

Un establecimiento aparece **una vez por corte** y hay 20 cortes, así que
`n_nombres > 20` es **imposible** para un cluster correcto: significa que la
identidad agrupó más de un establecimiento en el mismo corte.

Medido: **~1,245 negocios con `n_nombres >= 21`**, con casos de 345, 150 y
123 nombres distintos. Esos no son negocios que cambiaron de nombre 345
veces; son fusiones indebidas de la resolución.

Confirma por una vía independiente la métrica de "colisiones intra-periodo"
de `clee_propagation_stats.py`, y da una regla dura: **esos clusters no van
al LLM.** Pedirle un nombre canónico para algo que no es un negocio produce
una respuesta convincente y sin sentido. Se marcan con una bandera y se
reportan como límite de la resolución.

El umbral exacto se recalcula sobre la ventana oficial: ahí son 9 cortes, así
que el imposible es `n_nombres > 9`.

Y sigue en pie la condición de `normalizacion_gold.md` §6.4: el LLM se
adopta **solo si vence** a los dos baselines deterministas (moda desempatada
por el más reciente; núcleo común de tokens) sobre una muestra etiquetada a
mano. Si empata, se queda el baseline.

---

## 7. Lo que sale del camino crítico (y no se tira)

| | Antes | Ahora |
|---|---|---|
| Propagación (radio / h3 / `celda_h3_7`) | Prerrequisito de Gold | **Capítulo metodológico** y extensión de robustez a 2015-2020 |
| A, B, C, E (`clee_prob`, `clee_ml`, `clee_nombre`, `clee_llm`) | Necesarios para el consenso | Opcionales. Se deciden con el arnés, sin bloquear Gold |
| Ensemble y consenso (`alternativas_clee_propagation.md`) | La vía para unificar identidades | La vía para **extender la ventana**, que es otro problema |
| El backfill de `h3_cell_7` | Bloqueaba a `celda_h3_7` | Sigue pendiente, pero ya no bloquea a Gold |
| El reconciliador LLM | Archivado por desacuerdo cero | Sigue archivado, y ahora también por alcance |

**La extensión de robustez es el camino de vuelta**, y es valiosa: una vez
que Gold existe sobre la cohorte limpia, repetir el análisis sobre la
ventana completa (con identidad propagada) y comparar los coeficientes es
un resultado — dice cuánto del hallazgo depende del matching.

---

## 8. Orden de construcción

1. **Medir la ventana** (§1.1): qué cortes son oficiales y cuántos. Ese
   número decide si la ventana alcanza para el modelo de supervivencia, y es
   la única cosa que podría invalidar todo el enfoque.
2. **Medir la cohorte**: cuántos negocios tienen ≥1 observación oficial, y
   la distribución de `n_nombres` (§6).
3. **Parametrizar el agregado** por lista de cortes y destino (§4.1), con la
   renumeración de `idx` y sus tests.
4. **`gold.negocios_clee_oficial` determinista** + `gold.negocios_clee_oficial_variantes`, con los
   invariantes de §5.1. **Entregable usable: la tesis ya tiene su unidad de
   análisis.**
5. **Reportar la N** y la distribución de `duracion_periodos`.
6. **Solo entonces**, el nombre canónico: baselines primero, LLM después y
   solo si gana.

El paso 1 es el único que puede cambiar la decisión. Si resultan ser tres
cortes, la ventana no alcanza y hay que volver a la propagación.

---

## 9. Lo que este documento NO hace

- **No duplica las reglas por campo**: están en `normalizacion_gold.md`
  §§4–6 y aquí solo se declara lo que cambia en esta cohorte.
- **No decide la regla de cierre.** Sigue siendo preliminar y se aplica en
  la consulta (`AGENTS.md` §N3).
- **No cancela `alternativas_clee_propagation.md`.** Lo reubica: de
  prerrequisito a extensión.
- **No implementa nada.**
