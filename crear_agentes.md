# Plan — agentes LLM sobre el warehouse

**Estado:** propuesto, nada implementado. Decidido el 2026-10-26 escribirlo
antes de tocar código.

**Qué entrega:** la arquitectura de `src/agents/` para tres agentes de forma
distinta (§9), y el primero de ellos completo — un adjudicador que puebla
`silver.clee_llm` con un modelo en Ollama sobre la RTX 5090 de indominus,
comparable contra `clee_radio` y `clee_h3` con el mismo arnés de evaluación.

Las §§1–8 son el adjudicador CLEE. La **§9 es la arquitectura compartida** y
manda sobre lo que digan las secciones anteriores si algo difiere.

---

## 1. El techo está medido, y es acotado

Antes de diseñar nada: `plan.md` §2 ya cuantificó dónde está el error que
queda, y eso decide el alcance.

Error total tras `argmax`: **2.31%** de las 920,699 filas de la muestra
enmascarada. De los 20,579 `falso_match` con ambos registros localizables:

| | Filas | % | Quién lo puede arreglar |
|---|---:|---:|---|
| Features idénticas (gemelos reales) | 11,577 | 56.3% | **Solo asignación 1:1** |
| Alguna feature difiere | 9,002 | 43.7% | Un scorer mejor |

**Un LLM no puede tocar el 56.3%.** Esos casos tienen nombre literalmente
idéntico (97.8%) y están a la misma distancia exacta (99.3%): no hay señal
que interpretar, los inputs son iguales. Ningún modelo —LLM, XGBoost o
humano— distingue dos registros idénticos. Eso lo arregla la asignación
global 1:1 de `plan.md` §4, que es otro problema.

El LLM compite por el **43.7%**, y el techo proyectado de un scorer mejor es
**98.70% contra 97.72% actual: +0.98 puntos.**

### Por qué un LLM y no XGBoost

`arquitectura.md` §2.3.3 ya nombra al candidato natural para ese hueco: un
scorer aprendido (XGBoost/LightGBM con CUDA) más *embeddings de nombres de
negocio* "para los casos donde el nombre difiere pero el negocio es el mismo
— es la señal que al scorer actual le falta".

Esa frase es exactamente el caso de uso del LLM:

```
nom_estab A: "TAQUERIA EL FOGON"
nom_estab B: "TACOS EL FOGON SA DE CV"
rapidfuzz: ratio bajo  →  no match
humano:    obviamente el mismo negocio
```

| | LLM vía Ollama | XGBoost sobre features |
|---|---|---|
| Datos de entrenamiento | **ninguno** | hace falta etiquetar |
| Semántica de nombres | sí, nativa | solo vía embeddings aparte |
| Costo por decisión | ~50–200 ms | microsegundos |
| Reproducibilidad | hay que forzarla | determinista |
| Defendible en la tesis | sí, si se versiona todo | sí |

La ventaja decisiva del LLM es que **no necesita ground truth para
arrancar**, y el ground truth es justo lo que no tienes para 82.9M filas.
La desventaja es el costo por decisión, y eso obliga a targetear.

**Los dos no son excluyentes.** El LLM puede generar las etiquetas con las
que después se entrena el scorer barato. Esa es probablemente la ruta más
fuerte para la tesis, pero es la fase 2 de esto.

---

## 2. Es un adjudicador, no un agente

Pediste "agentes", y el nombre de la carpeta se queda, pero lo que sirve
aquí **no** es un agente en el sentido de tool-use y pasos libres:

- Un agente que puede decidir cuántas herramientas llama y en qué orden
  **no es reproducible**. Para una tesis eso es fatal: el revisor pide
  re-correr y los números cambian.
- El trabajo es una clasificación binaria con contexto corto: dado un
  registro y 1..N candidatos vecinos, ¿cuál es el mismo negocio, o ninguno?
- Sin pasos, sin herramientas, sin memoria entre llamadas: prompt fijo,
  salida estructurada, `temperature=0`, `seed` fijo.

**El LLM no genera candidatos.** Eso ya lo hacen BallTree y H3 en
microsegundos; pedirle a un modelo que busque en 82.9M filas es absurdo. El
LLM solo **decide** entre candidatos que la etapa espacial ya acotó.

### Dónde se inserta

```
_propagate_single_kb(df_pending, kb, tol, columna, neighbor_finder)
  └─ por fila: vecinos = neighbor_finder(...)      ← no cambia
       └─ _best_candidate(vecinos)                 ← AQUÍ
            ├─ calculate_match_score (rapidfuzz)   ← decide la mayoría
            └─ si queda en la banda ambigua  →  adjudicador LLM
```

El punto de inserción es `_best_candidate` de
`src/etl/utils/entity_resolution.py`, no el generador de vecinos. Hoy esa
función calcula `best_score` y lo descarta (ver
`docs/separacion_ingest_clee.md` §4, paso 1): hay que empezar por
devolverlo, porque **el score es lo que define la banda ambigua**.

### La banda ambigua

El LLM solo se invoca cuando el scorer determinista no decide con holgura:

```
score >= UMBRAL_ALTO   →  match, sin LLM
score <= UMBRAL_BAJO   →  no match, sin LLM
en medio               →  LLM adjudica
```

Los dos umbrales son calibración (`.env`), y su ancho controla
directamente el costo de la corrida. **Elegirlos es una medición, no una
corazonada** — es el paso 1 de la §6.

---

## 3. Dónde vive el adjudicador

Sí, va en `src/`. La estructura completa de los tres agentes está en la
**§9.3**; para el adjudicador son dos archivos:

```
src/shared/llm/              ← cliente Ollama: infraestructura, sin dominio
src/agents/clee/
    adjudicador.py           ← (registro, candidatos) -> decisión
    prompts/clee_v1.txt      ← versionado; su hash va en la tabla
```

Lo que importa no es la carpeta sino la dirección de las dependencias: el
**ETL invoca al agente**, no al revés, y el adjudicador **no importa
`src.etl`**. Recibe el registro y los candidatos como datos planos, porque
quien lo llama ya normalizó y generó los vecinos. Al revés habría un ciclo.

## 4. La tabla `silver.clee_llm`

`src/shared/db/models/clee.py` ya la anticipa. Hereda `ResolucionCleeMixin`
—`(id, period)`, `clee_resuelto`, `origen`, `resuelto_en`, `LIST (period)`—
y agrega **solo lo que el método produce**:

| columna | por qué |
|---|---|
| `modelo` | `qwen2.5:7b`, etc. Sin esto el resultado no es citable. **`NULL` = lo decidió la cascada determinista, no el LLM** |
| `prompt_version` | hash o etiqueta del prompt. Cambiar el prompt cambia los resultados |
| `confianza` | lo que el modelo reporta; permite curvas precisión/cobertura |
| `candidatos_evaluados` | cuántos vecinos vio. Un 1 no es lo mismo que un 8 |
| `latencia_ms` | el costo real, para reportarlo |

El `origen` **no necesita un valor nuevo**: sigue siendo `PROPAGADO` (heredó
una identidad) o `NUEVO` (sintética). Quién decidió se lee en `modelo IS NOT
NULL`, que es más preciso que un enum y no toca `OrigenClee`.

Registro en `CLEEResolver.METHODS`:

```python
"llm": {"table": "clee_llm", "propagate": propagate_clees_llm, "prefix_suffix": "LLM"}
```

Con eso `transform --only clee-llm` y `clee_propagation_stats --method llm`
funcionan sin tocar el orquestador ni el script de métricas — los dos se
derivan del registro.

---

## 5. Ollama en indominus

Ya hay un Ollama corriendo ahí (`wearchai-ollama-1`), de otro proyecto. **No
reusarlo:** acopla dos proyectos y su ciclo de vida no lo controlamos.

Servicio propio en `docker-compose.yml`, con la URL por `.env` para poder
apuntar a otro si hace falta:

```yaml
  ollama:
    image: ollama/ollama:latest
    profiles: ["llm"]            # no entra con un `up -d` normal
    volumes:
      - ollama_models:/root/.ollama
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

```bash
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=qwen2.5:7b
OLLAMA_TIMEOUT_SECONDS=30
OLLAMA_MAX_BATCH=16
```

### Esto cambia una decisión de arquitectura

`arquitectura.md` §2.3.3 dice **"la GPU no participa en el ETL"**, con
razones válidas: Postgres no la usa, Polars/rapidfuzz/H3 son CPU puro, y el
cuello es IO y RAM. Eso sigue siendo cierto **para el ETL como está**. Un
adjudicador LLM es la primera etapa que sí la usa, así que hay que
actualizar esa sección en vez de dejarla contradiciendo al código.

**Nota de hardware que ya está documentada y aplica aquí:** la RTX 5090 es
Blackwell `sm_120` y requiere CUDA 12.8+. Ollama trae sus propios binarios,
así que debería estar bien, pero es lo primero a verificar — un contenedor
viejo de Ollama no reconoce la arquitectura y cae a CPU **en silencio**,
que es el peor modo de fallo: funciona, pero 50× más lento.

Verificación obligatoria antes de medir nada:

```bash
docker compose --profile llm run --rm ollama ollama list
docker compose exec ollama nvidia-smi        # ¿ve la GPU?
# y durante una llamada: nvidia-smi debe mostrar el proceso
```

---

## 6. Pasos

Cada uno es un commit verificable, y los tres primeros **no escriben nada en
la base**.

- [ ] **1. Medir la banda ambigua.** Antes de cualquier código de LLM:
      hacer que `_best_candidate` devuelva `best_score` y volcar la
      distribución de scores de los casos que hoy acaban en `NUEVO` o en
      desacuerdo radio/h3.

      **Esto decide si el proyecto vale la pena.** Entregable: cuántas
      decisiones caen en la banda para cada par de umbrales. Si son 300k,
      son horas; si son 6M, el diseño no sirve y hay que targetear más fino.

- [ ] **2. Cliente Ollama** (`src/shared/llm/`). Con las lecciones que ya
      costaron caro en este repo:
      - **Deadline total por llamada**, no solo timeout entre tokens — es
        el mismo bug que colgó las descargas 16 horas.
      - `temperature=0` y `seed` fijo, ambos registrados.
      - Salida estructurada validada; una respuesta que no parsea es un
        fallo, no un `None` silencioso.
      - **Cache por hash del prompt.** Los nombres de negocio se repiten
        muchísimo entre cortes: la misma pareja no se pregunta dos veces.
        Es lo que puede bajar el costo un orden de magnitud.

- [ ] **3. Adjudicador** (`src/agents/clee/adjudicador.py`). Función
      pura sobre `(registro, candidatos) -> decisión`, probable sin servidor
      con un cliente de mentiras. Batch de hasta `OLLAMA_MAX_BATCH`.

- [ ] **4. Evaluar contra el ground truth que YA tienes.**
      `scripts/evaluate_masked_clee.py` enmascara CLEEs reales y mide
      aciertos. Correrlo con el adjudicador activo y comparar contra el
      97.72% actual, **sobre la misma muestra**.

      Criterio de aceptación: si no mejora el 97.72% de forma medible, el
      método no entra a Silver. Mejor saberlo con 920k filas de muestra que
      con 82.9M.

- [ ] **5. Tabla y método** (`silver.clee_llm` + migración + entrada en
      `METHODS`). Solo después del paso 4.

- [ ] **6. Corrida completa** y comparación de los tres métodos con
      `clee_propagation_stats --method radio --method h3 --method llm`.

- [ ] **7. Actualizar `arquitectura.md`** §2.3.3 (la GPU ya participa) y
      `docs/separacion_ingest_clee.md` (tercer método, como se anticipó).

---

## 7. Riesgos

| Riesgo | Mitigación |
|---|---|
| **Cae a CPU sin avisar** y la corrida tarda 50× | Verificar `nvidia-smi` durante una llamada real, no solo que el contenedor arranque. Registrar `latencia_ms` por decisión: un salto delata el fallback |
| **No reproducible**: re-correr da otros números | `temperature=0`, `seed` fijo, `modelo` y `prompt_version` en cada fila. Cambiar el prompt obliga a re-correr, y la tabla lo dice |
| **El volumen no cierra**: la banda es de millones | El paso 1 lo mide antes de escribir el adjudicador. Palancas: estrechar la banda, o limitar el LLM a los desacuerdos radio/h3 |
| **El modelo alucina un CLEE** | La salida es un **índice del candidato** o "ninguno", nunca un CLEE libre. El CLEE lo pone el código desde la KB |
| **No mejora nada** | Es un resultado válido y publicable: acota el techo del 43.7% y refuerza que lo que falta es estructura (asignación 1:1), no señal |
| Modelo de 7B demasiado débil | La comparación del paso 4 es barata; probar 2–3 tamaños antes de comprometerse |

---

## 8. Lo que este plan NO hace

- **No toca `clee_radio` ni `clee_h3`.** Es una tabla nueva: ese es
  justamente el punto del diseño de `docs/separacion_ingest_clee.md`.
- **No persigue el 56.3% de gemelos reales.** Eso es asignación 1:1
  (`plan.md` §4), y ningún scorer lo resuelve.
- **No reemplaza el método de referencia.** `entidades_economicas` sigue
  derivándose de `clee_radio` (`IDENTITY_TABLE` en
  `src/etl/loaders/entidades.py`). Cambiar la unidad de análisis es una
  decisión aparte, y se toma **con** los resultados de la comparación.

---

## 9. Arquitectura para varios agentes

Esta sección existe porque el adjudicador CLEE es el primero de **tres**, y
los tres tienen formas distintas. Diseñar para el primero solamente llevaría
a rehacer todo en el segundo.

### 9.1 Los tres agentes no son el mismo animal

| | **1. Adjudicador CLEE** | **2. Verificador de supervivencia** | **3. Analista** |
|---|---|---|---|
| Qué decide | cuál candidato es el mismo negocio | si el negocio cerró, y cuándo abrió | responde preguntas de análisis |
| Pasos | **uno, fijo** | varios, con ramas | varios |
| Herramientas | **ninguna** | búsqueda web, Maps, redes | SQL acotado sobre `gold` |
| Datos | **en el prompt**, los pone el código | los **busca** el agente | los **consulta** el agente |
| Volumen | ~300k decisiones | **cientos** (muestra) | decenas por sesión |
| Invocación | batch, desde el transform | batch, desde un comando | interactiva |
| Escribe en | `silver.clee_llm` | `gold.supervivencia_verificada` | nada |
| Determinismo | requisito | imposible (la web cambia) | no aplica |

El 1 es un **clasificador**. El 2 es un **agente de verdad**: no sabe qué va
a encontrar y tiene que decidir qué buscar después. El 3 es un **cliente**
de herramientas que nosotros exponemos.

Mi recomendación anterior de "nada de frameworks" era correcta para el 1 y
**no** se extiende al 2.

### 9.2 La regla que hace reutilizable todo esto

> **El contrato es nuestro; el framework es un detalle de implementación de
> un agente.**

Cada agente depende de `src/agents/base.py`, no de Agno ni de LangGraph. Si
Agno resulta un callejón, cambia **un** módulo y los otros dos agentes no se
enteran. Si al revés resulta excelente, el adjudicador no tiene por qué
adoptarlo: para 300k decisiones de un paso sigue siendo sobrecarga.

Esa inversión es lo mismo que ya hiciste con `METHODS` en el resolver: el
orquestador no sabe si es radio o H3, y agregar un método es una entrada en
un registro.

### 9.3 Estructura

```
src/
├── shared/
│   └── llm/                      ← infraestructura, sin dominio ni tools
│       ├── client.py             ← Ollama HTTP, deadline total, reintentos
│       ├── schema.py             ← pydantic -> JSON Schema para `format`
│       ├── cache.py              ← memoización por hash de entrada
│       └── budget.py             ← tope de llamadas, tiempo y costo POR CORRIDA
│
├── agents/                       ← paquete nuevo de primer nivel
│   ├── base.py                   ← Agente (Protocol), Decision, Evidencia
│   ├── registry.py               ← nombre -> clase; de aquí sale el CLI
│   ├── clee/                     ← #1  batch, sin tools
│   │   ├── adjudicador.py
│   │   └── prompts/clee_v1.txt
│   ├── supervivencia/            ← #2  tools externas, multi-paso
│   │   ├── verificador.py
│   │   ├── fuentes/              ← una por archivo: busqueda, maps, redes
│   │   └── prompts/
│   └── analista/                 ← #3  consume las tools de src/mcp
│       └── analista.py
│
└── mcp/                          ← servidor FastMCP sobre gold y meta
    ├── server.py
    └── consultas.py              ← SELECT parametrizados, nunca SQL del modelo
```

**Por qué `src/agents` es de primer nivel y no `src/etl/agents`:** solo el
adjudicador pertenece al ETL. El verificador escribe en `gold` y el analista
no toca el ETL en absoluto, asi que meter a los tres bajo `etl/` mentiría
sobre lo que son.

**Quién llama a quién** importa más que dónde viven:

```
src.etl.transformers.clee_resolver  →  src.agents.clee        (el ETL invoca al agente)
src.agents.*                        →  src.shared, src.meta   (y nada mas)
src.mcp                             →  src.shared, src.meta
```

El adjudicador **no importa `src.etl`**, y eso no es casual: recibe el
registro y los candidatos como datos planos. La normalización y la
generación de candidatos ya las hizo quien lo llama. Si necesitara
`src.etl.utils.matching` habría un ciclo.

Fronteras a declarar en `tests/unit/test_layer_boundaries.py` (el test falla
hasta que se hagan, a propósito):

```python
"src.agents": ("src.api", "src.etl", "src.scheduler", "src.mcp"),
"src.mcp":    ("src.api", "src.etl", "src.agents", "src.scheduler"),
"src.etl":    ("src.api", "src.scheduler", "src.mcp"),   # etl -> agents SÍ se permite
```

### 9.4 El contrato

```python
class Evidencia(BaseModel):
    """De dónde salió la decisión. Para el agente 2, el snapshot crudo."""
    fuente: str          # "prompt" | "serpapi" | "google_maps" | ...
    referencia: str      # URL, o el hash del prompt
    contenido_hash: str  # hash del contenido guardado

class Decision(BaseModel):
    """Lo que devuelve TODO agente, sin importar su forma."""
    veredicto: Any
    confianza: float
    evidencia: list[Evidencia]
    modelo: str
    prompt_version: str
    latencia_ms: int

class Agente(Protocol):
    nombre: str
    version: str
    def decidir(self, entrada: Any) -> Decision: ...
```

Y **una sola tabla de auditoría para los tres**, porque la forma del registro
es idéntica:

```
meta.agent_decisions
    agente, version, modelo, prompt_version,
    entrada_hash, veredicto (jsonb), confianza,
    evidencia (jsonb), latencia_ms, decidido_en
```

Eso sirve a tres cosas a la vez: **caché** (lookup por `entrada_hash`),
**auditoría** (qué decidió cada agente y con qué vio) y **reproducibilidad**
(re-correr con la caché poblada da los mismos números, que es la única
garantía real — ver §7).

### 9.5 Tecnología por agente

| Agente | Tecnología | Por qué |
|---|---|---|
| **1. CLEE** | `requests` + pydantic + `ThreadPoolExecutor` | Un paso, sin tools. Un framework solo agrega latencia por decisión ×300k |
| **2. Supervivencia** | **Agno** detrás de `base.Agente` — con bucle propio como punto de partida | Tiene tools y ramas, que es lo que un framework de agentes resuelve. Pero el conjunto de tools es chico (3–4), así que arrancar con un bucle propio de ~100 líneas mantiene el trace auditable. Se adopta Agno si el bucle se vuelve el cuello |
| **3. Analista** | **FastMCP** | Es literalmente para lo que existe MCP: exponer tools a un cliente LLM. No tiene sentido escribirlo a mano |

**LangGraph** queda fuera por ahora: su valor diferencial son los ciclos con
checkpointing, y el checkpointing ya lo tenemos en Postgres —`meta` y el
swap de particiones— que es donde queremos que viva el estado de una corrida
de horas, no en un store del framework.

### 9.6 El analista: dos consumidores, una definición de tools

```
src/mcp/consultas.py   ← los SELECT, parametrizados y acotados
          │
          ├── src/mcp/server.py (FastMCP)  →  Claude Desktop / IDE
          └── src/agents/analista           →  Ollama local, vía CLI o API
```

Las consultas se definen **una vez** y las usan los dos. Y nota lo que esto
implica: con el servidor MCP conectado a Claude Desktop, el agente 3 **ya no
hace falta** para el caso interactivo — Claude es el agente y nosotros solo
damos las herramientas. El `src/agents/analista` tiene sentido solo si
quieres respuestas sin salir del stack local.

**Nunca SQL generado por el modelo.** Las tools son funciones con parámetros
tipados (`negocios_desaparecidos(entidad, periodo_desde, periodo_hasta)`),
no un `ejecutar_sql(texto)`. El motivo está medido en este mismo repo: dos
consultas que yo escribí a mano tumbaron la corrida —el anti-join que
desbordó `/dev/shm` y el `SELECT DISTINCT` que se auto-bloqueó—. Un modelo
generando SQL contra 82.9M filas es la peor idea del diseño.

El rol **`api_ro`** ya acota el daño: `SELECT` solo en `gold` y `meta`, nunca
`silver`.

### 9.7 El agente 2 es un instrumento de muestreo, no un censo

Esto es lo más importante de su diseño, y sale de `docs/findings.md` §2:

| Par de cortes | Identidades | Desaparecen | % |
|---|---:|---:|---:|
| 2017-11 → 2018-03 | 2,100,277 | 21,267 | 1.0 |
| 2018-11 → 2019-04 | 2,168,018 | 42,039 | 1.9 |
| **2019-04 → 2019-11** | 2,246,283 | **922,285** | **41.1** |

La tasa aparente de desaparición oscila por un **factor de 400**. La
hipótesis documentada es que las desapariciones masivas son reasignación de
claves, no cierres.

**922,285 negocios no se verifican con búsquedas web.** Ni por costo ni por
tiempo ni por términos de servicio. El agente 2 existe para **estimar**: una
muestra estratificada de cientos de negocios por par de cortes, con la que
se calibra qué fracción de las "desapariciones" son cierres reales.

Eso cambia su tabla destino: no es "el estado de cada negocio" sino
`gold.supervivencia_verificada`, con los cientos verificados y su evidencia,
que es lo que sostiene una afirmación sobre los millones.

**Y obliga a guardar la evidencia cruda, no solo el veredicto.** Los
resultados de búsqueda cambian con el tiempo: sin el snapshot del HTML/JSON
que el agente leyó, en seis meses la tesis no es re-verificable. El
`contenido_hash` de `Evidencia` apunta a ese snapshot.

### 9.8 Riesgos propios del agente 2

| Riesgo | Mitigación |
|---|---|
| **Ausencia de evidencia ≠ cierre.** Una taquería chica no aparece en ningún lado | El veredicto tiene que admitir `SIN_EVIDENCIA` como valor de primera clase, distinto de `CERRADO`. Codificar lo primero como lo segundo inventaría cierres |
| **Costo**: Google Places y las APIs de búsqueda se pagan por llamada | `shared/llm/budget.py` con tope duro por corrida. El muestreo mantiene el orden en cientos, no millones |
| **Términos de servicio**: raspar resultados de Google o redes sociales los viola y termina bloqueado | Usar APIs oficiales con llave, respetar `robots.txt`, y documentar la fuente de cada dato en la tesis |
| **No reproducible**: la web de hoy no es la de ayer | Guardar el snapshot crudo, fechado. Es un requisito, no una optimización |
| **Sesgo de visibilidad**: los negocios grandes dejan rastro y los chicos no — y los chicos son el objeto de estudio | Medirlo: reportar la tasa de `SIN_EVIDENCIA` por estrato de tamaño. Si es alta en los micro, el instrumento tiene un límite que hay que declarar |

### 9.9 Orden de construcción

No es el orden en que los enumeraste, y el motivo es dónde está el riesgo:

1. **`shared/llm/` + `agents/base.py` + `meta.agent_decisions`.** Los tres
   agentes dependen de esto. Es poco código y define el contrato.
2. **Agente 1 (CLEE)**, siguiendo los pasos de la §6. Es el que tiene
   criterio de aceptación objetivo —el arnés de CLEEs enmascarados— así que
   valida la infraestructura contra algo medible.
3. **`src/mcp/` con dos o tres consultas.** Barato, y te da valor inmediato
   para explorar los resultados del agente 1.
4. **Agente 2**, al final. Es el de más incógnitas abiertas: fuente de
   datos, costo, diseño del muestreo y el límite ético. Y **depende de
   Gold**, que todavía no existe (Fase 5).
