"""Pruebas del paquete 1: configuración, SparkSession, Kafka, check_entorno y muestra mini."""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import uuid

import pytest

from recomendador import config, datos, kafka_utils, spark_utils

CORTE = "2016-01-01"


# --------------------------------------------------------------------------- #
# Configuración
# --------------------------------------------------------------------------- #

@pytest.fixture
def recargar_config(monkeypatch):
    """Permite cambiar variables de entorno y deja config como estaba al final."""
    yield monkeypatch
    monkeypatch.undo()
    importlib.reload(config)


def test_config_valores_por_defecto(recargar_config):
    for var in ("MODO", "DEMO_DESDE", "LLM_THINK", "LLM_MODEL", "SPARK_MEMORIA", "NUCLEOS",
                "KAFKA_BOOTSTRAP", "TOPIC", "ARTEFACTOS", "ARTEFACTOS_BASE"):
        recargar_config.delenv(var, raising=False)
    c = importlib.reload(config)
    assert c.MODO == "completo"
    assert c.DEMO_DESDE == "2015-01-01"
    assert c.LLM_THINK is False
    assert c.LLM_MODEL == "qwen3.5:9b"
    assert c.SPARK_MEMORIA == "6g"
    assert c.NUCLEOS == 6
    assert c.KAFKA_BOOTSTRAP == "localhost:9092"
    assert c.TOPIC == "ratings"
    assert c.ARTEFACTOS_BASE == c.RAIZ / "artefactos" / "25m"


def test_config_lee_variables_de_entorno(recargar_config):
    recargar_config.setenv("MODO", "demo")
    recargar_config.setenv("DEMO_DESDE", "2016-06-01")
    recargar_config.setenv("LLM_THINK", "true")
    recargar_config.setenv("NUCLEOS", "3")
    recargar_config.setenv("KAFKA_BOOTSTRAP", "otrohost:9093")
    recargar_config.setenv("SPARK_MEMORIA", "12g")  # se limita a 6g
    with pytest.warns(UserWarning):
        c = importlib.reload(config)
    assert c.MODO == "demo"
    assert c.DEMO_DESDE == "2016-06-01"
    assert c.LLM_THINK is True
    assert c.NUCLEOS == 3
    assert c.KAFKA_BOOTSTRAP == "otrohost:9093"
    assert c.SPARK_MEMORIA == "6g"
    assert c.artefactos_activos() == c.ARTEFACTOS_DEMO


def test_config_rechaza_modo_invalido(recargar_config):
    recargar_config.setenv("MODO", "turbo")
    with pytest.raises(ValueError):
        importlib.reload(config)


def test_opciones_spark_sin_jvm():
    op = spark_utils.opciones_spark(nucleos=4, kafka=True, memoria="6g")
    assert op["spark.master"] == "local[4]"
    assert op["spark.driver.memory"] == "6g"
    assert op["spark.sql.shuffle.partitions"] == "8"
    assert op["spark.jars.ivy"].endswith(".ivy2")
    version = spark_utils.version_pyspark()
    assert version.startswith("3.5.")
    conector = f"spark-sql-kafka-0-10_2.12-{version}.jar"
    if spark_utils.jars_conector_en_cache():
        # Con caché: jars locales (sin resolución Ivy ni red), misma versión que PySpark.
        assert "spark.jars.packages" not in op
        assert conector in op["spark.jars"]
        assert "kafka-clients" in op["spark.jars"]
    else:
        assert op["spark.jars.packages"] == f"org.apache.spark:spark-sql-kafka-0-10_2.12:{version}"
    # Sin caché (o forzando la descarga) se usa la coordenada Maven de la misma versión.
    sin_cache = spark_utils.opciones_spark(nucleos=4, kafka=True, usar_cache=False)
    assert sin_cache["spark.jars.packages"] == f"org.apache.spark:spark-sql-kafka-0-10_2.12:{version}"
    assert "spark.jars.packages" not in spark_utils.opciones_spark(nucleos=1)
    assert "spark.jars" not in spark_utils.opciones_spark(nucleos=1)
    with pytest.raises(ValueError):
        spark_utils.opciones_spark(nucleos=2, memoria="8g")


def test_popen_vigilado_no_deja_colgado_el_arranque():
    """Un .cmd que termina con código 0 sin dar el puerto no debe colgar a pyspark."""
    clase = spark_utils._popen_vigilado()
    proc = clase([sys.executable, "-c", "pass"])
    proc.wait()
    assert proc.returncode == 0
    assert proc.poll() == 1          # durante el arranque: 'murió', pyspark no se cuelga
    proc.arrancando = False
    assert proc.poll() == 0          # ya arrancado: código real


# --------------------------------------------------------------------------- #
# Kafka
# --------------------------------------------------------------------------- #

def test_kafka_disponible_no_lanza_excepcion():
    assert isinstance(kafka_utils.kafka_disponible(timeout=3), bool)
    # Un puerto donde no hay broker responde False, sin excepción.
    assert kafka_utils.kafka_disponible(timeout=1, bootstrap="127.0.0.1:1") is False
    assert kafka_utils.kafka_disponible(timeout=1, bootstrap="host-inexistente.invalid:9092") is False


def test_crear_topic_idempotente(requiere_kafka):
    nombre = f"ratings_test_{uuid.uuid4().hex[:8]}"
    try:
        assert kafka_utils.crear_topic(nombre, particiones=1) is True
        assert kafka_utils.crear_topic(nombre, particiones=1) is False
    finally:
        kafka_utils.borrar_topic(nombre)


# --------------------------------------------------------------------------- #
# Muestra mini (valores documentados en muestras/ml-mini/LEEME.md)
# --------------------------------------------------------------------------- #

def test_muestra_conteos_pandas(ruta_mini):
    t = datos.cargar_movielens_pandas(ruta_mini)
    r = t["ratings"]
    assert list(r.columns) == ["userId", "movieId", "rating", "timestamp"]
    assert list(t["movies"].columns) == ["movieId", "title", "genres"]
    assert list(t["links"].columns) == ["movieId", "imdbId", "tmdbId"]
    assert list(t["tags"].columns) == ["userId", "movieId", "tag", "timestamp"]
    assert list(t["genome_tags"].columns) == ["tagId", "tag"]
    assert list(t["genome_scores"].columns) == ["movieId", "tagId", "relevance"]
    assert len(r) == 300 and r["userId"].nunique() == 15 and r["movieId"].nunique() == 25
    assert len(t["movies"]) == 25 and len(t["links"]) == 25
    assert len(t["tags"]) == 13 and len(t["genome_tags"]) == 10 and len(t["genome_scores"]) == 250
    assert not r.duplicated(["userId", "movieId"]).any()
    assert r["rating"].between(0.5, 5.0).all()
    assert r["timestamp"].min() >= datos.fecha_a_epoch("2010-01-01")
    assert r["timestamp"].max() < datos.fecha_a_epoch("2020-01-01")
    # imdbId conserva los ceros (tt + 7 dígitos): Toy Story = tt0114709, tmdbId 862
    toy = t["links"].set_index("movieId").loc[1]
    assert toy["imdbId"] == "0114709" and toy["tmdbId"] == 862
    corte = datos.fecha_a_epoch(CORTE)
    demo = datos.fecha_a_epoch("2015-01-01")
    assert (r["timestamp"] < corte).sum() == 210
    assert (r["timestamp"] >= corte).sum() == 90
    assert ((r["timestamp"] >= demo) & (r["timestamp"] < corte)).sum() == 90
    assert (r["timestamp"] >= demo).sum() == 180
    assert (r["timestamp"] >= datos.fecha_a_epoch("2018-01-01")).sum() == 30


def _top_generos(t, usuario, n=2):
    """Géneros con mayor suma de (rating - media del usuario)."""
    r = t["ratings"][t["ratings"]["userId"] == usuario].merge(t["movies"], on="movieId")
    r = r.assign(peso=r["rating"] - r["rating"].mean(), genero=r["genres"].str.split("|"))
    pesos = r.explode("genero").groupby("genero")["peso"].sum().sort_values(ascending=False)
    return set(pesos.index[:n])


def test_muestra_orientacion_usuarios(ruta_mini):
    t = datos.cargar_movielens_pandas(ruta_mini)
    assert _top_generos(t, 1) == {"Action", "Thriller"}
    assert _top_generos(t, 2) == {"Romance", "Drama"}


def test_muestra_spark_toy_story_mas_popular(spark, ruta_mini):
    from pyspark.sql import functions as F

    tablas = datos.cargar_movielens(ruta_mini, spark=spark)
    ratings = tablas["ratings"]
    assert ratings.count() == 300
    assert tablas["movies"].count() == 25
    entrenamiento = datos.antes_de(ratings, CORTE)
    assert entrenamiento.count() == 210
    assert datos.desde(ratings, CORTE).count() == 90
    top = (entrenamiento.groupBy("movieId").agg(F.count("*").alias("n"))
           .orderBy(F.desc("n"), "movieId").limit(2).collect())
    assert top[0]["movieId"] == 1 and top[0]["n"] == 15
    assert top[1]["n"] == 11  # Toy Story es la única con 15
    titulo = tablas["movies"].filter("movieId = 1").first()["title"]
    assert titulo == "Toy Story (1995)"


def test_spark_configuracion(spark):
    conf = spark.sparkContext.getConf()
    assert conf.get("spark.master") == "local[2]"
    assert conf.get("spark.driver.memory") == "6g"
    assert conf.get("spark.jars.ivy").endswith(".ivy2")
    assert spark.conf.get("spark.sql.shuffle.partitions") == "4"
    if spark.conector_kafka_ok:
        # El conector quedó en el classpath (como jar local o como paquete Maven).
        cargado = conf.get("spark.jars", "") + conf.get("spark.jars.packages", "")
        assert "spark-sql-kafka-0-10_2.12" in cargado
        assert spark_utils.version_pyspark() in cargado
        # La fuente 'kafka' de Structured Streaming es cargable desde la JVM.
        clase = spark._jvm.org.apache.spark.util.Utils.classForName(
            "org.apache.spark.sql.kafka010.KafkaSourceProvider", True, False)
        assert clase.getName().endswith("KafkaSourceProvider")


# --------------------------------------------------------------------------- #
# check_entorno.py
# --------------------------------------------------------------------------- #

def test_check_entorno_reporta_cada_chequeo():
    proc = subprocess.run([sys.executable, str(config.RAIZ / "check_entorno.py")],
                          capture_output=True, text=True, timeout=90, cwd=config.RAIZ,
                          encoding="utf-8", errors="replace",
                          env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    salida = proc.stdout + proc.stderr
    assert proc.returncode == 0, salida
    assert "Traceback" not in salida
    for chequeo in ("Python 3.12", "Java 17", "JAVA_HOME", "HADOOP_HOME", "winutils.exe",
                    "hadoop.dll", "pyspark importable", "Conector Kafka", "Kafka (broker)"):
        lineas = [l for l in salida.splitlines() if chequeo in l and ("[OK]" in l or "[FALTA]" in l)]
        assert lineas, f"check_entorno no reportó '{chequeo}':\n{salida}"
