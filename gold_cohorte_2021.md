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

| Cuesta | Magnitud |
|---|---|
| La ventana de observación | De ~11 años a ~5. `duracion_periodos` pierde la mitad de su rango |
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

## 3. El sesgo de supervivencia es de encuadre, y hay que escribirlo

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

Y `gold.variantes_negocio` para los alias, con su índice trigram
(`normalizacion_gold.md` §6). **También acotada a esta cohorte**: no
contiene identidades propagadas ni sintéticas, y si algún día existe la
extensión de robustez sus variantes van en `gold.variantes_negocio_propagado`
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
- Todo `clee` de `gold.variantes_negocio` existe en esta tabla.
- Cada `(clee, campo)` de variantes tiene **exactamente una** fila con
  `es_canonico`, y su `valor` coincide con el que esta tabla publicó. Es lo
  que hace verificable la canonicalización sin re-derivarla.

### 5.2 Una tabla por régimen, nunca una columna `cohorte`

La identidad **observada** y la **inferida** no se mezclan en la misma
tabla. Si se construye la extensión de robustez (§7), va en tablas propias:

```
gold.negocios_clee_oficial     ←→  gold.variantes_negocio
gold.negocios_clee_propagado   ←→  gold.variantes_negocio_propagado
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

El alcance se mide antes de escribir nada:

```sql
SELECT n_nombres, count(*) AS negocios
FROM silver.permanencia_radio
WHERE cortes_directo > 0 AND NOT es_sintetica
GROUP BY 1 ORDER BY 1;
```

Los de `n_nombres = 1` **no necesitan LLM**: ya hay un solo nombre. El
trabajo es la cola de `n_nombres >= 2`, y hay que verla antes de dimensionar.

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
4. **`gold.negocios_clee_oficial` determinista** + `gold.variantes_negocio`, con los
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
