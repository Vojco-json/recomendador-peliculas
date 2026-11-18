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


def crear_artefactos_moviepredict(spark, ruta_mini, salida) -> Path:
    """Artefactos de la app MoviePredict: los de crear_artefactos_app + app/*.parquet
    (precalculo.ejecutar con IMDb mini), como los deja scripts/precalcular_app.py."""
    from recomendador import precalculo

    salida = crear_artefactos_app(spark, ruta_mini, salida)
    precalculo.ejecutar(spark, ruta_mini, salida, CORTE,
                        imdb_dir=config.RAIZ / "muestras" / "imdb-mini", log=lambda *_: None)
    return salida


def html_pagina(at) -> str:
    """Markdown de la página + HTML de los componentes de carátulas clicables."""
    import json
    partes = [str(m.value) for m in at.markdown]
    for el in at.get("component_instance") or []:
        try:
            partes.append(json.loads(el.proto.json_args).get("html", ""))
        except (AttributeError, ValueError, TypeError):
            continue
    return "\n".join(partes)


def clic_caratula(at, movieId: int, origen: str):
    """Simula el clic en una carátula: AppTest no puede pulsar dentro del iframe del
    componente, así que se escribe lo mismo que hace comun.abrir_ficha y se recarga."""
    at.session_state["ficha_abierta"] = {"movieId": int(movieId), "origen": origen}
    return at.run()


def ir_a(at, seccion: str):
    """Elige una sección de la app (solo se ejecuta la sección activa) y recarga."""
    try:
        radio = at.radio(key="seccion")
    except KeyError:                     # login u onboarding: no hay navegación
        return at
    if radio.value != seccion:
        radio.set_value(seccion).run()
    return at
