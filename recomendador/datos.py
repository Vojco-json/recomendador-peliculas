"""Carga de MovieLens (ml-25m o la muestra ml-mini) con esquemas explícitos.

Los timestamps de MovieLens son segundos epoch UTC; los filtros por fecha se hacen
comparando enteros para no depender de la zona horaria.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

# Archivos de ml-25m y su nombre lógico.
ARCHIVOS = {
    "ratings": "ratings.csv",
    "movies": "movies.csv",
    "tags": "tags.csv",
    "links": "links.csv",
    "genome_tags": "genome-tags.csv",
    "genome_scores": "genome-scores.csv",
}

# Esquemas en DDL de Spark (mismo orden de columnas que ml-25m).
ESQUEMAS = {
    "ratings": "userId INT, movieId INT, rating DOUBLE, timestamp LONG",
    "movies": "movieId INT, title STRING, genres STRING",
    "tags": "userId INT, movieId INT, tag STRING, timestamp LONG",
    "links": "movieId INT, imdbId STRING, tmdbId INT",
    "genome_tags": "tagId INT, tag STRING",
    "genome_scores": "movieId INT, tagId INT, relevance DOUBLE",
}


def fecha_a_epoch(fecha: str) -> int:
    """'2016-01-01' -> segundos epoch UTC del inicio de ese día."""
    d = date.fromisoformat(fecha)
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp())


def cargar_movielens(ruta, spark=None, tablas=None) -> dict:
    """Carga las tablas de MovieLens como DataFrames de Spark.

    ruta:   carpeta con los CSV (ml-25m o muestras/ml-mini).
    spark:  SparkSession; si es None usa la activa o crea una con config por defecto.
    tablas: lista de nombres lógicos (ver ARCHIVOS); por defecto, todas las que existan.
    Devuelve un dict {nombre: DataFrame}. imdbId se lee como texto para conservar
    los ceros a la izquierda (tt + 7 dígitos).
    """
    ruta = Path(ruta)
    if not ruta.is_dir():
        raise FileNotFoundError(f"No existe la carpeta de datos: {ruta}")
    if spark is None:
        from pyspark.sql import SparkSession

        spark = SparkSession.getActiveSession()
        if spark is None:
            from .spark_utils import crear_spark

            spark = crear_spark()
    salida = {}
    for nombre in tablas or ARCHIVOS:
        archivo = ruta / ARCHIVOS[nombre]
        if not archivo.exists():
            if tablas:  # se pidió explícitamente
                raise FileNotFoundError(f"Falta {archivo}")
            continue
        salida[nombre] = (spark.read
                          .option("header", True)
                          .option("quote", '"')
                          .option("escape", '"')
                          .schema(ESQUEMAS[nombre])
                          .csv(str(archivo)))
    return salida


def cargar_movielens_pandas(ruta, tablas=None) -> dict:
    """Igual que cargar_movielens pero con pandas (útil para tablas pequeñas y pruebas)."""
    import pandas as pd

    ruta = Path(ruta)
    tipos = {"imdbId": str}
    salida = {}
    for nombre in tablas or ARCHIVOS:
        archivo = ruta / ARCHIVOS[nombre]
        if archivo.exists():
            salida[nombre] = pd.read_csv(archivo, dtype=tipos if nombre == "links" else None)
    return salida


def antes_de(df_ratings, fecha: str):
    """Ratings con timestamp anterior a la fecha (Spark)."""
    return df_ratings.filter(df_ratings["timestamp"] < fecha_a_epoch(fecha))


def desde(df_ratings, fecha: str):
    """Ratings con timestamp igual o posterior a la fecha (Spark)."""
    return df_ratings.filter(df_ratings["timestamp"] >= fecha_a_epoch(fecha))
