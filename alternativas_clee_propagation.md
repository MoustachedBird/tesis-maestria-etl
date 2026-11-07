# Alternativas de propagación CLEE — métodos diversos y consenso

**Estado:** propuesto, nada implementado. Escrito el 2026-11-07.

**Qué entrega:** el criterio con que un método nuevo merece entrar al
conjunto, el inventario de alternativas ordenado por **qué eje del
algoritmo varían**, y el diseño del paso de consenso que las combina en una
sola identidad para `gold`.

**Por qué existe:** `todo.md` Fase 7 ya propone tres métodos para
**comparar** precisión, y `crear_agentes.md` diseña un cuarto con un LLM que
`arquitectura.md`:15 nombra "tercer método de resolución". Este documento
cubre lo que falta: **combinarlos**. Son dos propósitos con requisitos
distintos, y el segundo impone uno que el primero no necesita — diversidad
de errores.

> **REUBICADO (2026-11-07).** `gold_cohorte_2021.md` construye la capa de
> consumo solo sobre los cortes con CLEE oficial, donde no hay identidades
> propagadas ni sintéticas. Eso saca a este documento del camino crítico:
> los métodos diversos y el consenso dejan de ser el requisito para unificar
> identidades y pasan a ser **la vía para extender la ventana a 2015-2020**,
> como análisis de robustez. El contenido sigue vigente; cambia para qué
> sirve.

---

## 0. El hecho que lo motiva: hoy hay UN método, no dos

Medido el 2026-11-07 con `scripts/sql/desacuerdo_particiones.sql` sobre las
82.9M observaciones:

```
 obs_comparadas | clusters_radio | clusters_h3 | obs_en_disputa
----------------+----------------+-------------+----------------
       82896807 |     15,643,923 |  15,643,923 |              0
```

`clee_radio` y `clee_h3` producen la **misma partición**, identidad por
identidad. Era lo esperado —`todo.md` Fase 7 ya lo advertía: "radio y H3
producen asignaciones idénticas por construcción (mismo radio de 500m,
misma cascada; solo difiere el generador de candidatos)"— pero ahora está
medido sobre el universo completo, no argumentado.

Consecuencia para un ensemble: **`h3` no es un segundo voto, es un índice.**
Promediarlo con `radio` es promediar un número consigo mismo. El track H3
cumplió su función (demostrar que el blocking no altera el resultado) y a
partir de aquí su costo —~14h por corrida— no compra información.

---

## 1. Dos propósitos que conviene no confundir

| | Comparar | Combinar |
|---|---|---|
| Pregunta | ¿qué regla de decisión acierta más? | ¿qué identidad publica Gold? |
| Requisito | ground truth (CLEE enmascarado) | **errores poco correlacionados** |
| Salida | una tabla de precision/recall por método | una partición de consenso |
| Dónde vive | `todo.md` Fase 7 | este documento + `normalizacion_gold.md` |
| Métodos idénticos | no estorban, solo no aportan | **inutilizan el ensemble** |

Un ensemble de métodos correlacionados no mejora nada: si los tres se
equivocan en las mismas filas, el voto mayoritario reproduce el error con
más confianza. De ahí la §2.

---

## 2. Criterio de admisión: la diversidad se mide ANTES de ensamblar

Un método candidato pasa dos filtros, en este orden:

### 2.1 ¿Es diverso? (`desacuerdo_particiones.sql`)

La consulta que midió radio-vs-h3 sirve para cualquier par. Es barata
(~2.5 min) y se corre contra **cada** método ya admitido:

| Desacuerdo | Veredicto |
|---|---|
| **< 1%** | No aporta. Es una reimplementación, no un método. Puede servir como optimización, pero no entra al voto |
| **1 – 15%** | Diverso y plausible. Entra, y el consenso tiene trabajo real |
| **15 – 40%** | Muy diverso. Entra, pero hay que mirar los casos a mano: una diferencia así suele ser un criterio con sesgo propio, no dos opiniones legítimas |
| **> 40%** | Uno de los dos está mal. Se investiga antes de ensamblar |

### 2.2 ¿Acierta? (`scripts/evaluate_masked_clee.py`)

Diversidad sin precisión es ruido. El arnés de CLEE enmascarado da
precision/recall contra el `clee` oficial de INEGI, **sobre una muestra**
(CDMX / Jalisco / Zacatecas, periodo objetivo `05_2021`), así que probar un
método cuesta minutos y no una noche. Referencia actual: **97.72% F1**.

Un método entra al consenso si es diverso **y** su F1 no está muy por
debajo del de referencia. Uno diverso pero malo mete votos basura.

---

## 3. Los ejes por donde se diversifica

El algoritmo actual es *una* combinación de cinco decisiones. Variar
cualquiera produce un método distinto; variar solo el generador de
candidatos produce `h3`, que es el caso que ya sabemos que no diversifica.

| Eje | Hoy | Por qué variar aquí sí diversifica |
|---|---|---|
| **Generador de candidatos** | BallTree / `grid_disk` | **No diversifica.** Los dos terminan en el mismo filtro haversine de 500 m. Es un índice |
| **Criterio espacial** | círculo de 500 m, isotrópico | Una grilla, un AGEB o un CP cambian *quién es vecino*, y su error depende de la geografía y no de la distancia |
| **Comparación de nombre** | `token_sort_ratio` de rapidfuzz | TF-IDF/MinHash sobre n-gramas falla en casos distintos a los del fuzzy de tokens |
| **Modelo de decisión** | cascada con umbrales fijos | Un modelo probabilístico o aprendido pondera la evidencia en vez de aplicar cortes |
| **Qué se usa para bloquear** | el espacio | **El punto ciego estructural**: un negocio que se mudó a >500 m nunca matchea, con ningún umbral. Bloquear por nombre lo ve |
| **Asignación** | `argmax` por fila | 1:1 global resuelve los gemelos reales, que ningún scorer puede (§7) |

---

## 4. Inventario de alternativas

| | Método | Eje que varía | Estado | Dónde está diseñado |
|---|---|---|---|---|
| **A** | `clee_prob` — Fellegi-Sunter (EM, `splink`) | Modelo de decisión | Propuesto, recomendado | `todo.md` Fase 7 Opción A |
| **B** | `clee_ml` — LightGBM/logística supervisada | Modelo de decisión | Propuesto | `todo.md` Fase 7 Opción B |
| **C** | `clee_nombre` — blocking por MinHash/LSH de nombre | Qué se usa para bloquear | Propuesto | `todo.md` Fase 7 Opción C |
| **D** | `clee_celda_h3_7` — la celda H3 **es** el criterio | Criterio espacial | **Nuevo**, ver §5 | este documento |
| **E** | `clee_llm` — adjudicador LLM por candidato | Modelo de decisión | Propuesto, **ya previsto en la arquitectura** | `crear_agentes.md` §§1–8; `arquitectura.md`:15 lo nombra "tercer método de resolución"; la tabla está reservada en `docs/separacion_ingest_clee.md` |
| — | `clee_h3` | Generador de candidatos | **Implementado, 0% diverso** | §0 |

**El más prometedor para un ensemble es C**, y no por su precisión: su
perfil de error es **ortogonal**. Radio y todas las variantes espaciales
comparten el mismo punto ciego —el negocio que se mudó lejos— y C es el
único que lo ve. Donde los demás fallan por construcción, C puede acertar;
eso es exactamente lo que hace que votar sirva.

**A y B son el mismo experimento con distinta fuente de pesos**
(`todo.md` lo dice: FS/EM no supervisado vs GBM supervisado). Para el
ensemble cuentan casi como un solo voto, porque comparten candidatos y
features: su desacuerdo entre sí probablemente salga bajo. Vale medirlo
antes de admitir los dos.

### 4.1 Descartados, y por qué se respeta el descarte

- **Embeddings neuronales de nombres** — descartado en `todo.md` Fase 7:
  "costo/beneficio pobre vs TF-IDF/MinHash en nombres cortos y ruidosos de
  encuesta". `arquitectura.md` §2.3.3 los nombraba como candidatos; la
  decisión posterior de `todo.md` manda. No se re-litiga aquí.
- **Clustering global sobre el grafo de pares** (HDBSCAN, componentes
  conexos) — descartado en `todo.md` porque "cambia la semántica de
  asignación (entidad = clúster multi-periodo, no ventana deslizante) y
  rompe la comparabilidad columna a columna". **Ese descarte sigue en pie
  para Silver, y el consenso de §6 no lo contradice** — ver §6.3.

---

## 5. Opción D: la celda H3 como criterio, no como índice

La idea: en vez de "¿está a ≤500 m?", preguntar "¿cae en la misma celda
H3?". Un equi-join por entero, sin haversine, sin `grid_disk`. Sería el
método más barato del conjunto por un margen enorme.

Medido en Guadalajara (20.674, −103.344) con h3-py:

| res | Área | Arista | Distancia **máxima** entre dos puntos de la celda | Pierde de las parejas a ≤500 m |
|---|---|---|---|---|
| 6 | 42.91 km² | 4.06 km | 8.15 km | **2.2%** |
| 7 | 6.13 km² | 1.54 km | 3.08 km | 6.5% |
| 8 | 0.88 km² | 0.58 km | 1.16 km | **20.2%** |
| 9 | 0.13 km² | 0.22 km | 0.44 km | 63.6% |

Y la distribución de distancias entre dos negocios que caen en la **misma**
celda res 7 (20,000 pares simulados):

```
percentil  50:    972 m
percentil  90:  1,743 m
percentil  99:  2,377 m
percentil 100:  2,859 m
```

**Lo que hay que entender antes de implementarlo:** "misma celda res 7" no
es "tolerancia de 1.5 km". Es una tolerancia que **varía entre 0 y 2.86 km
según dónde caiga el negocio en la retícula**. Dos situaciones idénticas
reciben respuestas distintas por su posición en una grilla que no tiene
relación con la geografía mexicana: dos negocios a 10 m, separados por una
frontera de hexágono, nunca matchean.

Y no hay resolución que resuelva las dos cosas: bajar la pérdida infla la
tolerancia máxima y apretarla sube la pérdida. Es intrínseco a usar una
grilla fija como proxy de distancia.

**Veredicto: entra como candidato, no como reemplazo.** Es genuinamente
diverso (su error depende de la geometría de la grilla, no de la distancia),
es baratísimo, y por eso mismo merece pasar por el arnés. Si da F1
comparable con un algoritmo 50× más simple, la anisotropía es un precio
defendible **y el número es la defensa**. Si no, queda documentado como
camino explorado, que también es resultado.

**Detalle de implementación:** `h3_cell_7 = cell_to_parent(h3_cell_9, 7)`,
**derivado del padre y nunca calculado del punto** — es la misma regla que
ya documenta `h3_cell_5` (AGENTS.md, sección H3), porque ~6.5% de los puntos
cerca de fronteras caen en una celda de resolución gruesa que no es la
ancestra de su celda res 9. Sale gratis: no requiere re-ingestar nada.

---

## 6. El consenso: cómo se combinan

"Promediar particiones" no es una media aritmética. Es *consensus
clustering*, y tiene una parte mecánica y un residuo.

### 6.1 Voto por pares (determinista)

Cada método se expresa como un conjunto de **enlaces**: el par de
observaciones `(id_i, period_i)` y `(id_j, period_j)` está ligado si ambas
comparten `clee_resuelto` en ese método. Con `k` métodos admitidos, un
enlace entra al consenso si lo afirman **≥⌈k/2⌉**. Con tres métodos, 2 de 3.

Es un `GROUP BY` sobre el `UNION ALL` de los pares de cada método. Sin LLM,
sin heurística, y auditable: cada enlace del consenso sabe cuántos métodos
lo sostienen.

### 6.2 La trampa: los enlaces votados no son transitivos

Puede salir `A~B`, `B~C` y `A≁C`. Un `union-find` ingenuo mete a A, B y C
en el mismo negocio y el error se propaga en cadena — así se fusionan
cuadras enteras y la N del estudio se desploma.

**Ese triángulo inconsistente es el único trabajo que un agente tiene
aquí**, y es un trabajo real: un conjunto chico, bien definido, donde
ninguna regla tiene la respuesta y un humano sí la tendría mirando los
nombres y los giros. Misma forma que el adjudicador de `crear_agentes.md`
§9.1 —un paso, sin herramientas, determinismo requerido— y reusa su
contrato y su tabla de auditoría.

El tamaño de ese residuo **se mide**, no se estima: sale de contar
triángulos inconsistentes sobre los enlaces votados, y hay que medirlo
antes de escribir el agente. Es la misma disciplina que
`normalizacion_gold.md` §1 impuso para el reconciliador, con el mismo
motivo: ahí la franja resultó ser cero.

### 6.3 Por qué esto no contradice el descarte de `todo.md`

`todo.md` descartó el clustering global porque cambia la semántica de
asignación y **rompe la comparabilidad columna a columna**. Esa objeción es
correcta y sigue en pie — para Silver.

El consenso no vive en Silver. **Las tablas `clee_<metodo>` se quedan
exactamente como están**, cada una con su ventana deslizante y su semántica
intacta, individualmente comparables contra el arnés. El consenso es un
artefacto de **`gold`**, donde el grano ya es el negocio y la
comparabilidad entre métodos dejó de ser el objetivo porque el objetivo es
publicar una identidad.

Eso disuelve la tensión: Silver conserva los votos auditables, Gold publica
el veredicto, y `normalizacion_gold.md` §8 ya tiene el campo para decir de
dónde salió cada fila (`origen_identidad`: `ACUERDO` | `REGLA` | `AGENTE`).

### 6.4 Dos papeles distintos para un LLM, y conviene no mezclarlos

Un LLM puede entrar aquí por dos puertas que no son la misma, y la
arquitectura ya contempla la primera:

| | **Miembro del ensemble** | **Árbitro del consenso** |
|---|---|---|
| Qué es | Un método completo: `silver.clee_llm` | El desempate de los votos |
| Dónde escribe | **Silver**, su propia tabla por método | Nada; alimenta la fila de Gold |
| Qué decide | cuál candidato es el mismo negocio | si `A~B~C` es un negocio o dos |
| Volumen | ~300k decisiones (la banda ambigua, no los 82.9M) | los triángulos inconsistentes (§6.2), sin medir todavía |
| Se mide con | el arnés de CLEE enmascarado, como cualquier método | el F1 del consenso completo |
| Estado | Diseñado: `crear_agentes.md` §§1–8. `arquitectura.md`:15 lo llama "tercer método de resolución" y `docs/separacion_ingest_clee.md` ya reservó la tabla | Propuesto en §6.2 |

El primero es el que `crear_agentes.md` diseñó en detalle, y es el que tiene
el techo ya calculado (§7): compite por el 43.7% del error, +0.98 puntos. Lo
que lo hace factible es que **no corre sobre los 82.9M**: solo sobre la
banda ambigua donde la cascada no decide con confianza.

**La caución, si se usan los dos papeles a la vez:** que el árbitro no sea
el mismo modelo con el mismo prompt que el miembro. Si lo es, el consenso
hereda el sesgo de ese modelo dos veces —una al votar y otra al desempatar—
y el voto mayoritario deja de ser una salvaguarda. Si hacen falta los dos,
que difieran en modelo o en prompt, y que la diferencia quede registrada en
`prompt_version` dentro de `meta.agent_decisions`.

---

## 7. El techo está medido: dimensionar antes de construir

`plan.md` §2, vía `crear_agentes.md` §1, ya cuantificó el error que queda
tras el `argmax` sobre la muestra enmascarada: **2.31%** de 920,699 filas.
Y lo partió por quién lo puede arreglar:

| | % del error | Quién lo arregla |
|---|---:|---|
| Gemelos reales (features idénticas) | **56.3%** | **Solo asignación 1:1.** Nombre idéntico en 97.8% y misma distancia exacta en 99.3%: no hay señal que interpretar. Ningún método, ensemble ni LLM distingue dos registros idénticos |
| Alguna feature difiere | 43.7% | Un scorer mejor, o un ensemble |

**El ensemble compite por el 43.7%, con un techo proyectado de 98.70%
contra 97.72% actual: +0.98 puntos de F1.**

Eso no invalida el plan, lo dimensiona. Construir tres métodos diversos más
el consenso más el agente para perseguir ~1 punto vale la pena **si la
contribución metodológica es el punto de la tesis** —"comparé cinco
resolvedores y un esquema de consenso, y reporté sus perfiles de error"— y
no vale la pena si el objetivo es solo una tabla mejor.

Y hay una pieza más barata que da más: **la asignación 1:1 global de
`plan.md` §4 ataca el 56.3%**, que es el pedazo grande y el que ningún
método nuevo puede tocar. Si hubiera que elegir una sola cosa, es esa.

---

## 8. Orden de construcción

1. **Retirar `h3` del conjunto de votos** (no borrarlo: queda como la
   implementación rápida de `radio` si la comparación de tiempos lo
   respalda — `meta.clee_resolution_tasks.duration_seconds` ya tiene el
   dato). Con eso, correr el pipeline deja de costar ~14h de más.
2. **Un método diverso, el más ortogonal: C (`clee_nombre`).** Medir su
   desacuerdo contra `radio` (§2.1) y su F1 (§2.2). **Si el desacuerdo sale
   <1%, el plan de ensemble se detiene aquí** — significaría que el criterio
   espacial no era el factor limitante.
3. **Un segundo método diverso**: A (`clee_prob`) o D (`clee_celda_h3_7`). D es
   mucho más barato de implementar; A es más citable.
4. **Medir la matriz de desacuerdos** entre los tres y reportarla. Es un
   resultado publicable por sí mismo, independientemente del consenso.
5. **Voto por pares** (§6.1) y conteo de triángulos inconsistentes (§6.2).
6. **Solo entonces**, el agente para el residuo — si el conteo del paso 5
   dice que existe.
7. **Validar el consenso** con el arnés: ¿tiene mejor F1 que el mejor
   método individual? Si no, el consenso no entra a Gold y el mejor método
   individual se publica solo.

Y la regla que gobierna todo el orden: **`gold.negocios_clee_oficial` se
construye primero y sin nada de esto** (`gold_cohorte_2021.md`). Sobre los
cortes con CLEE oficial no hay identidades propagadas que reconciliar, así
que el consenso es una mejora posterior sobre una capa que ya funciona — y
su valor real es **extender la ventana a 2015-2020**, no unificar
identidades.

---

## 9. Lo que este documento NO hace

- **No rediseña los métodos A, B y C**: su diseño está en `todo.md` Fase 7 y
  no se duplica aquí.
- **No re-litiga los descartes** de `todo.md` (embeddings, clustering global
  en Silver).
- **No diseña el protocolo de evaluación**: es el de `todo.md` Fase 7, y se
  usa tal cual.
- **No decide si el ensemble se construye.** Eso lo decide el paso 2 de §8,
  y el techo de §7 es el marco con que se juzga el resultado.
- **No implementa nada.**
