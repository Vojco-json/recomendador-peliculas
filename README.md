# Recomendador de películas distribuido (MovieLens 25M)

Sistema de recomendación para una expo con póster: **PySpark/MLlib** entrena ALS con
partición temporal y agrupa a los usuarios en «tipos de espectador» (KMeans);
**Kafka + Spark Structured Streaming** procesan calificaciones en vivo; una app
**Streamlit** con chat LLM local (Ollama `qwen3.5:9b`) explica las recomendaciones solo
con datos obtenidos por herramientas.

Índice: [Instalación](#instalación) · [check_entorno.py](#check_entornopy) ·
[Corrida completa de 25M paso a paso](#corrida-completa-de-25m-paso-a-paso) ·
[Guía de la demo en vivo (5 minutos)](#guía-de-la-demo-en-vivo-5-minutos) ·
[App Streamlit con chat](#app-streamlit-con-chat) · [Pruebas](#pruebas) ·
[Licencias](#licencias) · [Solución de problemas en Windows](#solución-de-problemas-en-windows)

Todos los comandos se ejecutan **desde la carpeta del proyecto** en PowerShell.

---

## Instalación

Requisitos previos (los instala el usuario): Windows 11, **Python 3.12** (lanzador
`py`), **Java 17** con `JAVA_HOME`, **winutils** de Hadoop 3.3 con `HADOOP_HOME` y
**Kafka** corriendo en Docker en `localhost:9092`. Para el chat, **Ollama** con
`ollama pull qwen3.5:9b` (opcional: sin Ollama la app usa el modo `reglas`).

El entorno virtual se crea siempre con `py -3.12 -m venv .venv` e instala
`requirements.txt` (versiones fijadas, PySpark 3.5.x). Hay dos instaladores
equivalentes; **usa uno solo**:

| Script | Cuándo usarlo | Comando |
|---|---|---|
| `setup_entorno.ps1` | Opción normal, desde una consola PowerShell | `powershell -ExecutionPolicy Bypass -File .\setup_entorno.ps1` |
| `setup_entorno.py` | Si tu lanzador no permite abrir PowerShell anidado o la política de ejecución bloquea `.ps1` | `py -3.12 setup_entorno.py` |

Ambos: (1) crean `.venv` con `py -3.12`, (2) instalan `requirements.txt`, (3) verifican
que PySpark sea 3.5.x, (4) descargan el conector Kafka de Spark a la caché `.ivy2` del
proyecto (después funciona sin internet) y (5) ejecutan `check_entorno.py`. El registro
queda en `artefactos/instalacion.log`.

Opciones: `-Recrear` / `--recrear` borra y recrea `.venv`; `-SinConector` /
`--sin-conector` omite la descarga del conector.

## check_entorno.py

```powershell
.venv\Scripts\python check_entorno.py
.venv\Scripts\python check_entorno.py --descargar-conector   # baja el conector Kafka a .ivy2
.venv\Scripts\python check_entorno.py --estricto             # código de salida 1 si falta algo
```

Revisa Python 3.12, Java 17 y `JAVA_HOME`, `HADOOP_HOME` con `winutils.exe` y
`hadoop.dll`, que PySpark se importe, el conector Kafka en `.ivy2` y que el broker
responda en `KAFKA_BOOTSTRAP`. Para cada cosa que falte dice **qué hacer**.

La configuración se cambia con variables de entorno (`$env:NOMBRE = "valor"`); las
principales son `MODO` (`completo`/`demo`), `DEMO_DESDE` (2015-01-01), `FECHA_CORTE`
(2018-01-01), `NUCLEOS` (6), `SPARK_MEMORIA` (6g, máximo 6g), `KAFKA_BOOTSTRAP`,
`TOPIC` y `DATOS`. La lista completa está en `recomendador/config.py`.

## Corrida completa de 25M paso a paso

Las métricas, perfiles y el speedup del póster se calculan sobre los 25M completos.
Tiempos orientativos en un Ryzen 5 3600 con 6 GB para Spark.

1. **Descargar MovieLens 25M** (≈250 MB, queda en `data/ml-25m`):
   ```powershell
   .venv\Scripts\python scripts/descargar_movielens.py
   ```
2. **Ordenar los ratings por timestamp** (escribe `data/ml-25m/ratings_ordenados.parquet`,
   que usa el productor):
   ```powershell
   .venv\Scripts\python scripts/ordenar_ratings.py
   ```
3. **Crear el topic `ratings`** (6 particiones por defecto):
   ```powershell
   .venv\Scripts\python scripts/crear_topic.py
   ```
4. **Entrenar ALS batch y evaluar** (partición temporal: entrena antes de
   `FECHA_CORTE`, evalúa después; RMSE, Precision@10 y NDCG@10 contra popularidad).
   Escribe el modelo y `metricas.json` en `artefactos/25m`:
   ```powershell
   .venv\Scripts\python scripts/entrenar_batch.py
   ```
   Revisa que `metricas.json` tenga `usuarios_evaluados` mayor que 0: si es 0, la
   figura de métricas sale marcada con «Sin usuarios evaluados: no usar en el póster».
5. **Perfiles de gusto y tipos de espectador** (géneros + tag genome, KMeans):
   ```powershell
   .venv\Scripts\python scripts/perfilar_usuarios.py
   ```
6. **Experimento de escalabilidad** (1, 2, 4 y 6 núcleos, 3 repeticiones; es lo más
   largo, se puede reducir con `--fraccion 0.5` o `--repeticiones 1`):
   ```powershell
   .venv\Scripts\python scripts/escalabilidad.py
   ```
7. **Figuras del póster** (PNG a 300 dpi y SVG en `poster/figuras`: arquitectura,
   métricas, speedup y tipos de espectador):
   ```powershell
   .venv\Scripts\python scripts/figuras_poster.py
   ```
8. **Enriquecimiento opcional y legal**:
   ```powershell
   .venv\Scripts\python scripts/descargar_imdb.py          # IMDb non-commercial, cruzado por links.csv
   $env:TMDB_API_KEY = "tu_clave"                          # pósters y sinopsis desde la API de TMDb
   .venv\Scripts\python scripts/importar_letterboxd.py --csv ruta\ratings.csv   # usuario nuevo desde Letterboxd
   ```
9. **Preparar el modo demo** (carga ALS y perfiles de 25M y deja listo el subconjunto
   desde `DEMO_DESDE` para reentrenar en vivo; escribe `artefactos/demo`):
   ```powershell
   .venv\Scripts\python scripts/preparar_demo.py
   ```
10. **Streaming en vivo** (dos consolas):
    ```powershell
    # Consola A: Kafka -> Structured Streaming -> artefactos/estado.db
    .venv\Scripts\python scripts/streaming.py --fuente kafka --topic ratings
    # Consola B: reproduce los ratings en orden temporal (10 por segundo)
    .venv\Scripts\python scripts/productor_kafka.py --velocidad 10
    ```
11. **App**: `.venv\Scripts\python -m streamlit run app/app.py`.

## Guía de la demo en vivo (5 minutos)

**Antes de la expo (con internet, el día anterior):** pasos 1–9 terminados,
`check_entorno.py` sin faltantes, `ollama pull qwen3.5:9b` hecho y las figuras impresas.
El conector Kafka ya queda en `.ivy2`, así que el día de la expo no hace falta internet.

**Al llegar (3 consolas, en este orden):**

```powershell
$env:MODO = "demo"
# 1) Streaming del modo demo, reentrena ALS cada 5 minutos sobre el subconjunto
.venv\Scripts\python scripts/streaming.py --modo demo --reentrenar-cada-min 5
# 2) App
.venv\Scripts\python -m streamlit run app/app.py
# 3) Productor (lanzarlo recién en el minuto 1:30 del guion)
.venv\Scripts\python scripts/productor_kafka.py --desde 2019-01-01 --velocidad 20
```

**Guion:**

| Minuto | Qué mostrar | Qué decir |
|---|---|---|
| 0:00–0:45 | Figura de arquitectura del póster | MovieLens 25M → Kafka → Spark (batch ALS + Structured Streaming) → SQLite → Streamlit + LLM local |
| 0:45–1:30 | App: escribir un `userId`; barra lateral con su tipo de espectador y géneros | Perfil de gustos y KMeans; tarjetas = recomendaciones ALS |
| 1:30–2:30 | Lanzar el productor; mirar «Actualizado hace X s» y las tarjetas | Calificaciones en vivo: fold-in instantáneo y reentrenamiento periódico por lotes |
| 2:30–3:45 | Chat: «algo corto y liviano para hoy» | El LLM solo usa herramientas: no inventa películas ni datos y explica cada recomendación |
| 3:45–4:30 | Figuras de métricas y speedup | ALS vs popularidad con partición temporal; speedup de 1 a 6 núcleos frente al ideal |
| 4:30–5:00 | Preguntas, o un usuario nuevo con `importar_letterboxd.py` | Enriquecimiento legal: IMDb no comercial, TMDb con clave, sin scraping |

**Planes B:**
- Ollama no responde → la app avisa «Ollama no responde: usando modo reglas» y sigue;
  también se puede forzar con `$env:LLM_PROVIDER = "reglas"`.
- Kafka no responde → `scripts/streaming.py --fuente archivo --ruta <carpeta>` con
  archivos JSON (una línea por evento).
- El reentrenamiento tarda → bajar `DEMO_DESDE` a una fecha más reciente
  (p. ej. 2018-01-01) y volver a correr `preparar_demo.py`.

## App Streamlit con chat

```powershell
.venv\Scripts\python -m streamlit run app/app.py
```

- **Barra lateral:** `userId`, tipo de espectador, géneros con más peso, modo
  (completo/demo), «Actualizado hace X s» (leído de `artefactos/estado.db`) y la
  configuración del LLM.
- **Columna principal:** tarjetas con las recomendaciones y un chat
  (ej.: «algo corto y liviano para hoy»). El chat solo recomienda películas que
  entrega la herramienta `recomendar` y explica cada una con datos del usuario y de la
  película; las películas inventadas se descartan.

### Variables del LLM

| Variable | Por defecto | Uso |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` o `reglas` (determinista, sin red) |
| `LLM_MODEL` | `qwen3.5:9b` | modelo de Ollama |
| `OLLAMA_HOST` | `http://localhost:11434` | servidor de Ollama |
| `LLM_THINK` | `false` | razonamiento del modelo; su contenido nunca se muestra |

Si Ollama no responde, la app muestra «Ollama no responde: usando modo reglas» y sigue
funcionando con el proveedor `reglas`.

## Pruebas

Usan la muestra pequeña `muestras/ml-mini`, tardan menos de 2 minutos por suite y se
saltan con aviso si Kafka no responde.

```powershell
.venv\Scripts\python -m pytest -q
.venv\Scripts\python -m pytest -q tests/test_poster.py tests/test_readme.py
```

`scripts/estres_pruebas.py` repite suites varias veces para detectar pruebas
inestables (ej.: `.venv\Scripts\python scripts/estres_pruebas.py tests/test_batch.py --veces 3`).

## Licencias

- **MovieLens 25M** (GroupLens, Universidad de Minnesota): uso para investigación y
  educación; no se redistribuye (cada uno lo descarga con `descargar_movielens.py`) y
  se debe citar: F. Maxwell Harper y Joseph A. Konstan. 2015. *The MovieLens Datasets:
  History and Context*. ACM TiiS 5, 4. Uso comercial requiere permiso de GroupLens.
- **IMDb non-commercial datasets** (datasets.imdbws.com): licencia de uso no comercial
  (solo personal), según los términos de IMDb. No se publican los archivos descargados.
- **TMDb**: pósters y sinopsis vía su API con clave propia (`TMDB_API_KEY`). Hay que
  mostrar la atribución «This product uses the TMDB API but is not endorsed or certified
  by TMDB» y respetar sus términos de uso de la API.
- **Letterboxd**: solo se importa el CSV que el propio usuario exporta de su cuenta.
  **Nada de scraping** en ninguna fuente.
- Software: Apache Spark y Apache Kafka (Apache 2.0), Streamlit (Apache 2.0), Ollama (MIT);
  el modelo Qwen tiene su propia licencia.

## Solución de problemas en Windows

| Síntoma | Causa probable | Solución |
|---|---|---|
| `No se puede cargar ... setup_entorno.ps1 porque la ejecución de scripts está deshabilitada` | Política de ejecución | `powershell -ExecutionPolicy Bypass -File .\setup_entorno.ps1` o usar `py -3.12 setup_entorno.py` |
| `py -3.12` dice que no encuentra Python | Falta Python 3.12 o el lanzador `py` | Instalar Python 3.12 desde python.org marcando «py launcher» |
| `HADOOP_HOME and hadoop.home.dir are unset` o `UnsatisfiedLinkError ... NativeIO$Windows` | Falta winutils o `hadoop.dll` | Descargar winutils para Hadoop 3.3, dejar `winutils.exe` y `hadoop.dll` en `C:\hadoop\bin`, definir `HADOOP_HOME=C:\hadoop` y reabrir la consola; confirmar con `check_entorno.py` |
| `Python worker failed to connect back` | Spark usa otro Python | El proyecto fija `PYSPARK_PYTHON` al del `.venv`; lanzar siempre con `.venv\Scripts\python` |
| `JAVA_HOME is not set` o error de versión de Java | Java distinto de 17 | Instalar Java 17 y apuntar `JAVA_HOME` a esa carpeta |
| `java.lang.OutOfMemoryError` | 6 GB no alcanzan con 6 núcleos | Bajar `$env:NUCLEOS = "4"`, o usar `--fraccion` en `entrenar_batch.py` / `escalabilidad.py` |
| `Failed to find data source: kafka` | Falta el conector en `.ivy2` | `check_entorno.py --descargar-conector` (con internet, una vez) |
| El productor o el streaming no conectan a `localhost:9092` | Kafka apagado o mal anunciado | Levantar el contenedor de Kafka y revisar que anuncie `localhost:9092`; `check_entorno.py` lo prueba |
| La app dice «Sin actualizaciones en vivo» | El streaming no está corriendo o usa otro `estado.db` | Lanzar `scripts/streaming.py` con el mismo `MODO` que la app |
| El streaming no ve ratings nuevos tras reiniciar | El checkpoint ya avanzó | Es lo esperado (no duplica lotes); para reprocesar, borrar `<artefactos>\checkpoints\ratings` y usar `--starting-offsets earliest` |
| Errores por rutas largas al instalar | Límite de 260 caracteres | Habilitar rutas largas en Windows o mover el proyecto a una ruta más corta |
| Puerto 8501 ocupado | Otra app Streamlit abierta | `streamlit run app/app.py --server.port 8502` |
