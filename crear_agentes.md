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
tiempo ni por términos de servicio — la aritmética está en §10.2. El agente
2 existe para **estimar**: muestra nacional estratificada más censo de un
municipio (§10.4), con lo que se calibra qué fracción de las
"desapariciones" son cierres reales.

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

---

## 10. Alcance: censo contra muestreo, con la aritmética

Esta sección documenta la discusión del 2026-10-26 sobre si los agentes
deben recorrer **todos** los negocios. La conclusión es distinta para cada
uno, y en los dos casos la decide un número, no una preferencia.

### 10.1 Agente 1 (CLEE): posible, y la medición decide

Recorrer las 31,392,164 filas que necesitan propagación **sí es posible**.
Un 7B en la 5090, prompt de ~600 tokens y salida de ~20:

| Throughput | 31.4M decisiones |
|---|---|
| 10 dec/s | **36 días** |
| 30 dec/s | **12 días** |
| 50 dec/s (optimista) | **7.3 días** |

Radio tardó 8.5 h, así que son 20–100×. La workstation está dedicada, así
que 12 días no es descalificante por sí mismo. **El problema es iterar**: un
cambio de prompt son otros 12 días, y es la primera vez que esto corre.

**Concesión al recorrido completo.** Targetear solo la banda ambigua tiene
un hueco real: atrapa los casos donde rapidfuzz *dudó*, no donde se equivocó
con **confianza alta**. `plan.md` §2 dice que el 43.7% de los `falso_match`
"tiene alguna feature que difiere", pero **no dice en qué parte del rango de
score cayeron**. Si están arriba, la banda no los ve y el recorrido completo
es la respuesta correcta.

Eso no se decide opinando. Se mide sobre la muestra enmascarada, que ya
tiene verdad conocida:

```
¿Cómo se distribuyen los 9,002 errores a lo largo del score?
  concentrados en la banda media  →  targetear: de 12 días a horas
  repartidos por todo el rango    →  recorrido completo justificado
```

**Decisión:** la corrida completa se hace **una vez, con el prompt ya
congelado**, después de validar en la muestra. No primero.

### 10.2 Agente 2 (supervivencia): el censo no es factible

Precios de lista, orden de magnitud, a verificar antes de comprometerse:

| Fuente | Costo/1000 | ~10.45M negocios | Límite diario |
|---|---|---|---|
| Google Custom Search | ~$5 | **$52,250** | 10k/día → **2.9 años** |
| Google Places (Details) | ~$17 | **$177,650** | — |
| SerpAPI | ~$10 | **$104,500** | — |
| Serper.dev | ~$1 | **$10,450** | — |

Raspar en lugar de pagar viola los ToS y termina en CAPTCHA a los pocos
miles.

**El problema de fondo no es el dinero, es el sesgo.** Una taquería de barrio
no tiene sitio, ni ficha de Maps con horarios, ni redes. Para ella el
resultado modal es `SIN_EVIDENCIA` — y eso **correlaciona con el tamaño del
negocio**, que es la variable de estudio. Un censo por búsqueda web mediría
bien a los grandes y mal a los micro, y produciría la conclusión de que los
micronegocios "no se pueden verificar": un artefacto del instrumento
disfrazado de hallazgo.

### 10.3 Por qué el muestreo es mejor instrumento, no un recorte

La pregunta de investigación no es *"¿cerró el negocio X?"* diez millones de
veces. Es:

> De las **922,285** identidades que desaparecen entre 2019-04 y 2019-11
> (41.1%, contra 1.0% en otros pares — `docs/findings.md` §2), ¿qué fracción
> son cierres reales y qué fracción es reasignación de claves?

Eso es una **proporción**, y las proporciones se estiman con muestras:

| n | Margen de error (95%) | Costo API | Tiempo |
|---|---|---|---|
| 1,000 | ±3.1% | ~$10 | horas |
| **2,400** | **±2.0%** | ~$24 | horas |
| 9,600 | ±1.0% | ~$96 | 1 día |

Con ±2% se distingue sin ambigüedad "el 41% son cierres reales" de "el 41%
es un artefacto de reasignación". Esa es toda la pregunta.

Y el muestreo compra algo que el censo **no puede**: con 2,400 casos se
puede **verificar bien cada uno** —varias fuentes, revisión manual de los
ambiguos, medir la tasa de `SIN_EVIDENCIA` por estrato—. Con 10M se tiene un
veredicto de una sola fuente, sin revisar, sobre datos donde la mayoría no
tiene presencia web. **Un muestreo bien hecho es evidencia más fuerte que un
censo mal hecho**, y aquí el censo no puede hacerse bien.

### 10.4 El diseño: caso-control en dos fases

Una muestra de 2,400 da **±2.0%** para una proporción nacional, y el tamaño
de la población es irrelevante —el mismo margen para 922,285 que para
10.4M—. Eso basta y sobra para distinguir "41% son cierres" de "41% es
artefacto".

Pero hay tres cosas que el tamaño de muestra **no** resuelve, y son las que
definen el diseño.

#### a. El margen es POR CELDA, así que la estratificación tiene que ser gruesa

| Estratificación | n/celda | Margen |
|---|---:|---:|
| 19 pares × 7 estratos de tamaño = 133 celdas | 18 | **±23%** inservible |
| 4 grupos de pares × 3 tamaños = 12 celdas | 200 | ±6.9% |
| 2 grupos × 3 tamaños = 6 celdas | 400 | ±4.9% |

Con 200 por celda se detectan diferencias de **14 puntos o más** entre
estratos. Alcanza si la brecha de verificabilidad es grande —lo que espero—
y no alcanza para matices.

**Decisión:** el par anómalo (2019-04 → 2019-11) más dos o tres pares de
control, y tres estratos de tamaño (micro / pequeña / mediana+). No 133
celdas.

#### b. Sin controles, la evidencia no se puede interpretar

```
casos:     identidades que DESAPARECEN entre el corte t y t+1
controles: identidades presentes en AMBOS  →  no cerraron, por definición
```

Si los controles también vuelven `SIN_EVIDENCIA` al 70%, entonces
`SIN_EVIDENCIA` **no carga información sobre cierre** y el instrumento no
sirve para nada. La medición real es el **contraste entre los dos grupos**,
no la tasa en los casos.

#### c. La cobertura, no n, es la variable crítica

Si el 70% vuelve `SIN_EVIDENCIA`, lo que se estima es *"de los que pudimos
verificar, X% cerró"* — y ese subconjunto es sistemáticamente más grande,
más formal y más urbano. Es *missing not at random*: **una muestra de 10,000
con 70% de faltantes sigue sesgada.**

Las cotas honestas (Manski), si de los verificados el 50% cerró:

| Cobertura | La tasa real está entre | Ancho |
|---|---|---|
| 30% | 15% y 85% | **70%** — no responde nada |
| 50% | 25% y 75% | 50% |
| 70% | 35% y 65% | 30% |
| 90% | 45% y 55% | 10% |

**Segunda fase para estrechar las cotas.** El DENUE trae `telefono`:
llamar a ~150 de los `SIN_EVIDENCIA` permite estimar la tasa de cierre entre
los no verificables y bajar el ancho de 70 puntos a algo usable. Es el
patrón estándar de seguimiento de no respuesta.

#### d. Medir el sesgo hoy, sin gastar un peso

El DENUE ya trae `www`, `correoelec` y `telefono`. **La presencia de esos
campos predice la verificabilidad**, así que buena parte de la brecha por
estrato se mide con una consulta sobre Silver, antes de contratar nada:

```sql
select ep.per_ocu,
       count(*) as negocios,
       round(100.0*count(e.www) filter (where e.www <> '')/count(*), 1) as pct_con_web,
       round(100.0*count(e.telefono) filter (where e.telefono <> '')/count(*), 1) as pct_con_tel
from silver.establecimientos e
left join silver.denue_estratos_personal ep on ep.id = e.per_ocu_id
where e.period = '2019-11-01'
group by ep.per_ocu order by 2 desc;
```

Si confirma que los micronegocios casi no tienen web ni correo, el sesgo
queda cuantificado de gratis — y es un argumento más fuerte en la solicitud
a Meta, porque justifica por qué hace falta una fuente externa.

#### Resumen del diseño

1. **Medir** la cobertura esperada con `www`/`correoelec` por estrato (gratis).
2. **Fase 1**: caso-control, estratificación gruesa, ~2,400 en total → cascada
   de §11.6 (Places → búsqueda web → LLM).
3. **Fase 2**: llamadas a ~150 de los `SIN_EVIDENCIA` → estrecha las cotas.
4. **Censo de un municipio** como caso de estudio, solo si la fase 1 muestra
   que la cobertura lo justifica.

Estimación nacional con controles, más seguimiento de no respuesta, más caso
de estudio exhaustivo. Los tres son diseños estándar; lo que no es estándar
es saltarse los controles.

---

## 11. Inventario de fuentes para el agente 2

El canal que importa está bien identificado: **los cierres de negocios
establecidos se anuncian en redes sociales**. El problema es el acceso, que
cambió mucho desde 2018.

> Los trámites, URLs oficiales, costos y el presupuesto están en
> **`docs/apis_externas.md`**. Aquí va solo qué sirve y qué no.

### 11.1 El hallazgo: `business_status` de Google Places

Antes de cualquier LLM o búsqueda de texto, Places devuelve un campo
estructurado:

```
business_status:  OPERATIONAL | CLOSED_TEMPORARILY | CLOSED_PERMANENTLY
```

**Es el dato más valioso de toda esta sección** y no necesita interpretación:
es una bandera, no un texto que un modelo tenga que leer. Para los negocios
que tienen ficha, responde la pregunta directamente.

Limitaciones a medir, no a suponer: qué fracción de los negocios del DENUE
tiene ficha en Maps (sospecho que baja en micronegocios), y cuánto tarda
Google en marcar un cierre — el campo refleja *cuándo se enteró Google*, no
cuándo cerró el negocio. Para fechar la apertura sirve más la fecha de la
reseña más antigua.

### 11.2 Facebook e Instagram: el canal correcto, la puerta cerrada

| Vía | Estado | Sirve |
|---|---|---|
| Graph API, páginas de terceros | Requiere *Page Public Content Access* con App Review y verificación de negocio | Muy improbable para una tesis |
| Instagram Graph API | Solo cuentas que tú administras | No |
| Instagram Basic Display | Descontinuada (2024) | No |
| Búsqueda pública de FB | Retirada tras 2018 | No |
| CrowdTangle | **Cerrado en agosto de 2024** | No |
| **Meta Content Library** | Reemplazo de CrowdTangle, **para investigadores académicos** vía solicitud (ICPSR) | **Sí, si se obtiene acceso** |
| Raspado | Viola ToS, bloqueo agresivo | No |

**La Meta Content Library es la única vía legítima**, y existe justamente
para casos como este: investigación académica con afiliación institucional.
Vale la solicitud, pero es un trámite con tiempos propios, así que **no
puede ser una dependencia del camino crítico**. Diseñar asumiendo que no se
tiene, y sumarla si llega.

Señal indirecta y accesible: una página de Facebook cerrada suele aparecer
en los resultados de un buscador web con su último post o con reseñas que
dicen "ya cerró". Eso entra por la vía de búsqueda, no por la API de Meta.

### 11.3 Motores de búsqueda

| Fuente | Acceso | Nota |
|---|---|---|
| **Brave Search API** | Oficial, índice propio, capa gratuita + ~$3–5/1000 | **Primera opción**: sin zona gris de ToS |
| **Serper.dev** | ~$1/1000, resultados de Google | El más barato; servicio intermediario de Google |
| SerpAPI | ~$10/1000 | Más caro, muy establecido |
| Tavily / Exa | Diseñadas para agentes, devuelven contenido limpio | Útiles: menos post-procesamiento |
| **DuckDuckGo** | **Sin API oficial de resultados web** | El paquete `ddgs` raspa un endpoint HTML: se rompe seguido y limita por tasa. No apto |
| Bing Web Search | Microsoft movió/retiró estas APIs; verificar estado actual | No asumir disponibilidad |

Con la muestra de 2,400, Brave o Serper cuestan entre $3 y $12. El costo
deja de ser un criterio de diseño.

### 11.4 Fuentes oficiales mexicanas

Aquí hay algo importante para la tesis, y es incómodo:

| Fuente | Qué da | Para quién sirve |
|---|---|---|
| Registro Público de Comercio / SIGER | Disolución de sociedades | Solo las **constituidas** (SA de CV) |
| SAT — padrón de contribuyentes | Estatus del RFC | Sin consulta masiva por nombre |
| IMSS | Patrones registrados | Publica **agregados** por municipio y sector, no por negocio |
| DENUE (el propio) | Ausencia en un corte posterior | Es la señal que queremos validar, no sirve para validarse |

**Para los negocios formales y constituidos existen registros oficiales.
Para los micronegocios no existe ninguna fuente.** Y los micronegocios son
el objeto de estudio.

### 11.5 La verificabilidad está estratificada, y eso es un hallazgo

La conclusión de juntar §11.1–11.4:

```
negocio constituido, con ficha en Maps y presencia en redes
    →  verificable por varias fuentes independientes
micronegocio de barrio, sin RFC visible, sin ficha, sin redes
    →  NINGUNA fuente lo alcanza
```

Eso **no es una limitación que haya que esconder**: es un resultado
reportable. Decir *"la supervivencia de los micronegocios mexicanos no es
verificable con fuentes públicas, y aquí está la medición de esa brecha por
estrato"* es una contribución, y además justifica por qué el DENUE —con
todos sus defectos de reasignación de claves— sigue siendo la mejor fuente
disponible para ese segmento.

**Consecuencia de diseño:** el agente 2 reporta obligatoriamente
`SIN_EVIDENCIA` como valor de primera clase y **la cobertura por estrato de
tamaño** es una de sus salidas principales, no un diagnóstico secundario.

### 11.6 Cascada de verificación

Barato y estructurado primero; el LLM al final y solo donde hace falta:

```
1. Google Places business_status        ← bandera, sin LLM. Resuelve lo que puede
2. Si no hay ficha o dice OPERATIONAL dudoso:
   búsqueda web (Brave/Serper) por nombre + municipio
3. El LLM lee los resultados y decide:   CERRADO | ABIERTO | SIN_EVIDENCIA
   + fecha estimada + las URLs que lo sustentan
4. Meta Content Library, si hay acceso, solo para los ambiguos del paso 3
```

El LLM entra en el paso 3, que es donde aporta: interpretar texto libre
—reseñas, posts, notas— para distinguir "cerró definitivamente" de "cambió
de dirección" o "cerró por remodelación". Los pasos 1 y 2 no lo necesitan.

**Guardar el snapshot crudo de cada paso** (§9.7): los resultados de
búsqueda de hoy no son los de dentro de seis meses, y sin el snapshot la
tesis no es re-verificable.
