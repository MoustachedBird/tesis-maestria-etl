# Plan: más fuentes para el agente de supervivencia (v5)

Estado (2026-11-20): **F0–F4 implementadas** (v5), sin probar todavía en
indominus; F5 (PSM, DOF) y F6 (fachada con visión) pendientes, después de la
corrida chica que mide el tiempo por negocio. Parte de la v4 (`AGENTS.md`
N7): recolección base fija por fuente → especialista por fuente → decisor →
juez.

| Fase | Estado |
|---|---|
| F0 infraestructura (`b0012`, `Especialidad`, `AGENTE_FUENTES`, pausas, bloqueos, perfil de Chrome, `sondear`, recorte al contexto) | hecha |
| F1 buscadores (SearXNG con motores explícitos, Bing, DuckDuckGo, Google, Brave) | hecha |
| F2 Maps con reseñas recientes, OSM | hecha |
| F3 Internet Archive, Common Crawl, dominio, SAT | hecha |
| F4 plataformas (TripAdvisor, Yelp, Rappi...) | hecha; falta revisar qué directorios copian al DENUE |
| F5 PSM, DOF | pendiente: sondear primero si sus buscadores piden CAPTCHA |
| F6 fachada (Street View, Mapillary, KartaView + visión) | pendiente: decidir el modelo midiendo VRAM |
| F7 evaluación v4 vs v5, aporte por fuente, tiempos | `metricas` ya da los tiempos |

Lo que se cambió respecto al plan: la "ventana de relevancia" de §1.4 no se
hizo; en su lugar el expediente se recorta parejo para caber en el contexto
(`formatear_expediente(presupuesto=...)`). El paralelismo recolección/modelo
tampoco: el tiempo no es restricción y en serie es más simple de auditar.

Dos reglas de la petición:

1. **Lo gratuito se implementa por su API o descarga oficial.**
2. **Lo que solo tiene API de paga se lee como lo vería una persona**: Chrome
   headless sin sesión (Selenium, que ya está en la imagen `agente`) o
   `requests` sobre el HTML público, y **el LLM hace el trabajo que haría la
   API**: leer la ficha, ubicar el estado, las fechas y decidir si es el mismo
   negocio.

Todo se prueba en indominus; en la Mac solo `ruff`.

---

## 1. Qué cambia en la arquitectura

### 1.1 Recolector fino, especialista por grupo

Hoy hay 4 recolectores y un especialista por recolector. Con ~20 fuentes, un
especialista por fuente serían ~20 llamadas al modelo por negocio. Se separan
dos ejes:

| Eje | Qué es | Para qué |
|---|---|---|
| `Recolector` (fino) | La fuente concreta: `maps`, `buscador_bing`, `osm`, `wayback`… | El `GROUP BY` del análisis: qué aporta cada fuente |
| `Especialidad` (grupo) | El especialista que lee esas piezas | El costo: 7 especialistas, no 20 |

| Especialista | Recolectores que lee |
|---|---|
| **mapas** | `maps` (ficha + reseñas recientes), `osm` |
| **buscadores** | `buscador_searxng`, `buscador_bing`, `buscador_ddg`, `buscador_google`, `buscador_brave` |
| **redes** | `facebook`, `instagram` |
| **plataformas** | `tripadvisor`, `yelp`, `seccion_amarilla`, `delivery` (Rappi, Uber Eats, DiDi Food) |
| **huella_digital** | `sitio`, `wayback`, `common_crawl`, `dominio` (RDAP + DNS) |
| **registros** | `sat_69`, `psm`, `dof` (solo si hay razón social de persona moral) |
| **fachada** | `street_view`, `mapillary`, `kartaview`: imágenes leídas por un modelo de visión |

Un especialista sin piezas no llama al modelo, como hoy. El decisor y el juez
no cambian de forma: leen más informes.

### 1.2 Esquema (migración `b0012`)

- `Recolector` gana los valores nuevos y nace `Especialidad`
  (`src/shared/enums/verificacion.py`); `informes.especialista` pasa a
  `Especialidad`. Los `CHECK` se recrean desde los enums.
- `consultas` gana `imagen_ruta` (TEXT, NULL): la captura que vio el modelo de
  visión, guardada en `data/agente/capturas/<veredicto_id>/<orden>.png`. Sin
  ella, una pieza de la fachada no se puede auditar.
- Aditiva; la aplica el usuario con `docker compose up -d migrate`.

### 1.3 Configuración (`.env`, `.env.example`, `AGENTS.md`)

| Variable | Default | Qué |
|---|---|---|
| `AGENTE_FUENTES` | todas las de la fase implementada | Lista ordenada de recolectores activos; el orden es el de la recolección base |
| `AGENTE_VERSION` | `v5` | Separa los veredictos de v4 en `metricas` |
| `AGENTE_PAUSA_SEGUNDOS` | `3` | Pausa mínima entre dos consultas al mismo dominio (+ jitter) |
| `AGENTE_MAX_CAPTCHAS` | `3` | CAPTCHAs seguidos de un motor antes de apagarlo por el resto de la corrida |
| `MAPILLARY_TOKEN` | vacío | Token gratuito de Mapillary; vacío = la fuente se salta |
| `VISION_MODEL` | vacío | Modelo de visión; vacío = sin especialista de fachada (ver §5) |
| `AGENTE_RADIO_METROS` | `150` | Radio para buscar el negocio por coordenadas (OSM, Mapillary) |

### 1.4 Infraestructura común (antes de cualquier fuente)

- **Detector de bloqueo**: CAPTCHA, "unusual traffic", consentimiento de
  cookies, login → la pieza queda `error` con el motivo, **nunca** como una
  lectura vacía (misma regla que el muro de login de v2). Lo cuenta un
  cortacircuitos por motor (`AGENTE_MAX_CAPTCHAS`).
- **Perfil de Chrome persistente sin sesión** (`data/agente/chrome/`): guarda
  solo el consentimiento de cookies de Google/Bing para no toparse con la
  pantalla de "Antes de ir a Google" en cada consulta.
- **Ventana de relevancia**: de una página larga, en vez de los primeros
  `AGENTE_MAX_CARACTERES_PAGINA`, se toman los fragmentos alrededor de donde
  aparece el nombre, el teléfono o la calle. En código, sin LLM: la cita sigue
  validándose contra el texto exacto.
- **Paralelismo**: mientras el modelo lee el negocio *i*, un hilo recolecta el
  *i+1* (la recolección es red; el modelo, GPU). Es el mayor ahorro de tiempo
  disponible sin tocar la calidad.

---

## 2. Fuentes gratuitas, por su API o descarga

| Recolector | Endpoint | Qué aporta | Fecha citable |
|---|---|---|---|
| `osm` | Overpass (`overpass-api.de/api/interpreter`), nodos y vías con nombre a `AGENTE_RADIO_METROS` de las coordenadas, **incluidos** los prefijos `disused:`, `was:`, `abandoned:` | Busca por posición: separa homónimos. Un `disused:shop` es cierre explícito | `out meta`: fecha de la última edición del objeto |
| `wayback` | CDX (`web.archive.org/cdx/search/cdx`) sobre el sitio y las URLs de Facebook halladas; lee la captura más reciente (`id_`) | Último rastro archivado y qué decía | `timestamp` de la captura |
| `common_crawl` | Índice del crawl más reciente (`index.commoncrawl.org`) sobre las mismas URLs | Segundo rastreador, otra fecha | Fecha del crawl |
| `dominio` | RDAP (`rdap.org` para gTLD; `.mx` por NIC México, **verificar** si expone RDAP o hay que ir a WHOIS) + resolución DNS | Dominio del sitio o del correo del DENUE vencido, liberado o sin DNS | `expiration`, `last changed` |
| `sat_69` | Listas del art. 69 del CFF del SAT (descarga CSV), caché en `data/agente/sat/` con fecha de descarga | **Solo "No localizados"** es señal: el SAT no encontró al contribuyente en su domicilio. "Cancelados" son *créditos* cancelados, no negocios: neutral | Fecha de publicación en la lista |
| `mapillary` | Graph API v4 (`/images?bbox=…&fields=captured_at,thumb_1024_url,compass_angle`), token gratuito | Fotos de calle cerca del punto → fachada (§5) | `captured_at` |
| `kartaview` | API pública de KartaView, mismo uso | Segunda cobertura de calle | Fecha de captura |
| `buscador_searxng` | El SearXNG del compose, con motores **habilitados a propósito**: `bing`, `duckduckgo`, `brave`, `mojeek`, `startpage` (resultados de Google sin raspar Google), `qwant` | Ya existe; se guarda qué motores respondieron en cada consulta | Fecha del resultado |

Descartada: la API del DENUE de INEGI. Es el mismo directorio cuya ausencia
se está verificando; ya se trata como `directorio_denue` (neutral).

## 3. Fuentes de paga, leídas como página (Selenium/requests + LLM)

| Lo que cuesta | Sustituto | Lo que hace el LLM |
|---|---|---|
| Google Places API (`businessStatus`) | `maps` en Chrome, como hoy, **más**: abrir la pestaña de reseñas ordenada por "Más recientes" y anotar sus fechas relativas | Lee "Cerrado permanentemente/temporalmente", horarios y la reseña más reciente (`hace 3 meses [≈ 2026-08]`) |
| Google Custom Search | `buscador_google`: `google.com/search?hl=es&gl=mx` en Chrome; a la primera CAPTCHA se apaga (cortacircuitos) y queda Startpage vía SearXNG | Elige qué resultados abrir (ampliación) |
| Bing Search API (retirada) | `buscador_bing`: `bing.com/search` por `requests`, Chrome si bloquea | Igual |
| Brave Search API | `buscador_brave`: `search.brave.com` en Chrome. Si algún día hay `BRAVE_API_KEY`, la API sustituye a la página sin cambiar el recolector | Igual |
| DuckDuckGo (sin API) | `buscador_ddg`: `html.duckduckgo.com/html` por `requests` | Igual |
| Street View Static | `street_view`: Maps en Chrome en modo panorama sobre las coordenadas (`map_action=pano`), captura de pantalla + la fecha "Imagen: mes año" que muestra la página | Visión (§5) |
| Foursquare Places API | Fichas `foursquare.com/v/…` halladas por los buscadores. **Verificar** si la web sobrevivió al cierre de City Guide (2024); si no, se descarta | Lee el estado |
| Yelp Fusion / TripAdvisor Content API | `yelp`, `tripadvisor`: búsqueda `site:` en los buscadores y la ficha en Chrome | "Cerrado definitivamente", fecha de la última reseña |
| — (sin API pública) | `seccion_amarilla`, `delivery` (Rappi, Uber Eats, DiDi Food): `site:` + Chrome | ¿Sigue recibiendo pedidos? ¿mismo domicilio? |
| — | `psm` (Publicaciones de Sociedades Mercantiles, SE) y `dof`: formulario de búsqueda por razón social en Chrome | Avisos de disolución, liquidación y balance final, con fecha |
| Meta Content Library | `facebook`/`instagram` en Chrome sin sesión, como hoy. La solicitud académica va en paralelo (no es código) | Lee la última publicación fechada |

**Directorios que copian al DENUE** (`clasificar_fuente`): se revisa si
Sección Amarilla, Cylex, Infoisinfo y Hotfrog copian al INEGI. Los que sí,
entran a `directorio_denue` y quedan neutrales; los que tienen reseñas
fechadas propias, no.

## 4. Dónde se aprovecha más el LLM

1. **Identidad**: el código calcula distancia al punto del DENUE y similitud
   de nombre y la escribe en la pieza ("a 40 m · nombre 92/100"); el
   especialista decide si es el mismo negocio con eso a la vista, no a ojo.
2. **Qué abrir**: los buscadores traen decenas de resultados; el especialista
   elige cuáles leer (ampliación), en vez de abrir los N primeros.
3. **Variantes de nombre**: el especialista de buscadores puede pedir la
   consulta con el nombre coloquial ("Tacos El Güero" por "TAQUERIA EL GUERO
   SA DE CV"); queda marcada `ampliada`, así la base sigue fija.
4. **Lectura de fichas sin API**: el estado y las fechas de Maps, TripAdvisor,
   Yelp y delivery los extrae el modelo del texto, con cita validada.
5. **Visión** (§5): la única fuente que ve si el local lo ocupa otro negocio,
   que es exactamente el estrato `cerrado|reocupado`.
6. **Juez**: sin cambios; con más fuentes, su lectura a ciegas pesa más.

## 5. Visión: fachada

Cada imagen (Street View, Mapillary, KartaView, y la ficha de Maps como
respaldo cuando el texto no se extrae) pasa por un modelo de visión que
**describe** lo que ve: letrero, giro aparente, cortina abierta/cerrada,
"se renta". Esa descripción es la pieza que lee el especialista de fachada.

Dos reglas, porque esa pieza es texto generado y no de la fuente:

- **La fecha sale del metadato** (`captured_at`, "Imagen: mayo 2024"), no
  del modelo.
- **Una pieza de visión sola no da confianza alta.** El decisor puede usarla
  para `activo`/`cerrado`, pero con confianza `media` como tope si no hay otra
  evidencia. La captura queda en `imagen_ruta` para auditarla a mano.

Modelo — se decide midiendo en indominus (la 5090 tiene 32 GB y `qwen3:32b`
con 32k de contexto ya ocupa 28):

| Opción | Pro | Contra |
|---|---|---|
| `qwen3-vl:32b` como **único** modelo | Un solo modelo, sin cambios de carga | Hay que comprobar que su tool-calling iguale a `qwen3:32b` |
| `qwen2.5vl:7b` al lado de `qwen3:32b` con contexto a 16k | Caben los dos | Menos contexto para el decisor |
| VLM grande alternado | Mejor visión | ~10–15 s de recarga por cambio, dos por negocio |

## 6. Fases

Cada fase termina con: tests unitarios con HTML/JSON guardados como
fixtures (sin red), push, y una corrida de 10 negocios en indominus que el
usuario revisa antes de seguir.

| Fase | Qué | Depende de |
|---|---|---|
| **F0** | Enums + `b0012`, `Especialidad`, `AGENTE_FUENTES`, detector de bloqueo, cortacircuitos, pausas, perfil de Chrome, ventana de relevancia | — |
| **F1** | Buscadores: SearXNG con motores explícitos, `buscador_bing`, `buscador_ddg`, `buscador_google`, `buscador_brave`; dedupe de URLs entre motores | F0 |
| **F2** | Maps: reseñas más recientes; `osm` | F0 |
| **F3** | Huella digital: `wayback`, `common_crawl`, `dominio`; `sat_69` | F0 |
| **F4** | Plataformas: `tripadvisor`, `yelp`, `seccion_amarilla`, `delivery`; revisión de directorios que copian al DENUE | F1 (los encuentra por `site:`) |
| **F5** | Registros: `psm`, `dof` (primero un sondeo de viabilidad de cada formulario) | F0 |
| **F6** | Fachada: decisión de modelo, `street_view`, `mapillary`, `kartaview`, especialista de visión | F0, `MAPILLARY_TOKEN` |
| **F7** | Evaluación: v4 vs v5 sobre la misma muestra; aporte por recolector (veredictos firmes en los que fue citado); tiempo por negocio; bloqueos por motor | todo |

Orden sugerido: F0 → F1 → F2 → F3 en un bloque (todo gratis, mayor
impacto), corrida de 10; luego F4–F6.

## 7. Costo en tiempo

v4 son ~1 min por negocio. v5 con 7 especialistas, más recolección por
Chrome con pausas: estimado 3–4 min por negocio, **~1 día para 400**. El
paralelismo recolección/modelo (§1.4) debería bajarlo a ~2 min. Se mide en
la corrida de 10 de F3 antes de lanzar la muestra completa (`nohup`).

## 8. Riesgos

- **Términos de uso**: leer Google, Bing, Brave, Maps, Facebook, TripAdvisor
  y Yelp con un navegador automatizado está fuera de lo que sus términos
  permiten. Mitigación, como en v3: sin sesión, solo páginas públicas,
  volumen de muestra (cientos), pausas, y se documenta en la metodología.
- **Bloqueos**: Google es el más probable. El cortacircuitos lo apaga y la
  corrida sigue; `consultas` deja registrado qué motor faltó en qué negocio,
  así que el sesgo se puede medir.
- **Cambios de HTML**: los selectores se rompen. Por eso el LLM lee texto
  visible y no se parsean campos con selectores frágiles; el código solo
  navega.
- **Comparabilidad**: un motor apagado a media corrida hace que dos negocios
  no tengan las mismas fuentes. Se reporta en `metricas` (negocios con
  recolección incompleta) en vez de ocultarlo.

## 9. Por verificar antes de implementar

- RDAP para `.mx` (NIC México) o si hace falta WHOIS por puerto 43.
- URL vigente de las listas del art. 69 del SAT y su formato.
- Si `foursquare.com/v/…` sigue publicando fichas.
- Si PSM y DOF permiten buscar por razón social sin CAPTCHA.
- Si `qwen3-vl:32b` mantiene el tool-calling.
