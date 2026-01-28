DOCUMENTACION TECNICA: LOGICA DEL PROCESO DE ARMONIZACION (MDM)
PROYECTO: DENUE BACKEND CORE - CAPA CURADA
VERSION DEL ALGORITMO: v4 (Memoria Acumulativa / Golden Record)
FECHA: Enero 2026

1. OBJETIVO DEL PROCESO
-----------------------
El objetivo del script 'harmonizer.py' es transformar la Capa Plata (Silver Layer / Processed) en una Capa Curada (Curated Layer). 
Su funcion principal es resolver la identidad unica de cada negocio a traves del tiempo (2015-2025+), superando problemas como:
- Cambios de ID oficial (CLEE) por parte del INEGI.
- Cambios leves en el nombre comercial o razon social.
- Errores de captura o datos faltantes en encuestas antiguas.
- Pequeñas variaciones en la ubicacion fisica (GPS) o mudanzas.

El resultado es un "Grafo de Identidad" donde cada negocio tiene un identificador unico universal (master_clee) que persiste en toda su historia.

2. ESTRATEGIA GENERAL: BACK-PROPAGATION CON MEMORIA
---------------------------------------------------
El algoritmo opera bajo la premisa de que los datos mas recientes son la "Verdad Base" (Anchor Truth).
- Se procesan los periodos en orden cronologico inverso: Del Futuro (ej. 2024) hacia el Pasado (ej. 2015).
- Se mantiene una "Base de Conocimiento" (Knowledge Base - KB) en memoria que acumula atributos de identidad.

Ejemplo del "Efecto Bola de Nieve":
Si en 2024 un negocio tiene Email pero no Telefono, y en 2023 tiene Telefono pero no Email, el sistema aprende ambos datos y puede usarlos para identificar al negocio en 2020.

3. PASO A PASO DEL ALGORITMO
----------------------------

FASE 1: PREPARACION Y LIMPIEZA
- Se escanean todos los archivos Parquet de la Capa Plata (data/processed/denue).
- Se normalizan datos criticos: 
  * Telefonos: Se eliminan guiones, espacios y parentesis.
  * Emails y Web: Minusculas y sin espacios.
  * Nombres: Se tratan los valores nulos.

FASE 2: INICIALIZACION (AÑO BASE)
- Se toma el año mas reciente.
- Se asignan los IDs iniciales:
  * Si tiene CLEE oficial, se usa ese.
  * Si no tiene, se genera un UUID temporal (NEW_...).
- Se crea la primera version de la Base de Conocimiento (KB), guardando listas de nombres, telefonos, emails y coordenadas de cada negocio identificado.

FASE 3: BUCLE DE RESOLUCION (VIAJE AL PASADO)
Para cada año anterior (Target), se compara contra la Base de Conocimiento acumulada.

Jerarquia de Decision (3 Niveles):

  NIVEL A: VERDAD ABSOLUTA (Oficial)
  - Si el registro del pasado ya tiene un CLEE oficial no nulo, se respeta ese ID.
  - Este ID se busca en la KB; si existe, se actualizan sus datos. Si es nuevo, se agrega a la KB.

  NIVEL B: HUELLA DIGITAL (Digital Fingerprint)
  - Si no hay CLEE, se busca coincidencia exacta en datos de contacto dentro de la KB.
  - Criterio: Coincidencia en Telefono, Email o Sitio Web.
  - Ventaja: Permite identificar negocios que se mudaron lejos (>60m) o cambiaron drasticamente de nombre, pero mantuvieron su contacto.

  NIVEL C: PROBABILISTICO (Espacial + Semantico)
  - Si no hay datos digitales, se usa un enfoque local estricto.
  - Filtro 1 (Espacial): El negocio debe estar a menos de 60 metros de alguna ubicacion conocida en la KB (BallTree Algorithm).
  - Filtro 2 (Giro): La actividad economica debe tener una similitud >60% (Token Set Ratio).
  - Filtro 3 (Identidad): El Nombre Comercial O la Razon Social deben tener una similitud >80% (Token Sort Ratio).

FASE 4: APRENDIZAJE (ACTUALIZACION DE KB)
- Una vez resueltas las identidades del año en curso, se actualiza la Base de Conocimiento.
- Se agregan los nombres antiguos, telefonos antiguos, etc., a las listas de cada entidad.
- Esto mejora la capacidad de match para los años siguientes (mas antiguos).

FASE 5: MATERIALIZACION (ESCRITURA)
- Se borra el directorio de destino (data/curated/denue) para asegurar limpieza.
- Se leen los archivos originales uno por uno.
- Se hace un JOIN con el mapa de identidades calculado.
- Se reemplaza la columna 'clee' original con el 'master_clee' unificado.
- Se guarda el archivo manteniendo la estructura de particiones (state=.../period=...).

4. PRODUCTOS FINALES
--------------------

A. DATASET CURADO (data/curated/denue/*)
   Archivos Parquet identicos a los procesados, pero con la columna 'clee' estandarizada. 
   Permite hacer un "Group By CLEE" directo para ver la historia del negocio.

B. CATALOGO MAESTRO (data/curated/denue/master_catalog.parquet)
   Un archivo rico ("Golden Profile") que contiene:
   - master_clee: ID Unico.
   - latitud/longitud: Ubicacion ancla (mas reciente).
   - names: Lista de todos los nombres comerciales que ha tenido.
   - tels: Lista de todos los telefonos historicos.
   - emails: Lista de todos los emails historicos.
   - razones: Lista de todas las razones sociales.

5. CONSIDERACIONES IMPORTANTES
------------------------------
- La "Herencia" de IDs es transitiva gracias a la memoria acumulativa.
- El proceso es destructivo en la carpeta de destino (Curated) pero de solo lectura en el origen (Processed).
- Los registros que nunca logran vincularse (ni por CLEE, ni huella digital, ni cercania) nacen como nuevos UUIDs (NEW_...) en ese año.