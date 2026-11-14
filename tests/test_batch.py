"""Pruebas del paquete 3: ALS batch con partición temporal, popularidad, métricas y fold-in.

Usan la SparkSession compartida (conftest.py) y la muestra ml-mini con corte 2016-01-01
(valores conocidos en muestras/ml-mini/LEEME.md). El entrenamiento completo se corre
una sola vez por módulo y se reutiliza.
"""

from __future__ import annotations

import json
import math

import numpy as np
import pandas as pd
import pytest

from recomendador import batch_als, foldin
from recomendador.datos import cargar_movielens, fecha_a_epoch

CORTE = "2016-01-01"


@pytest.fixture(scope="module")
def ratings(spark, ruta_mini):
    return cargar_movielens(ruta_mini, spark, ["ratings"])["ratings"].cache()


@pytest.fixture(scope="module")
def corrida(spark, ratings, tmp_path_factory):
    """Una corrida completa sobre la muestra, con artefactos en una carpeta temporal."""
    salida = tmp_path_factory.mktemp("mini_batch")
    res = batch_als.ejecutar(spark, ratings, CORTE, salida, log=lambda *_: None)
    return res, salida


# --------------------------------------------------------------------------- #
# Partición temporal
# --------------------------------------------------------------------------- #

def test_particion_temporal_correcta(ratings):
    part = batch_als.particion_temporal(ratings, CORTE)
    t = fecha_a_epoch(CORTE)
    assert part["n_train"] == 210 and part["n_test"] == 90 and part["descartados"] == 0
    assert part["train"].filter(f"timestamp >= {t}").count() == 0
    assert part["test"].filter(f"timestamp < {t}").count() == 0


def test_test_descarta_usuarios_e_items_sin_entrenamiento(spark, tmp_path):
    t = fecha_a_epoch(CORTE)
    filas = [(1, 10, 4.0, t - 100), (1, 20, 3.0, t - 50), (2, 10, 5.0, t - 10),
             (1, 20, 4.0, t + 10),    # válido
             (3, 10, 4.0, t + 20),    # usuario nuevo -> se descarta
             (2, 99, 4.0, t + 30)]    # película nueva -> se descarta
    # Se lee desde CSV (JVM) en vez de createDataFrame, que necesita workers de Python.
    csv = tmp_path / "ratings.csv"
    pd.DataFrame(filas, columns=["userId", "movieId", "rating", "timestamp"]).to_csv(
        csv, index=False)
    df = spark.read.option("header", True).schema(
        "userId INT, movieId INT, rating DOUBLE, timestamp LONG").csv(str(csv))
    part = batch_als.particion_temporal(df, CORTE)
    assert (part["n_train"], part["n_test"], part["descartados"]) == (3, 1, 2)


def test_desde_reduce_el_entrenamiento(ratings):
    completo = batch_als.particion_temporal(ratings, CORTE)
    demo = batch_als.particion_temporal(ratings, CORTE, desde="2015-01-01")
    assert demo["n_train"] == 90 < completo["n_train"]  # LEEME: 90 entre 2015 y el corte
    assert demo["train"].filter(f"timestamp < {fecha_a_epoch('2015-01-01')}").count() == 0
    with pytest.raises(ValueError):
        batch_als.particion_temporal(ratings, CORTE, desde="2017-01-01")


# --------------------------------------------------------------------------- #
# Popularidad y métricas
# --------------------------------------------------------------------------- #

def test_toy_story_primera_en_popularidad(ratings):
    train = batch_als.particion_temporal(ratings, CORTE)["train"]
    pop, _ = batch_als.popularidad(train)
    assert int(pop.iloc[0]["movieId"]) == 1 and int(pop.iloc[0]["n_votos"]) == 15
    assert list(pop["movieId"][1:3]) == [296, 2571]  # 11 votos cada una


def test_precision_ndcg_valores_conocidos():
    assert batch_als.precision_ndcg([1, 2, 3], {1, 3}, k=10) == pytest.approx(
        (0.2, (1 + 1 / math.log2(4)) / (1 + 1 / math.log2(3))))
    assert batch_als.precision_ndcg([5, 6], {1}, k=10) == (0.0, 0.0)
    assert batch_als.precision_ndcg(list(range(10)), set(range(10)), k=10) == (1.0, 1.0)


def test_metricas_en_rango_y_sin_nan(corrida):
    res, _ = corrida
    for modelo in ("als", "popularidad"):
        m = res[modelo]
        assert all(not math.isnan(m[c]) for c in ("RMSE", "Precision@10", "NDCG@10"))
        assert 0 <= m["Precision@10"] <= 1 and 0 <= m["NDCG@10"] <= 1
        assert m["RMSE"] > 0
    assert res["popularidad"]["top10"][0] == 1
    assert res["relevante_min"] == 4.0
    tabla = batch_als.tabla_metricas(res)
    for texto in ("RMSE", "Precision@10", "NDCG@10", "ALS", "Popularidad"):
        assert texto in tabla
    assert "nan" not in tabla.lower()


def test_metricas_ranking_con_usuarios_evaluables(corrida, ratings):
    """En ml-mini hay exactamente 2 usuarios (12 y 15) con un rating >= 4 posterior al
    corte (LEEME), así que con el umbral oficial se evalúan 2 usuarios en todos los
    modelos. Con umbral 3 hay más relevantes y el cálculo da valores en (0, 1]."""
    res, salida = corrida
    for modelo in ("als", "popularidad", "als_implicito", "hibrido"):
        if modelo in res:
            assert res[modelo]["usuarios_evaluados"] == 2, modelo
    # Al usuario 12 le quedan pocas películas sin ver: Schindler's List (527) entra en su
    # top-10, así que al menos un modelo tiene precisión > 0 con el umbral oficial.
    assert max(res[m]["Precision@10"] for m in ("als", "popularidad") if m in res) > 0
    recs = pd.read_parquet(salida / "recs_top20.parquet")
    test = batch_als.particion_temporal(ratings, CORTE)["test"]
    m = batch_als.metricas_ranking(recs, test, k=10, relevante_min=3.0)
    assert m["usuarios_evaluados"] > 0
    assert 0 < m["Precision@10"] <= 1 and 0 < m["NDCG@10"] <= 1


def test_artefactos_guardados(corrida):
    res, salida = corrida
    for nombre in ("metricas.json", "modelo_als", "item_factors.parquet",
                   "user_factors.parquet", "recs_top20.parquet"):
        assert (salida / nombre).exists(), nombre
    meta = json.loads((salida / "metricas.json").read_text(encoding="utf-8"))
    for clave in ("als", "popularidad", "corte", "n_train", "n_test", "segundos"):
        assert clave in meta
    assert meta["corte"] == CORTE and meta["n_train"] == 210 and meta["n_test"] == 90


def test_recs_top20_sin_peliculas_vistas(corrida, ratings):
    _, salida = corrida
    recs = pd.read_parquet(salida / "recs_top20.parquet")
    train = batch_als.particion_temporal(ratings, CORTE)["train"].toPandas()
    vistas = set(zip(train["userId"], train["movieId"]))
    assert len(recs) > 0 and recs["userId"].nunique() == 15
    assert not any((u, m) in vistas for u, m in zip(recs["userId"], recs["movieId"]))
    # 25 películas - 14 vistas = 11 candidatas por usuario (menos de 20)
    assert recs.groupby("userId").size().max() == 11


def test_entrenar_es_reutilizable(ratings):
    train = batch_als.particion_temporal(ratings, CORTE, desde="2015-01-01")["train"]
    modelo = batch_als.entrenar(train, {"rank": 4, "maxIter": 3})
    assert modelo.rank == 4 and modelo.itemFactors.count() > 0


# --------------------------------------------------------------------------- #
# Fold-in
# --------------------------------------------------------------------------- #

def test_foldin_sin_peliculas_vistas(corrida):
    _, salida = corrida
    vistas = [(1, 5.0), (2571, 4.5), (1036, 4.0)]
    recs = foldin.recomendar_foldin(salida / "item_factors.parquet", vistas, n=5,
                                    excluir=[296], reg=0.1)
    ids = [m for m, _ in recs]
    assert len(recs) == 5 and len(set(ids)) == 5
    assert not set(ids) & {1, 2571, 1036, 296}
    scores = [s for _, s in recs]
    assert scores == sorted(scores, reverse=True)


def test_foldin_matriz_conocida_y_casos_borde():
    ids = np.array([10, 20, 30, 40])
    v = np.array([[1.0, 0.0], [0.9, 0.1], [0.0, 1.0], [0.8, 0.0]])
    recs = foldin.recomendar_foldin((ids, v), [(10, 5.0)], n=2, reg=0.01)
    assert [m for m, _ in recs] == [20, 40]  # parecidas a la 10, sin la 10
    assert foldin.recomendar_foldin((ids, v), [(999, 5.0)], n=3) == []
    assert len(foldin.recomendar_foldin((ids, v), [(10, 5.0)], n=10)) == 3
