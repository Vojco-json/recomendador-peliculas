"""Fábrica de SparkSession para el proyecto.

- Spark local con ``local[nucleos]`` y ``spark.driver.memory`` (6g por defecto, máximo 6g).
- Particiones de shuffle proporcionales a los núcleos (2 por núcleo).
- Caché Ivy dentro del proyecto (``.ivy2``): tras la primera descarga del conector
  Kafka todo funciona sin internet (importante el día de la expo).
- Con ``kafka=True`` agrega ``org.apache.spark:spark-sql-kafka-0-10_2.12`` con la
  MISMA versión que ``pyspark.__version__``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def version_pyspark() -> str:
    """Versión de PySpark instalada (por ejemplo '3.5.5')."""
    import pyspark

    return pyspark.__version__


def conector_kafka(version: str | None = None) -> str:
    """Coordenada Maven del conector Kafka para la versión de PySpark instalada."""
    return f"org.apache.spark:spark-sql-kafka-0-10_2.12:{version or version_pyspark()}"


def conector_en_cache(version: str | None = None) -> Path | None:
    """Ruta del jar del conector en la caché Ivy del proyecto, o None si no está."""
    version = version or version_pyspark()
    nombre = f"org.apache.spark_spark-sql-kafka-0-10_2.12-{version}.jar"
    candidato = config.IVY_DIR / "jars" / nombre
    return candidato if candidato.exists() else None


def jars_conector_en_cache(version: str | None = None) -> list[Path] | None:
    """Jars del conector Kafka y sus dependencias ya descargados en ``.ivy2/jars``.

    Devuelve None si falta el conector o su ``spark-token-provider`` de la misma
    versión. Se excluyen los jars de otras versiones del conector (si alguna vez
    se cambió la versión de PySpark) para no mezclar clases.
    """
    version = version or version_pyspark()
    carpeta = config.IVY_DIR / "jars"
    propios = (f"org.apache.spark_spark-sql-kafka-0-10_2.12-{version}.jar",
               f"org.apache.spark_spark-token-provider-kafka-0-10_2.12-{version}.jar")
    if not carpeta.is_dir() or not all((carpeta / n).exists() for n in propios):
        return None
    jars = []
    for jar in sorted(carpeta.glob("*.jar")):
        es_conector = jar.name.startswith(("org.apache.spark_spark-sql-kafka-0-10",
                                           "org.apache.spark_spark-token-provider-kafka-0-10"))
        if es_conector and jar.name not in propios:
            continue
        jars.append(jar)
    return jars


def _preparar_entorno_windows() -> None:
    """Ajustes del proceso para que Spark funcione bien en Windows.

    - PYSPARK_PYTHON apunta al Python del .venv (evita 'Python worker failed to connect back').
    - Si existe HADOOP_HOME, agrega %HADOOP_HOME%\\bin al PATH para que se cargue hadoop.dll.
    """
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    hadoop = os.environ.get("HADOOP_HOME")
    if hadoop:
        bin_dir = str(Path(hadoop) / "bin")
        if bin_dir.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def opciones_spark(nucleos: int | None = None, kafka: bool = False,
                   memoria: str | None = None, usar_cache: bool = True) -> dict[str, str]:
    """Configuración que usa crear_spark (separada para poder probarla sin JVM).

    Con ``kafka=True``:
    - si el conector y sus dependencias ya están en ``.ivy2/jars`` (y ``usar_cache``),
      se pasan como ``spark.jars`` (rutas locales). Así el arranque NO ejecuta la
      resolución Ivy, que consulta Maven Central y podía quedarse esperando la red
      varios minutos de forma intermitente (causa de las pruebas colgadas);
    - si no están, se usa ``spark.jars.packages`` con la coordenada Maven de la misma
      versión que PySpark, que los descarga a ``.ivy2`` (solo la primera vez).
    """
    n = int(nucleos or config.NUCLEOS)
    if n < 1:
        raise ValueError("nucleos debe ser 1 o más")
    memoria = (memoria or config.SPARK_MEMORIA).lower()
    if config.memoria_a_mb(memoria) > config.MEMORIA_MAXIMA_MB:
        raise ValueError(f"memoria {memoria} supera el máximo permitido de 6g")
    particiones = str(max(2, 2 * n))
    opciones = {
        "spark.master": f"local[{n}]",
        "spark.driver.memory": memoria,
        "spark.sql.shuffle.partitions": particiones,
        "spark.default.parallelism": particiones,
        "spark.jars.ivy": config.IVY_DIR.as_posix(),
        "spark.sql.session.timeZone": "UTC",
        "spark.driver.host": "127.0.0.1",
        "spark.driver.bindAddress": "127.0.0.1",
        "spark.ui.showConsoleProgress": "false",
        "spark.sql.adaptive.enabled": "true",
    }
    if kafka:
        locales = jars_conector_en_cache() if usar_cache else None
        if locales:
            opciones["spark.jars"] = ",".join(j.resolve().as_uri() for j in locales)
        else:
            opciones["spark.jars.packages"] = conector_kafka()
    return opciones


def crear_spark(nucleos: int | None = None, kafka: bool = False, memoria: str | None = None,
                app: str = "recomendador-peliculas", extra: dict | None = None):
    """Crea (o reutiliza) la SparkSession del proceso.

    Parámetros
    ----------
    nucleos: núcleos de ``local[N]``; por defecto ``config.NUCLEOS``.
    kafka:   si es True agrega el conector spark-sql-kafka de la misma versión que PySpark.
             La primera vez lo descarga de Maven a ``.ivy2``; después funciona sin internet.
    memoria: memoria del driver (por defecto ``config.SPARK_MEMORIA``; máximo 6g).
    extra:   opciones adicionales de Spark.

    Nota: si ya hay una SparkSession activa en el proceso, Spark la reutiliza y las
    opciones de JVM (memoria, paquetes) no cambian. Llama a ``detener_spark()`` antes
    si necesitas otra configuración.
    """
    from pyspark.sql import SparkSession

    _preparar_entorno_windows()
    config.IVY_DIR.mkdir(parents=True, exist_ok=True)

    builder = SparkSession.builder.appName(app)
    for clave, valor in {**opciones_spark(nucleos, kafka, memoria), **(extra or {})}.items():
        builder = builder.config(clave, valor)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


def detener_spark() -> None:
    """Detiene la SparkSession activa, si existe."""
    from pyspark.sql import SparkSession

    activa = SparkSession.getActiveSession()
    if activa is not None:
        activa.stop()


def precalentar_conector() -> Path | None:
    """Descarga el conector Kafka a la caché .ivy2 (requiere internet la primera vez).

    Devuelve la ruta del jar si quedó en caché.
    """
    if conector_en_cache():
        return conector_en_cache()
    spark = crear_spark(nucleos=1, kafka=True, memoria="1g", app="precalentar-conector")
    spark.stop()
    return conector_en_cache()
