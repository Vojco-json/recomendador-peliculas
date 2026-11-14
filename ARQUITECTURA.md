# Plan de «recomendador-peliculas» (version 2)
Lo mantiene Delegado; no lo edites.

Vision: Sistema de recomendación de películas distribuido sobre MovieLens 25M para una expo con póster. Usa PySpark/MLlib para entrenar ALS batch con evaluación temporal, construir perfiles de gusto y agrupar a los usuarios con KMeans en «tipos de espectador». Kafka y Spark Structured Streaming procesan calificaciones en vivo, hay un experimento de speedup de 1 a 6 núcleos y una app Streamlit con chat LLM local (Ollama qwen3.5:9b, thinking desactivado por defecto) que solo explica con datos obtenidos por herramientas. Las métricas, los perfiles y el speedup los calcula el autor sobre los 25M completos. Para la demo en vivo, un «modo demo» carga ALS y perfiles precalculados sobre 25M y reentrena en vivo sobre un subconjunto configurable (por defecto, ratings desde 2015).
Stack: Python 3.12 en .venv creado con 'py -3.12 -m venv .venv' por setup_entorno.ps1, con requirements.txt de versiones fijadas (Lo exige el usuario. Fijar versiones evita sorpresas el día de la expo.); PySpark 3.5.x fijado (p. ej. 3.5.5) en local[N] con spark.driver.memory=6g, más el conector org.apache.spark:spark-sql-kafka-0-10_2.12 con la MISMA versión que pyspark.__version__, resuelto con spark.jars.packages y caché Ivy dentro del proyecto (spark.jars.ivy=.ivy2) (La guía oficial de integración Kafka de Spark 3.5 (spark.apache.org/docs/3.5.x/structured-streaming-kafka-integration.html) define el artefacto _2.12 con la versión de Spark. Con la caché Ivy local, después de la primera descarga funciona sin internet en la expo. La rama 3.5 evita los cambios de Scala 2.13 y winutils que trae Spark 4.); Trigger.availableNow + startingOffsets='earliest' para la prueba de integración real con Kafka (y para la opción --availableNow del streaming) (Desde Spark 3.3 la fuente Kafka soporta AvailableNow (SPARK-36649), y en 3.4 se agregaron validaciones del rango de offsets (SPARK-41387). Así la prueba procesa lo que hay en el topic y termina sola, sin quedar colgada, lo que respeta el límite de 2 minutos.); Cliente confluent-kafka (Producer y AdminClient) (Trae wheels oficiales para Windows y Python 3.12. AdminClient crea topics, incluido un topic de prueba único, sin tocar Docker.); SQLite (stdlib) en modo WAL como estado en vivo compartido (artefactos/estado.db) (No requiere instalación y permite que el proceso de streaming escriba mientras Streamlit lee, sin los problemas de leer un parquet a medio escribir.); Fold-in con numpy para actualizar recomendaciones al instante y reentrenamiento ALS periódico por lotes en un hilo aparte (Cumple «tiempo real + reentrenamiento periódico» sin reentrenar ALS en cada micro-lote. En modo demo, el reentrenamiento usa solo el subconjunto desde 2015 para que tarde minutos y no horas.); Paquete oficial 'ollama' con tool calling nativo y parámetro think=False por defecto (LLM_THINK configurable) (La documentación de Ollama expone 'think' por solicitud en chat() (docs.ollama.com/capabilities/thinking) y tool calling con funciones Python (docs.ollama.com/capabilities/tool-calling). Hay reportes de que algunos wrappers no propagan 'think' (github.com/tidyverse/ellmer/issues/940) y de que LiteLLM pierde los tool_calls de qwen3.5 (github.com/BerriAI/litellm/issues/24091), por eso se usa la API directa.); Streamlit, probado con streamlit.testing.v1.AppTest (Lo pide el usuario. AppTest permite probar la UI sin navegador, dentro de pytest.); matplotlib para figuras y diagrama (PNG a 300 dpi y SVG) (No hace falta instalar Graphviz ni nada externo, y las figuras se reproducen con un comando.); pytest con muestra mini fija y SparkSession compartida (scope session, local[2], con conector Kafka) (Una sola SparkSession por proceso evita pagar el arranque (10–15 s en Windows) en cada prueba y permite que el streaming Kafka use la misma sesión.); Sin Dask (El usuario lo decidió: queda como extensión futura.)
Arquitectura: recomendador-peliculas/
  setup_entorno.ps1, requirements.txt, check_entorno.py, README.md, .gitignore
  recomendador/
    config.py        rutas, FECHA_CORTE, NUCLEOS, SPARK_MEMORIA=6g, KAFKA_BOOTSTRAP, TOPIC, MODO (completo|demo), DEMO_DESDE=2015-01-01, ARTEFACTOS_BASE, LLM_PROVIDER, LLM_MODEL, OLLAMA_HOST, LLM_THINK=false (todo se sobrescribe con variables de entorno)
    spark_utils.py   crear_spark(nucleos, kafka=False)
    datos.py         cargar_movielens(ruta)
    kafka_utils.py   kafka_disponible(timeout=3), crear_topic(nombre)
    batch_als.py     partición temporal, ALS, popularidad, métricas
    foldin.py        recomendar_foldin(...)
    perfiles.py      vectores de gusto, KMeans, nombrado, perfil_usuario()
    estado.py        SQLite WAL: eventos, perfiles, recomendaciones, meta
    demo.py          preparar_demo(): base precalculada de 25M + subconjunto >= DEMO_DESDE
    streaming.py     readStream kafka|archivo -> foreachBatch procesar_lote() -> estado.db; hilo de reentrenamiento
    enriquecimiento.py  IMDb, TMDb con caché, Letterboxd
    herramientas.py  funciones para el LLM (leen artefactos y estado.db)
    llm.py           Ollama (tools, think configurable) y proveedor 'reglas'
  scripts/  descargar_movielens.py, ordenar_ratings.py, crear_topic.py, productor_kafka.py, entrenar_batch.py, perfilar_usuarios.py, preparar_demo.py, streaming.py, escalabilidad.py, figuras_poster.py, descargar_imdb.py, importar_letterboxd.py
  app/app.py
  muestras/ml-mini/ (formato de ml-25m, con LEEME.md de valores conocidos), muestras/imdb-mini/, muestras/letterboxd_ratings.csv
  tests/ conftest.py y test_<paquete>.py
  data/ (ml-25m, imdb; ignorado)  artefactos/<corrida>/ (25m, mini, demo)  poster/figuras/
Flujo completo (lo corre el usuario): descargar -> ordenar -> entrenar_batch (25M) -> perfilar (25M) -> escalabilidad -> figuras.
Flujo demo: preparar_demo (toma artefactos/25m y crea artefactos/demo con el subconjunto desde DEMO_DESDE) -> crear_topic -> streaming --modo demo -> productor --desde FECHA_CORTE -> app (lee artefactos/demo y estado.db).
Todos los scripts aceptan --datos/--salida/--artefactos para correr sobre ml-mini en las pruebas.
Diseno: Identidad común para el póster y la app: fondo oscuro de cine (#141414), acento rojo (#E50914) para la acción principal, ámbar (#F5C518) para métricas y estrellas, texto #F2F2F2/#B3B3B3 y paleta tab10 para los clusters (apta para daltónicos). Figuras en DejaVu Sans de 14 pt o más, con títulos en español. En la app, la barra lateral muestra el userId, un badge con el tipo de espectador, las barras de géneros del perfil, un indicador «Modo demo · reentrenado hace X min» y la configuración del LLM. La columna principal tiene tarjetas de película (póster TMDb si hay clave, título, año, duración y motivo de una línea) y el chat. Tono cercano en español; el LLM siempre cita datos concretos («calificaste Heat (1995) con 5★»).
Supuestos: La carpeta solo tiene ESPEC.md y ARQUITECTURA.md (plan v1): no hay código verificado, así que el plan v2 parte de cero y conserva la estructura del v1.; Las métricas, perfiles y speedup de 25M los corre el usuario. Las pruebas automáticas usan ml-mini.; En el modo demo, el productor reproduce ratings desde la fecha de corte (2018-01-01) en adelante y acelerados. El reentrenamiento en vivo usa ratings desde 2015-01-01 anteriores al corte, más los eventos recibidos.; Las pruebas usan un topic único 'ratings_test_<uuid>' (que se borra al final) para no contaminar el topic 'ratings' de la demo. Se asume que el broker permite crear y borrar topics.; El conector Kafka de Spark se descarga de Maven la primera vez (requiere internet) y queda en .ivy2 del proyecto para usarse sin conexión.; Fecha de corte de 25M: 2018-01-01. Relevante = rating >= 4. 'Corto' = 100 min o menos según IMDb.; Los ítems que no están en el subconjunto demo después de reentrenar no se recomiendan hasta el siguiente reentrenamiento completo.

## Paquetes de trabajo
### 1. 1. Entorno, configuración, check_entorno y muestra mini [verificado]
Objetivo: Dejar el .venv, la configuración central (incluido el modo demo y LLM_THINK), la fábrica de SparkSession con conector Kafka cacheado, la detección de Kafka, check_entorno.py y la muestra mini con valores conocidos.
Requisitos: 1, 2, 11, 12, 13, 14, 23, 25 · Archivos: setup_entorno.ps1, requirements.txt, check_entorno.py, .gitignore, recomendador/__init__.py, recomendador/config.py, recomendador/spark_utils.py, recomendador/datos.py, recomendador/kafka_utils.py, muestras/ml-mini/movies.csv, muestras/ml-mini/ratings.csv, muestras/ml-mini/tags.csv, muestras/ml-mini/links.csv, muestras/ml-mini/genome-tags.csv, muestras/ml-mini/genome-scores.csv, muestras/ml-mini/LEEME.md, tests/conftest.py, tests/test_entorno.py · Depende de: -
- Criterio: setup_entorno.ps1 ejecuta 'py -3.12 -m venv .venv' e instala requirements.txt con versiones fijadas (pyspark==3.5.x, confluent-kafka, pandas, pyarrow, numpy, matplotlib, scikit-learn, streamlit, ollam
- Criterio: check_entorno.py reporta [OK]/[FALTA] con una sugerencia para: Python 3.12, Java 17 (java -version y JAVA_HOME), HADOOP_HOME, winutils.exe y hadoop.dll, pyspark importable, conector Kafka en caché, Ka
- Criterio: config.py incluye MODO ('completo'|'demo'), DEMO_DESDE='2015-01-01', ARTEFACTOS_BASE (artefactos/25m), LLM_THINK=False, LLM_MODEL='qwen3.5:9b', SPARK_MEMORIA='6g', NUCLEOS=6, KAFKA_BOOTSTRAP='localhos
- Criterio: spark_utils.crear_spark(nucleos, kafka=False) usa local[nucleos], driver.memory 6g, shuffle.partitions proporcional y spark.jars.ivy=<proyecto>/.ivy2. Con kafka=True agrega spark-sql-kafka-0-10_2.12 c
- Criterio: kafka_utils.kafka_disponible(timeout=3) devuelve bool sin lanzar excepción. crear_topic(nombre, particiones) es idempotente.
- Criterio: ml-mini tiene el formato exacto de ml-25m: unos 15 usuarios, unas 25 películas reales con movieId/imdbId/tmdbId reales y unos 300 ratings entre 2010 y 2019. LEEME.md documenta los conteos totales, los
- Criterio: conftest.py ofrece la fixture spark (session, local[2], kafka=True), ruta_mini y kafka_ok. Las pruebas de Kafka hacen pytest.skip('Kafka no disponible') solo si kafka_disponible() es False.
- Prueba de aceptacion: check_entorno corre sin traceback y reporta cada chequeo: el comando «.venv/Scripts/python check_entorno.py» debe terminar sin error
- Prueba de aceptacion: La muestra se carga con los conteos documentados, Toy Story es la más popular antes del corte, la config lee variables de entorno (MODO, DEMO_DESDE, LLM_THINK) y kafka_disponible no lanza excepciones: el comando «.venv/Scripts/python -m pytest -q -rs tests/test_entorno.py» debe terminar sin error
### 2. 2. Ingesta: descarga, topic 'ratings' y productor Kafka [verificado]
Objetivo: Descargar ml-25m con verificación md5, ordenar los ratings por timestamp y reproducirlos en un topic Kafka a velocidad configurable, con una función de publicación reutilizable por el streaming y las pruebas.
Requisitos: 3, 4, 13 · Archivos: scripts/descargar_movielens.py, scripts/ordenar_ratings.py, scripts/crear_topic.py, scripts/productor_kafka.py, tests/test_ingesta.py · Depende de: 1
- Criterio: descargar_movielens.py baja https://files.grouplens.org/datasets/movielens/ml-25m.zip a data/, verifica el .md5, descomprime en data/ml-25m y no repite la descarga si ya existe. Tiene --dry-run.
- Criterio: ordenar_ratings.py --datos --salida escribe ratings_ordenados.parquet ordenado por timestamp con Spark.
- Criterio: crear_topic.py [--topic ratings] crea el topic (6 particiones, RF 1) si no existe y avisa si ya existía.
- Criterio: productor_kafka.py acepta --datos, --topic, --velocidad (ratings/seg; 0 = sin pausa), --acelerar X, --desde y --hasta AAAA-MM-DD, --limite N y --dry-run. Envía JSON {userId, movieId, rating, timestamp
- Criterio: Expone publicar(eventos, topic, bootstrap) -> int, que reutiliza la prueba de integración del paquete 5.
- Criterio: Si Kafka no responde, termina con un mensaje claro y código distinto de 0, sin traceback.
- Prueba de aceptacion: Dry-run sobre la muestra emite 5 eventos ordenados por timestamp: el comando «.venv/Scripts/python scripts/productor_kafka.py --datos muestras/ml-mini --dry-run --limite 5» debe terminar sin error
- Prueba de aceptacion: Pruebas de orden temporal, formato JSON y filtro --desde; con broker disponible, ida y vuelta real de 5 mensajes por un topic de prueba único: el comando «.venv/Scripts/python -m pytest -q -rs tests/test_ingesta.py» debe terminar sin error
### 3. 3. Recomendador batch ALS con partición temporal y métricas [verificado]
Objetivo: Entrenar ALS con los datos anteriores al corte y evaluar con los posteriores (RMSE, Precision@10, NDCG@10) frente a la popularidad, y ofrecer fold-in para usuarios nuevos o activos.
Requisitos: 5, 6, 13 · Archivos: recomendador/batch_als.py, recomendador/foldin.py, scripts/entrenar_batch.py, tests/test_batch.py · Depende de: 1
- Criterio: entrenar_batch.py --datos --corte --salida [--nucleos] [--fraccion] [--desde]. Por defecto usa ml-25m con corte 2018-01-01. --desde filtra los ratings anteriores a esa fecha, lo que usa el modo demo.
- Criterio: Test = ratings posteriores al corte de usuarios e ítems vistos en el entrenamiento (coldStartStrategy='drop'). Informa cuántos se descartan.
- Criterio: ALS explícito con rank, regParam y maxIter configurables y seed fija. Relevante = rating >= 4. P@10 y NDCG@10 sobre el top-10 sin los ítems ya vistos.
- Criterio: La popularidad es el top por número de ratings en el entrenamiento (con un mínimo de votos). Su RMSE usa la media por ítem.
- Criterio: Guarda metricas.json ({als, popularidad, corte, n_train, n_test, segundos}), modelo_als/, item_factors.parquet, user_factors.parquet y recs_top20.parquet.
- Criterio: La función entrenar(df_train, params) es reutilizable por el reentrenamiento del streaming.
- Criterio: foldin.recomendar_foldin(item_factors, [(movieId, rating)], n, excluir, reg) devuelve un top-n sin películas vistas.
- Criterio: Imprime una tabla con 'RMSE', 'Precision@10' y 'NDCG@10' para ALS y Popularidad, sin NaN.
- Prueba de aceptacion: El entrenamiento sobre la muestra imprime la tabla de métricas sin NaN: el comando «.venv/Scripts/python scripts/entrenar_batch.py --datos muestras/ml-mini --corte 2016-01-01 --salida artefactos/mini --nucleos 2» debe terminar sin error
- Prueba de aceptacion: Partición temporal correcta, Toy Story primera en popularidad, métricas en [0,1], fold-in sin películas vistas y --desde reduce el entrenamiento: el comando «.venv/Scripts/python -m pytest -q tests/test_batch.py» debe terminar sin error
### 4. 4. Perfiles de gusto y tipos de espectador (KMeans) [verificado]
Objetivo: Construir vectores de gusto por usuario (géneros y tag genome ponderados por sus calificaciones) y agruparlos con KMeans en tipos de espectador con nombres interpretables.
Requisitos: 7, 8, 13 · Archivos: recomendador/perfiles.py, scripts/perfilar_usuarios.py, tests/test_perfiles.py · Depende de: 1
- Criterio: Peso = rating - media del usuario. El vector de géneros es la suma ponderada normalizada (20 géneros). El vector genome usa unos 40 tags interpretables, ponderados por relevancia.
- Criterio: KMeans de MLlib (k configurable: 8 en 25M, 3 en mini; seed fija) sobre features estandarizadas más log(n_ratings).
- Criterio: Nombrado automático en español a partir de los 2 rasgos con mayor desviación positiva del centroide, con un diccionario de traducción. Los nombres no se repiten.
- Criterio: Salidas: perfiles.parquet (userId, top_generos, top_tags, cluster, tipo) y tipos.json (nombre, tamaño, rasgos y centroide).
- Criterio: perfil_usuario(userId, artefactos) devuelve un dict legible. actualizar_perfil_generos(perfil, nuevos_ratings) permite la actualización incremental desde el streaming.
- Prueba de aceptacion: El perfilado sobre la muestra lista los tipos de espectador con nombre y tamaño: el comando «.venv/Scripts/python scripts/perfilar_usuarios.py --datos muestras/ml-mini --salida artefactos/mini --k 3» debe terminar sin error
- Prueba de aceptacion: El usuario 1 tiene Action entre sus 2 géneros principales y el usuario 2 tiene Romance o Drama. Quedan en clusters distintos, los nombres son únicos y la actualización incremental sube el peso del género calificado: el comando «.venv/Scripts/python -m pytest -q tests/test_perfiles.py» debe terminar sin error
### 5. 5. Streaming real Kafka → Structured Streaming → SQLite, reentrenamiento periódico y modo demo [verificado]
Objetivo: Leer Kafka con Structured Streaming, actualizar en SQLite los perfiles y las recomendaciones (fold-in) de los usuarios activos, reentrenar ALS periódicamente y preparar el modo demo (artefactos de 25M + subconjunto en vivo desde DEMO_DESDE).
Requisitos: 9, 13, 22, 23, 25 · Archivos: recomendador/estado.py, recomendador/streaming.py, recomendador/demo.py, scripts/streaming.py, scripts/preparar_demo.py, tests/test_streaming.py, tests/test_demo.py · Depende de: 2, 3, 4
- Criterio: estado.py: SQLite en modo WAL con tablas eventos, perfiles, recomendaciones (userId, movieId, rank, score, actualizado_en) y meta (ultimo_reentrenamiento, version_modelo, modo).
- Criterio: scripts/streaming.py --fuente kafka|archivo --topic --artefactos --estado [--modo completo|demo] [--trigger 5] [--reentrenar-cada-min 30] [--availableNow] [--starting-offsets latest|earliest].
- Criterio: foreachBatch llama a procesar_lote(df, batch_id, ctx), que guarda eventos sin duplicar (clave userId, movieId, timestamp), actualiza el perfil de géneros, recalcula el top-10 por fold-in sin películas
- Criterio: Checkpoint en <artefactos>/checkpoints/<topic>. Al reiniciar no duplica lotes.
- Criterio: El reentrenamiento corre en un hilo aparte: base + eventos nuevos -> batch_als.entrenar -> item_factors en una carpeta versionada nueva, con un swap atómico del puntero en meta. En modo demo la base e
- Criterio: preparar_demo.py --base <artefactos 25M> --datos <ml> --desde AAAA-MM-DD --salida <dir>: copia o enlaza modelo, factores, perfiles y tipos, escribe base_demo.parquet con los ratings >= desde y anterio
- Criterio: Prueba de integración REAL (test_streaming.py): crea un topic único 'ratings_test_<uuid>', publica con productor_kafka.publicar los ratings de la muestra posteriores al corte, corre el streaming con s
- Criterio: También hay una prueba con fuente archivo (sin Kafka) para procesar_lote.
- Prueba de aceptacion: Integración real productor → Kafka → Structured Streaming → SQLite (se salta solo si no hay broker) y prueba con fuente archivo: el comando «.venv/Scripts/python -m pytest -q -rs tests/test_streaming.py» debe terminar sin error
- Prueba de aceptacion: preparar_demo sobre la muestra crea el subconjunto desde 2015 con el conteo documentado en LEEME.md, y el reentrenamiento demo cambia version_modelo en meta: el comando «.venv/Scripts/python -m pytest -q tests/test_demo.py» debe terminar sin error
### 6. 6. Experimento de escalabilidad y figuras del póster [verificado]
Objetivo: Medir tiempos de ALS con 1, 2, 4 y 6 núcleos, calcular el speedup y generar todas las figuras del póster.
Requisitos: 10, 20 · Archivos: scripts/escalabilidad.py, scripts/figuras_poster.py, poster/figuras/, tests/test_poster.py · Depende de: 3, 4
- Criterio: escalabilidad.py --datos --nucleos 1,2,4,6 --repeticiones R --fraccion f --salida: usa un subproceso por N con su propia SparkSession (6g), descarta una corrida de calentamiento y guarda escalabilidad
- Criterio: Gráfico speedup medido vs ideal (línea punteada) y tiempos en poster/figuras/speedup.png (300 dpi) y .svg, anotando dataset, fracción y Ryzen 5 3600.
- Criterio: figuras_poster.py --artefactos genera metricas.png/.svg, tipos_espectador.png/.svg (PCA 2D con nombres y tamaños) y arquitectura.png/.svg (con Kafka, Spark batch y streaming, SQLite, Streamlit, Ollama
- Criterio: Paleta y tipografía del diseño, fuentes de 14 pt o más y títulos en español. Si falta un artefacto, avisa y genera las demás figuras.
- Prueba de aceptacion: La escalabilidad rápida sobre la muestra con 1 y 2 núcleos imprime los speedups: el comando «.venv/Scripts/python scripts/escalabilidad.py --datos muestras/ml-mini --nucleos 1,2 --repeticiones 1 --salida artefactos/mini» debe terminar sin error
- Prueba de aceptacion: Se generan las figuras del póster como PNG y SVG no vacíos: el comando «.venv/Scripts/python -m pytest -q tests/test_poster.py» debe terminar sin error
### 7. 7. Enriquecimiento legal: IMDb, TMDb y Letterboxd [verificado]
Objetivo: Unir IMDb no comercial por links.csv, agregar pósters y sinopsis de TMDb con caché, e importar CSV de Letterboxd como usuario nuevo con fold-in.
Requisitos: 15, 16, 17 · Archivos: recomendador/enriquecimiento.py, scripts/descargar_imdb.py, scripts/importar_letterboxd.py, muestras/imdb-mini/title.basics.tsv, muestras/imdb-mini/title.ratings.tsv, muestras/letterboxd_ratings.csv, tests/test_enriquecimiento.py · Depende de: 1, 3
- Criterio: descargar_imdb.py descarga title.basics.tsv.gz y title.ratings.tsv.gz de https://datasets.imdbws.com/ a data/imdb (no comercial, sin scraping).
- Criterio: Cruce de imdbId a 'tt' + 7 dígitos con tconst. Genera peliculas_enriquecidas.parquet (movieId, titulo, anio, generos, duracion_min, imdb_rating, imdb_votos, tmdbId). Con muestras/imdb-mini, Toy Story 
- Criterio: Cliente TMDb: lee TMDB_API_KEY del entorno y llama GET /3/movie/{tmdbId} con language=es-ES y respaldo en en-US. Guarda la caché en artefactos/tmdb_cache.json. Sin clave devuelve None, sin errores ni 
- Criterio: importar_letterboxd.py valida las columnas Date, Name, Year, Letterboxd URI y Rating. Empareja por título normalizado y año (±1), imprime 'Emparejadas: X de Y', crea un userId >= 1000000, guarda sus r
- Criterio: muestras/letterboxd_ratings.csv contiene 4 películas de ml-mini y 1 inexistente. tests/test_enriquecimiento.py ejecuta el importador sobre un directorio temporal y comprueba 'Emparejadas: 4 de 5', use
- Criterio: No modifica módulos verificados, salvo para importar sus funciones. No crea archivos .py en la raíz del proyecto.
- Prueba de aceptacion: Cruce IMDb (Toy Story, 81 min), TMDb sin clave degrada sin error, normalización de títulos e importación de Letterboxd con 'Emparejadas: 4 de 5' en un directorio temporal: el comando «.venv/Scripts/python -m pytest -q -rs tests/test_enriquecimiento.py» debe terminar sin error
- Prueba de aceptacion: El importador funciona de punta a punta sobre la muestra creando sus artefactos si faltan: el comando «.venv/Scripts/python scripts/importar_letterboxd.py --csv muestras/letterboxd_ratings.csv --datos muestras/ml-mini --artefactos artefactos/mini_letterboxd» debe terminar sin error
### 8. 8. App Streamlit con chat LLM por herramientas y README de la demo [verificado]
Objetivo: Herramientas de datos, LLM configurable (Ollama qwen3.5:9b con think=False por defecto y proveedor 'reglas' de respaldo) y app Streamlit con userId, tipo de espectador, tarjetas y chat que solo usa datos de herramientas, leyendo el estado en vivo de SQLite. El README queda para el paquete 9.
Requisitos: 18, 19, 20, 21, 24 · Archivos: recomendador/herramientas.py, recomendador/llm.py, app/app.py, README.md, tests/test_app.py · Depende de: 3, 4, 5, 7
- Criterio: herramientas.py ofrece peliculas_calificadas, tags_usuario, perfil_gustos, recomendar(userId, n, max_duracion_min, generos, excluir_vistas=True) e info_pelicula. Prioriza estado.db (en vivo) sobre los
- Criterio: llm.py: el proveedor 'ollama' llama a ollama.chat(model, messages, tools, think=config.LLM_THINK), con LLM_THINK=False por defecto y LLM_THINK=true por entorno. El bucle de herramientas es limitado. L
- Criterio: El proveedor 'reglas' es determinista: interpreta 'corto' (≤ 100 min) y 'liviano' (Comedy/Animation/Family). Se activa con el aviso visible 'Ollama no responde: usando modo reglas' cuando Ollama falla
- Criterio: app/app.py: barra lateral con userId, badge del tipo, barras de géneros, modo (completo/demo), 'Actualizado hace X s' leído de estado.db, y LLM_PROVIDER/LLM_MODEL/think. Columna principal con tarjetas
- Criterio: Las pruebas construyen sus artefactos mini en un directorio temporal (reutilizando tests/base_mini.py) y no dependen de artefactos de 25M.
- Prueba de aceptacion: AppTest con el proveedor 'reglas' sobre la muestra: el usuario 1 ve su tipo de espectador y, al pedir 'algo corto y liviano para hoy', recibe recomendaciones explicadas sin películas vistas. Con un cliente ollama simulado se verifica think=False por defecto y que se descartan las películas inventadas.: el comando «.venv/Scripts/python -m pytest -q tests/test_app.py» debe terminar sin error
- Prueba de aceptacion: Las herramientas devuelven datos reales del usuario 1 de ml-mini (Action/Thriller) y un userId inexistente no rompe: el comando «.venv/Scripts/python -m pytest -q tests/test_herramientas.py» debe terminar sin error
### 9. 9. README de la demo, aviso en figuras y cierre de integración [verificado]
Objetivo: Entregar el README completo (instalación, corrida de 25M, guía de la demo de 5 minutos, licencias y solución de problemas en Windows), hacer que la figura de métricas avise cuando no hay usuarios evaluados y comprobar que todas las suites siguen pasando juntas.
Requisitos: 20, 21 · Archivos: README.md, recomendador/poster.py, tests/test_readme.py, tests/test_poster.py · Depende de: -
- Criterio: README.md contiene estas secciones: Instalación (setup_entorno, cuál script usar y el .venv con py -3.12), check_entorno.py, Corrida completa de 25M paso a paso (descargar → ordenar → crear_topic → en
- Criterio: La figura de métricas muestra el texto 'Sin usuarios evaluados: no usar en el póster' cuando la evaluación tiene 0 usuarios. Se prueba en tests/test_poster.py.
- Criterio: tests/test_readme.py verifica que existan los títulos de sección y que cada script citado en el README exista en el repositorio.
- Criterio: No cambia el comportamiento de los paquetes verificados más allá del aviso de la figura.
- Prueba de aceptacion: El README tiene todas las secciones y solo cita scripts que existen: el comando «.venv/Scripts/python -m pytest -q tests/test_readme.py» debe terminar sin error
- Prueba de aceptacion: Las figuras siguen generándose y la de métricas avisa cuando hay 0 usuarios evaluados: el comando «.venv/Scripts/python -m pytest -q tests/test_poster.py» debe terminar sin error

## Riesgos
- Windows y Spark: si faltan winutils.exe o hadoop.dll, fallan los checkpoints del streaming y la escritura de parquet. check_entorno lo detecta.
- La prueba de integración Kafka puede acercarse a los 2 minutos por el arranque de Spark y la resolución del conector. Mitigación: SparkSession compartida, caché .ivy2, availableNow y timeout de 90 s.
- El tool calling de qwen3.5:9b con think=False puede ser irregular (hay reportes con qwen3 sin thinking). Mitigación: validación posterior, bucle limitado, proveedor 'reglas' y LLM_THINK configurable.
- En el modo demo, reentrenar sobre ratings desde 2015 (unos varios millones) con 6 GB puede tardar varios minutos. Mitigación: hilo aparte, swap atómico y DEMO_DESDE configurable.
- Concurrencia en SQLite entre el streaming y Streamlit. Mitigación: modo WAL y transacciones cortas.
- genome-scores (15M filas) con 6 GB puede generar spill a disco. Mitigación: subconjunto de unos 40 tags y parquet intermedio.

## Limitaciones
- Las pruebas automáticas validan la lógica sobre ml-mini. Los números del póster dependen de la corrida de 25M que hace el usuario.
- El speedup se mide en una sola máquina (local[N], 6 núcleos físicos, 6 GB), no en un clúster. En ml-mini no es representativo.
- La calidad de las explicaciones depende de qwen3.5:9b. La validación evita películas inventadas, pero no garantiza la redacción.
- IMDb es solo para uso personal y no comercial, y TMDb exige atribución: hay que citarlos en el póster.
- Dask queda fuera del alcance, por decisión del usuario.