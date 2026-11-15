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

### Métricas de ranking: ALS implícito, híbrido y cold start

La primera corrida de 25M dio RMSE 0,845 con ALS explícito, pero Precision@10 y NDCG@10
de 0,0002 (azar) frente a 0,060 / 0,072 de la popularidad. Se descartó primero un error de
IDs: `metricas.json` trae `error_ids_max`, la diferencia entre la predicción de Spark y u·v
calculado con los factores guardados (debe ser ~0). El problema era de modelo: el ALS
explícito predice bien la nota, pero sube al top películas con muy pocos votos.

`entrenar_batch.py` ahora:

1. Recomienda solo entre películas con **al menos 50 votos** (`--min-votos`).
2. Entrena **ALS implícito** (`implicitPrefs=True`, `alpha` configurable con `--alphas` o
   `ALS_ALPHA`, ranks `--ranks-implicito 50,100`).
3. Elige rank, alpha y el peso del **híbrido ALS + popularidad** (`--pesos-hibrido`;
   score = peso·z(ALS) + (1−peso)·z(log votos)) con **validación temporal**: entrena antes de
   2017 y valida con 2017. El conjunto de prueba (2018 en adelante) nunca se usa para elegir.
4. Reporta en `metricas.json` y en la figura `metricas`: `als` (explícito, con RMSE),
   `als_implicito`, `hibrido`, `popularidad`, la grilla de `validacion` y `cold_start_pct`.
   Los artefactos `modelo_als`/factores y `recs_top20` salen del modelo del híbrido.

**Cold start:** cerca del **82 % de las calificaciones de prueba** son de usuarios (o películas)
que no aparecen antes del corte. Ningún modelo de factores puede recomendarles nada, por eso
quedan fuera de Precision@10/NDCG@10 (se reporta el porcentaje como `cold_start_pct`). En la
demo, esos usuarios se atienden con la popularidad, el fold-in del streaming o Letterboxd.


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

