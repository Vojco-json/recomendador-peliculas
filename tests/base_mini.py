"""Ayuda de pruebas: artefactos «base» (como los de 25M) calculados sobre la muestra mini.

ALS entrenado con los ratings anteriores al corte 2016-01-01 + perfiles con k=3.
Es más rápido que batch_als.ejecutar porque omite métricas y recomendaciones.
"""

from __future__ import annotations

import json
from pathlib import Path

from recomendador import batch_als, config, perfiles
from recomendador.datos import antes_de, cargar_movielens

CORTE = config.FECHA_CORTE_MINI   # 2016-01-01


def crear_base_mini(spark, ruta_mini, salida) -> Path:
    salida = Path(salida)
    salida.mkdir(parents=True, exist_ok=True)
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    tablas = cargar_movielens(ruta_mini, spark,
                              ["ratings", "movies", "genome_tags", "genome_scores"])
    train = antes_de(tablas["ratings"], CORTE)
    modelo = batch_als.entrenar(train)
    items, _ = batch_als.factores(modelo)
    items.toPandas().to_parquet(salida / "item_factors.parquet", index=False)
    (salida / "metricas.json").write_text(json.dumps({"params": batch_als.PARAMS_DEFECTO}),
                                          encoding="utf-8")
    perfiles.ejecutar(spark, tablas, salida, k=3, seed=42, hasta=CORTE, log=lambda *_: None)
    return salida


def crear_artefactos_app(spark, ruta_mini, salida) -> Path:
    """Base mini + peliculas_enriquecidas.parquet (duraciones de muestras/imdb-mini)."""
    from recomendador.enriquecimiento import enriquecer_imdb

    salida = crear_base_mini(spark, ruta_mini, salida)
    enriquecer_imdb(ruta_mini, config.RAIZ / "muestras" / "imdb-mini", salida)
    return salida


def crear_artefactos_cineruta(spark, ruta_mini, salida) -> Path:
    """Artefactos de la app CineRuta: los de crear_artefactos_app + app/*.parquet
    (precalculo.ejecutar con IMDb mini), como los deja scripts/precalcular_app.py."""
    from recomendador import precalculo

    salida = crear_artefactos_app(spark, ruta_mini, salida)
    precalculo.ejecutar(spark, ruta_mini, salida, CORTE,
                        imdb_dir=config.RAIZ / "muestras" / "imdb-mini", log=lambda *_: None)
    return salida
