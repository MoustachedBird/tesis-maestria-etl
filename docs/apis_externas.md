# APIs externas — acceso, costos y trámites

Fuentes que necesita el **agente 2** (verificador de supervivencia) de
`crear_agentes.md` §11. Aquí van los trámites, las páginas oficiales y los
costos.

> ⚠️ **Los precios y los procesos de estas plataformas cambian seguido.**
> Todo monto de aquí es **orden de magnitud al 2026-10-26** y sirve para
> dimensionar, no para presupuestar. Verificar siempre en la página oficial
> antes de comprometerse. Las **URLs** sí son estables; los números no.

---

## 1. Qué solicitar, y en qué orden

El criterio es el **tiempo de obtención**, no la utilidad: lo que tarda
semanas se solicita hoy, aunque se use al final.

| Fuente | Obtención | Costo | Prioridad |
|---|---|---|---|
| **Meta Content Library** | **semanas o meses** (revisión académica) | gratis si se aprueba | **Solicitar YA** — es el camino crítico |
| Google Maps Platform | minutos (tarjeta) | de pago | Alta: trae `business_status` |
| Brave Search API | minutos | capa gratuita + de pago | Alta |
| Serper.dev | minutos | muy barato | Alternativa a Brave |
| INEGI DENUE API | minutos (token) | **gratis** | Media |
| OpenStreetMap | sin trámite | gratis, con límite de tasa | Baja (cobertura pobre) |
| SIGER / RPC | consulta web | gratis | Solo sociedades constituidas |
| Meta Graph API (páginas ajenas) | App Review | gratis | **Descartada** (§4) |

**Lo único que bloquea** es la Content Library. Todo lo demás se obtiene el
mismo día, así que el diseño del agente 2 debe funcionar **sin** ella y
sumarla como fuente extra si llega la aprobación.

---

## 2. Google Maps Platform (Places API)

La fuente de mayor valor: devuelve `business_status` como campo estructurado
(`OPERATIONAL` / `CLOSED_TEMPORARILY` / `CLOSED_PERMANENTLY`), sin que un
modelo tenga que interpretar texto.

### Páginas

| | |
|---|---|
| Consola | <https://console.cloud.google.com/> |
| Docs de Places | <https://developers.google.com/maps/documentation/places/web-service/overview> |
| **Precios** | <https://developers.google.com/maps/billing-and-pricing/pricing> |
| Cuotas y límites | <https://developers.google.com/maps/documentation/places/web-service/usage-and-billing> |
| Términos | <https://cloud.google.com/maps-platform/terms> |

### Trámite

1. Cuenta de Google Cloud y un proyecto nuevo.
2. **Habilitar facturación** (tarjeta). No hay vía gratuita sin tarjeta.
3. Habilitar *Places API (New)* en el proyecto.
4. Crear credencial tipo **API key** y **restringirla**: por API (solo
   Places) y por IP si se corre desde indominus.
5. Verificar el modelo de cuotas gratuitas vigente — Google cambió en 2025
   del crédito mensual de USD 200 a topes gratuitos por SKU. **Confirmar en
   la página de precios cuál aplica hoy.**

### Costo

El precio depende del **SKU**, no de "una llamada": Places (New) separa
*Essentials*, *Pro* y *Enterprise* según los campos que pidas. `businessStatus`
no está en el nivel más barato, así que **lo primero es confirmar en qué SKU
cae** — eso define todo el presupuesto.

Orden de magnitud: **USD 5–35 por 1,000 solicitudes** según SKU.

| Volumen | Costo aprox. |
|---|---|
| 2,400 (muestra nacional de §10.4) | **USD 12–85** |
| 30,000 (censo de un municipio chico) | USD 150–1,050 |
| 10.45M (censo completo) | **USD 52k–180k** — descartado en §10.2 |

### Control de gasto — no opcional

Un bucle con un bug puede gastar miles de dólares en una noche.

1. **Presupuesto con alertas** en Cloud Billing (50%, 90%, 100%).
2. **Cuota diaria por API** en la consola: el tope duro que un presupuesto
   no da (las alertas avisan, no cortan).
3. `budget.py` de `src/shared/llm/` con su propio tope de llamadas por
   corrida, independiente del de Google.

Las tres capas son necesarias: la de Google no sabe cuándo nuestro código se
volvió loco, y la nuestra no sabe el precio real por SKU.

### Lo que hay que medir antes de confiar en la fuente

- **Cobertura**: qué fracción de los negocios del DENUE tiene ficha en Maps.
  Sospecho que cae mucho en micronegocios — es justo el sesgo de §11.5.
- **Latencia del cierre**: `business_status` dice cuándo *Google se enteró*,
  no cuándo cerró el negocio. Para fechar, la reseña más antigua da un
  límite inferior de existencia.

---

## 3. Meta Content Library — la única vía legítima a Facebook/Instagram

### Páginas

| | |
|---|---|
| Descripción | <https://transparency.meta.com/researchtools/meta-content-library> |
| **Solicitud (ICPSR)** | <https://www.icpsr.umich.edu/web/pages/ICPSR/mcl/> |
| Docs de la API | <https://developers.facebook.com/docs/content-library-api/> |

### Qué da

Contenido **público** de Páginas, Grupos, Eventos y cuentas públicas de
Instagram, consultable con una API de Python **dentro de un entorno de
investigación cerrado** — no se descargan los datos crudos a tu máquina.

Para el agente 2 eso sirve para: el último post de la página de un negocio,
publicaciones de cierre, y actividad que indique que sigue operando.

### Requisitos

- Afiliación a una institución académica o de investigación, con correo
  institucional.
- Propuesta de investigación con objetivos y uso de datos.
- Posible aval de comité de ética según la institución.
- Aceptar los términos de uso para investigadores.

### Tiempos y advertencias

**Semanas a meses.** Por eso va primero en la lista aunque se use al final.

Tres cosas que vale saber antes de apostarle:

- **CrowdTangle cerró en agosto de 2024.** Si encuentras tutoriales o
  papers que lo usan, están desactualizados — la Content Library es su
  reemplazo, con otro modelo de acceso.
- El entorno cerrado **limita cómo se integra con tu pipeline**: no es una
  API que llames desde el contenedor del ETL. Habrá que exportar resultados
  agregados o trabajar la muestra ahí dentro.
- La aprobación **no está garantizada** y no depende de ti. De ahí la regla
  de §1: el agente 2 debe funcionar sin esta fuente.

---

## 4. Meta Graph API (páginas de terceros) — descartada

| | |
|---|---|
| App Review | <https://developers.facebook.com/docs/apps/review/> |
| Page Public Content Access | <https://developers.facebook.com/docs/features-reference/page-public-content-access> |

Leer páginas que no administras exige el permiso *Page Public Content
Access*, que pasa por App Review más verificación de negocio. Para un caso
de uso de investigación la aprobación es muy improbable, y la Content
Library existe precisamente para cubrir ese hueco por la vía correcta.

**Lo que ya no existe:** la búsqueda pública de Facebook (retirada tras
2018), la Instagram Basic Display API (descontinuada en 2024) y CrowdTangle
(cerrado en 2024). La Instagram Graph API solo funciona con cuentas que tú
administras.

**Raspar no es alternativa:** viola los ToS, el bloqueo es agresivo, y en
una tesis la procedencia de los datos tiene que ser defendible.

---

## 5. Buscadores web

### Brave Search API — primera opción

| | |
|---|---|
| Página | <https://brave.com/search/api/> |
| Docs | <https://api-dashboard.search.brave.com/app/documentation> |

Índice propio, no intermedia a Google: **sin zona gris de ToS**, que es lo
que la hace la primera opción para una tesis. Llave inmediata al registrarse.

Capa gratuita con límite de tasa (del orden de 1 consulta/segundo y unos
miles al mes) y planes de pago en el orden de **USD 3–9 por 1,000**.

### Serper.dev — la más barata

| | |
|---|---|
| Página | <https://serper.dev/> |

Resultados de Google como servicio intermediario. Del orden de **USD 1 por
1,000**, con créditos gratis al registrarse. Llave inmediata.

Contra: intermedia a Google, así que la procedencia es menos limpia de
explicar que Brave.

### Otras

| Fuente | Página | Nota |
|---|---|---|
| SerpAPI | <https://serpapi.com/pricing> | Más caro (~USD 10–15/1000); consultar si tienen descuento académico |
| Tavily | <https://tavily.com/> | Pensada para agentes: devuelve contenido limpio, menos post-procesamiento |
| Exa | <https://exa.ai/> | Búsqueda neuronal, orientada a agentes |
| **DuckDuckGo** | — | **No tiene API oficial de resultados web.** El paquete `ddgs` raspa un endpoint HTML: se rompe seguido y limita por tasa. No apto para esto |
| Bing Web Search | <https://learn.microsoft.com/azure/ai-services/> | Microsoft movió/retiró estas APIs hacia Azure AI. **Verificar estado antes de considerarla** |

Para la muestra de 2,400 de §10.4: **USD 3–25** según proveedor. El costo
deja de ser criterio de diseño a esa escala.

---

## 6. Fuentes gratuitas

### INEGI — API del DENUE

| | |
|---|---|
| Página | <https://www.inegi.org.mx/servicios/api_denue.html> |

Token gratis al registrarse. Consulta por nombre, actividad y radio
geográfico **del corte vigente**.

Útil para: confirmar que un negocio sigue en el DENUE actual, y resolver
dudas puntuales sobre un registro.

**No sirve para la pregunta de fondo**: solo tiene el corte vigente, y la
ausencia en un corte posterior es justo la señal que queremos validar — no
puede validarse a sí misma.

### OpenStreetMap

| | |
|---|---|
| Nominatim (geocodificación) | <https://nominatim.org/release-docs/latest/api/Search/> |
| Política de uso | <https://operations.osmfoundation.org/policies/nominatim/> |
| Overpass (consultas masivas) | <https://wiki.openstreetmap.org/wiki/Overpass_API> |

Gratis, pero con **política de uso estricta**: Nominatim pide máximo 1
consulta/segundo y prohíbe usos masivos — para volumen hay que montar una
instancia propia.

Cobertura de micronegocios mexicanos: baja. Tiene la convención `disused:`
para locales cerrados, pero depende de que un voluntario lo haya marcado.

---

## 7. Fuentes oficiales mexicanas

| Fuente | Página | Qué da | Para quién sirve |
|---|---|---|---|
| Registro Público de Comercio / SIGER | portal de la Secretaría de Economía (<https://www.gob.mx/se>) | Constitución y disolución de sociedades | Solo las **constituidas** (SA de CV, S de RL) |
| SAT — consulta de RFC | <https://www.sat.gob.mx/> | Estatus del RFC | Una a la vez, con captcha. **Sin consulta masiva** |
| IMSS — datos abiertos | <http://datos.imss.gob.mx/> | Patrones y asegurados | **Agregados** por municipio y sector, no por negocio |
| Datos abiertos federales | <https://datos.gob.mx/> | Catálogos varios | Es de donde ya sale SESNSP |

**Esto es lo que sostiene el hallazgo de `crear_agentes.md` §11.5:** para los
negocios constituidos hay registros oficiales; para los micronegocios no hay
**ninguna** fuente. Y los micronegocios son el objeto de estudio.

---

## 8. Configuración en el proyecto

Las llaves van en `.env`, **nunca** en el código ni en un commit. Siguiendo
la convención del proyecto, en `.env.example` van vacías con su comentario:

```bash
# Google Maps Platform — business_status del negocio (docs/apis_externas.md §2)
# Restringir la llave por API y por IP en la consola de Google Cloud, y
# poner cuota diaria: las alertas de presupuesto avisan, no cortan.
GOOGLE_MAPS_API_KEY=
GOOGLE_MAPS_MAX_REQUESTS_PER_RUN=3000

# Buscador web. Brave es la primera opcion por no intermediar a Google
# (docs/apis_externas.md §5).
SEARCH_PROVIDER=brave
BRAVE_SEARCH_API_KEY=
SERPER_API_KEY=
SEARCH_MAX_REQUESTS_PER_RUN=3000

# INEGI DENUE — token gratuito, corte vigente unicamente
INEGI_DENUE_TOKEN=
```

Y `.gitignore` ya cubre `.env`. Verificarlo antes de pegar una llave real.

---

## 9. Presupuesto del diseño de §10.4

Muestra nacional de 2,400 más censo de un municipio chico (~30,000):

| Concepto | Volumen | Costo aprox. |
|---|---|---|
| Places — muestra nacional | 2,400 | USD 12–85 |
| Búsqueda web — muestra nacional | 2,400 | USD 3–25 |
| Places — censo municipal | 30,000 | USD 150–1,050 |
| Búsqueda web — censo municipal | 30,000 | USD 30–300 |
| Meta Content Library | — | gratis si se aprueba |
| INEGI DENUE | — | gratis |
| **Total** | | **USD 200–1,500** |

Contra los **USD 52k–180k** del censo completo de §10.2. Y el rango es tan
ancho porque depende del SKU de Places: **confirmar eso primero** es lo que
convierte este presupuesto en un número.

Recomendación: empezar por la **muestra nacional** (USD 15–110). Con eso se
responde la pregunta de `findings.md` §2 —qué fracción de las 922,285
desapariciones son cierres reales— y se mide la cobertura por estrato antes
de decidir si el censo municipal vale la pena.
