"""Motor batch: ALS de MLlib con partición temporal, línea base de popularidad y métricas.

Flujo (lo usa scripts/entrenar_batch.py y, en parte, el reentrenamiento del streaming):

1. ``particion_temporal``: entrenamiento = ratings anteriores al corte (opcionalmente
   solo desde ``desde``); prueba = ratings desde el corte de usuarios e ítems que
   aparecen en el entrenamiento (equivale a ``coldStartStrategy='drop'``).
2. ``entrenar(df_train, params)``: ALS explícito con semilla fija (reutilizable).
3. ``top_n_sin_vistas``: top-N por usuario excluyendo lo ya visto en el entrenamiento.
   ALS, joins y agregaciones corren en Spark; el ranking final se hace con numpy en
   el driver por bloques de usuarios (sin workers de Python, que fallan en Windows).
4. ``evaluar``: RMSE, Precision@10 y NDCG@10 (relevante = rating >= 4) para ALS y
   para la popularidad (top por número de ratings; su RMSE usa la media por ítem).
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path

import numpy as np

from . import config
from .datos import fecha_a_epoch

# Parámetros por defecto de ALS (se pueden cambiar desde el script o el streaming).
PARAMS_DEFECTO = {"rank": 10, "regParam": 0.1, "maxIter": 10, "seed": 42}
K = 10                  # top-K de las métricas de ranking
N_RECS = 20             # recomendaciones guardadas por usuario
MIN_VOTOS = 50          # mínimo de votos para entrar en la lista de popularidad
MIN_CANDIDATOS_POP = 100  # si menos ítems cumplen MIN_VOTOS, se usan todos (muestra mini)


# --------------------------------------------------------------------------- #
# Datos y partición temporal
# --------------------------------------------------------------------------- #

def filtrar_fraccion(ratings, fraccion: float | None):
    """Submuestra determinista por usuario (conserva historias completas)."""
    from pyspark.sql import functions as F

    if fraccion is None or fraccion >= 1:
        return ratings
    if not 0 < fraccion < 1:
        raise ValueError("fraccion debe estar en (0, 1]")
    cubeta = F.abs(F.hash("userId")) % 10000
    return ratings.filter(cubeta < int(round(fraccion * 10000)))


def particion_temporal(ratings, corte: str, desde: str | None = None) -> dict:
    """Divide los ratings en entrenamiento y prueba según la fecha de corte.

    Devuelve dict con ``train``, ``test`` (ya filtrado a usuarios e ítems conocidos),
    ``test_bruto`` y los conteos ``n_train``, ``n_test``, ``n_test_bruto`` y
    ``descartados`` (ratings de prueba de usuarios o películas sin entrenamiento).
    """
    t_corte = fecha_a_epoch(corte)
    train = ratings.filter(ratings["timestamp"] < t_corte)
    if desde:
        t_desde = fecha_a_epoch(desde)
        if t_desde >= t_corte:
            raise ValueError(f"--desde {desde} debe ser anterior al corte {corte}")
        train = train.filter(train["timestamp"] >= t_desde)
    test_bruto = ratings.filter(ratings["timestamp"] >= t_corte)

    usuarios = train.select("userId").distinct()
    items = train.select("movieId").distinct()
    test = test_bruto.join(usuarios, "userId", "left_semi").join(items, "movieId", "left_semi")

    train, test = train.cache(), test.cache()
    n_train, n_test, n_bruto = train.count(), test.count(), test_bruto.count()
    return {"train": train, "test": test, "test_bruto": test_bruto,
            "n_train": n_train, "n_test": n_test, "n_test_bruto": n_bruto,
            "descartados": n_bruto - n_test}


# --------------------------------------------------------------------------- #
# Entrenamiento (reutilizable por el streaming)
# --------------------------------------------------------------------------- #

def entrenar(df_train, params: dict | None = None):
    """Entrena ALS explícito sobre un DataFrame (userId, movieId, rating).

    ``params`` admite rank, regParam, maxIter y seed (ver PARAMS_DEFECTO).
    Devuelve el ``ALSModel`` (con coldStartStrategy='drop').
    """
    from pyspark.ml.recommendation import ALS

    p = {**PARAMS_DEFECTO, **(params or {})}
    als = ALS(userCol="userId", itemCol="movieId", ratingCol="rating",
              rank=int(p["rank"]), regParam=float(p["regParam"]), maxIter=int(p["maxIter"]),
              seed=int(p["seed"]), implicitPrefs=False, nonnegative=False,
              coldStartStrategy="drop")
    return als.fit(df_train.select("userId", "movieId", "rating"))


def factores(modelo) -> tuple:
    """(item_factors, user_factors) con columnas (movieId|userId, features)."""
    items = modelo.itemFactors.withColumnRenamed("id", "movieId")
    usuarios = modelo.userFactors.withColumnRenamed("id", "userId")
    return items, usuarios


def factores_numpy(df_items) -> tuple[np.ndarray, np.ndarray]:
    """DataFrame (movieId, features) de Spark o pandas -> (ids, matriz float64)."""
    pdf = df_items if hasattr(df_items, "iloc") else df_items.toPandas()
    ids = pdf["movieId"].to_numpy(dtype=np.int64)
    matriz = np.vstack(pdf["features"].to_numpy()).astype(np.float64) if len(pdf) else \
        np.zeros((0, 0))
    return ids, matriz


# --------------------------------------------------------------------------- #
# Popularidad
# --------------------------------------------------------------------------- #

def popularidad(train, min_votos: int = MIN_VOTOS):
    """Lista de popularidad (movieId, n_votos, media) ordenada por votos.

    Si menos de MIN_CANDIDATOS_POP películas tienen ``min_votos``, se usan todas
    (pasa en la muestra mini). Empates: movieId ascendente (determinista).
    Devuelve (pandas.DataFrame ordenado, min_votos efectivo).
    """
    from pyspark.sql import functions as F

    conteos = (train.groupBy("movieId")
               .agg(F.count("*").alias("n_votos"), F.avg("rating").alias("media"))
               .toPandas())
    efectivo = min_votos
    if (conteos["n_votos"] >= min_votos).sum() < MIN_CANDIDATOS_POP:
        efectivo = 1
    lista = conteos[conteos["n_votos"] >= efectivo]
    lista = lista.sort_values(["n_votos", "movieId"], ascending=[False, True])
    return lista.reset_index(drop=True), efectivo


# --------------------------------------------------------------------------- #
# Top-N sin películas vistas (distribuido)
# --------------------------------------------------------------------------- #

def vistas_pandas(train, usuarios=None):
    """Pares (userId, movieId) del entrenamiento como pandas, ordenados por userId.

    Solo columnas enteras: en 25M son ~20M filas (unos 160 MB), dentro del límite de 6 GB.
    """
    df = train if usuarios is None else train.join(usuarios, "userId", "left_semi")
    pdf = df.select("userId", "movieId").toPandas()
    return pdf.sort_values("userId", kind="stable").reset_index(drop=True)


def top_n_sin_vistas(item_ids: np.ndarray, item_valores: np.ndarray, n: int, vistas,
                     user_ids: np.ndarray | None = None, user_matriz: np.ndarray | None = None,
                     bloque: int = 512):
    """Top-N por usuario sin las películas que ya vio en el entrenamiento.

    - ALS: ``item_valores`` es la matriz de factores de ítems (I × rank) y
      ``user_ids``/``user_matriz`` los factores de usuarios; score = u · v.
    - Popularidad: ``item_valores`` es un vector de puntajes (I,) igual para todos;
      ``user_ids`` son los usuarios a recomendar (``user_matriz`` = None).
    ``vistas``: pandas (userId, movieId) con lo visto en el entrenamiento.

    Se calcula en el driver con numpy por bloques de usuarios (matriz bloque × I).
    Se eligió así porque los workers de Python de Spark no son confiables en Windows;
    ALS, joins y agregaciones sí corren distribuidos en la JVM.
    Devuelve pandas (userId, movieId, rank, score) en formato largo.
    """
    import pandas as pd

    item_ids = np.asarray(item_ids, dtype=np.int64)
    orden_items = np.argsort(item_ids)
    ids_ordenados = item_ids[orden_items]
    if user_ids is None:
        user_ids = np.unique(vistas["userId"].to_numpy())
    user_ids = np.asarray(user_ids, dtype=np.int64)

    # Índices (fila de usuario, columna de ítem) de cada película vista.
    v_users = vistas["userId"].to_numpy(dtype=np.int64)
    v_items = vistas["movieId"].to_numpy(dtype=np.int64)
    pos = np.searchsorted(ids_ordenados, v_items).clip(0, max(len(ids_ordenados) - 1, 0))
    conocido = (len(ids_ordenados) > 0) & (ids_ordenados[pos] == v_items) if len(v_items) else \
        np.zeros(0, dtype=bool)
    v_users, v_cols = v_users[conocido], orden_items[pos[conocido]]
    orden_v = np.argsort(v_users, kind="stable")
    v_users, v_cols = v_users[orden_v], v_cols[orden_v]

    salidas = []
    for ini in range(0, len(user_ids), bloque):
        uids = user_ids[ini:ini + bloque]
        if user_matriz is not None:
            scores = user_matriz[ini:ini + bloque] @ np.asarray(item_valores).T
        else:
            scores = np.tile(np.asarray(item_valores, dtype=np.float64), (len(uids), 1))
        scores = scores.astype(np.float64, copy=False)
        # Marca lo visto con -inf (búsqueda por rangos en los pares ordenados por usuario).
        a = np.searchsorted(v_users, uids, side="left")
        b = np.searchsorted(v_users, uids, side="right")
        for fila, (x, y) in enumerate(zip(a, b)):
            if y > x:
                scores[fila, v_cols[x:y]] = -np.inf
        m = min(n, scores.shape[1])
        if m == 0:
            continue
        top = np.argpartition(-scores, m - 1, axis=1)[:, :m]
        top_scores = np.take_along_axis(scores, top, axis=1)
        # Orden por score descendente y, ante empates, por movieId ascendente
        # (lexsort ordena cada fila por la última clave y desempata con la anterior).
        o = np.lexsort((item_ids[top], -top_scores), axis=-1)
        top = np.take_along_axis(top, o, axis=1)
        top_scores = np.take_along_axis(top_scores, o, axis=1)
        validos = np.isfinite(top_scores)  # sin -inf: lo visto no se recomienda
        salidas.append(pd.DataFrame({
            "userId": np.repeat(uids, m).reshape(len(uids), m)[validos].astype(np.int32),
            "movieId": item_ids[top][validos].astype(np.int32),
            "rank": np.tile(np.arange(1, m + 1), (len(uids), 1))[validos].astype(np.int32),
            "score": top_scores[validos]}))
    if not salidas:
        return pd.DataFrame({"userId": pd.Series(dtype="int32"),
                             "movieId": pd.Series(dtype="int32"),
                             "rank": pd.Series(dtype="int32"),
                             "score": pd.Series(dtype="float64")})
    return pd.concat(salidas, ignore_index=True)


# --------------------------------------------------------------------------- #
# Métricas
# --------------------------------------------------------------------------- #

def precision_ndcg(recomendadas: list, relevantes: set, k: int = K) -> tuple[float, float]:
    """Precision@k y NDCG@k (relevancia binaria) de una lista de recomendaciones."""
    top = list(recomendadas)[:k]
    aciertos = [1.0 if m in relevantes else 0.0 for m in top]
    precision = sum(aciertos) / k
    dcg = sum(a / math.log2(i + 2) for i, a in enumerate(aciertos))
    idcg = sum(1.0 / math.log2(i + 2) for i in range(min(len(relevantes), k)))
    return precision, (dcg / idcg if idcg > 0 else 0.0)


def metricas_ranking(recs, test, k: int = K, relevante_min: float | None = None) -> dict:
    """Promedia Precision@k y NDCG@k sobre usuarios con al menos un relevante en prueba.

    ``recs``: pandas (userId, movieId, rank); ``test``: DataFrame de Spark o pandas.
    ``relevante_min``: umbral de relevancia (por defecto config.RELEVANTE_MIN = 4).
    Si ningún usuario tiene relevantes, ambas métricas valen 0 y usuarios_evaluados = 0.
    """
    umbral = config.RELEVANTE_MIN if relevante_min is None else relevante_min
    if not hasattr(test, "iloc"):
        test = test.select("userId", "movieId", "rating").toPandas()
    rel_pdf = test[test["rating"] >= umbral]
    relevantes: dict[int, set] = {}
    for uid, mid in zip(rel_pdf["userId"].to_numpy(), rel_pdf["movieId"].to_numpy()):
        relevantes.setdefault(int(uid), set()).add(int(mid))
    top: dict[int, list] = {}
    ordenadas = recs[recs["rank"] <= k].sort_values(["userId", "rank"])
    for uid, mid in zip(ordenadas["userId"].to_numpy(), ordenadas["movieId"].to_numpy()):
        top.setdefault(int(uid), []).append(int(mid))
    precisiones, ndcgs = [], []
    for uid, rel in relevantes.items():
        p, nd = precision_ndcg(top.get(uid, []), rel, k)
        precisiones.append(p)
        ndcgs.append(nd)
    if not precisiones:
        return {f"Precision@{k}": 0.0, f"NDCG@{k}": 0.0, "usuarios_evaluados": 0}
    return {f"Precision@{k}": float(np.mean(precisiones)), f"NDCG@{k}": float(np.mean(ndcgs)),
            "usuarios_evaluados": len(precisiones)}


def rmse_als(modelo, test) -> float:
    from pyspark.ml.evaluation import RegressionEvaluator

    pred = modelo.transform(test)
    return float(RegressionEvaluator(metricName="rmse", labelCol="rating",
                                     predictionCol="prediction").evaluate(pred))


def rmse_media_item(train, test) -> float:
    """RMSE de la popularidad: predice la media del ítem en el entrenamiento."""
    from pyspark.sql import functions as F

    medias = train.groupBy("movieId").agg(F.avg("rating").alias("prediction"))
    fila = (test.join(medias, "movieId", "inner")
            .agg(F.sqrt(F.avg((F.col("rating") - F.col("prediction")) ** 2)).alias("rmse"))
            .first())
    return float(fila["rmse"]) if fila and fila["rmse"] is not None else float("nan")


# --------------------------------------------------------------------------- #
# Corrida completa
# --------------------------------------------------------------------------- #

def ejecutar(spark, ratings, corte: str, salida=None, params: dict | None = None,
             desde: str | None = None, fraccion: float | None = None,
             min_votos: int = MIN_VOTOS, n_recs: int = N_RECS, k: int = K,
             guardar: bool = True, log=print, relevante_min: float | None = None) -> dict:
    """Partición temporal -> ALS -> popularidad -> métricas -> artefactos.

    Devuelve el dict de metricas.json (más ``modelo`` y ``particion`` sin serializar).
    """
    inicio = time.perf_counter()
    # Arrow acelera toPandas (en 25M se traen ~20M pares userId/movieId) y no usa
    # workers de Python; si no se puede, Spark vuelve al camino normal.
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    ratings = filtrar_fraccion(ratings, fraccion)
    part = particion_temporal(ratings, corte, desde)
    log(f"Entrenamiento: {part['n_train']:,} ratings antes de {corte}"
        + (f" (desde {desde})" if desde else ""))
    log(f"Prueba: {part['n_test']:,} ratings desde {corte}; descartados "
        f"{part['descartados']:,} de {part['n_test_bruto']:,} por usuario o película "
        "sin entrenamiento (coldStartStrategy='drop')")
    if part["n_train"] == 0:
        raise ValueError("No hay ratings de entrenamiento antes del corte")
    if part["n_test"] == 0:
        raise ValueError("No hay ratings de prueba evaluables después del corte")

    p = {**PARAMS_DEFECTO, **(params or {})}
    t0 = time.perf_counter()
    modelo = entrenar(part["train"], p)
    seg_als = time.perf_counter() - t0
    log(f"ALS entrenado en {seg_als:.1f} s (rank={p['rank']}, regParam={p['regParam']}, "
        f"maxIter={p['maxIter']}, seed={p['seed']})")

    train, test = part["train"], part["test"]
    test_pdf = test.select("userId", "movieId", "rating").toPandas()
    usuarios_test = np.unique(test_pdf["userId"].to_numpy(dtype=np.int64))
    items_df, users_df = factores(modelo)
    ids, matriz = factores_numpy(items_df)
    u_pdf = users_df.toPandas().sort_values("userId").reset_index(drop=True)
    u_ids = u_pdf["userId"].to_numpy(dtype=np.int64)
    u_mat = np.vstack(u_pdf["features"].to_numpy()).astype(np.float64)
    vistas = vistas_pandas(train)

    # ALS: top-k sin vistas de los usuarios con ratings de prueba.
    sel = np.isin(u_ids, usuarios_test)
    recs_eval = top_n_sin_vistas(ids, matriz, k, vistas, u_ids[sel], u_mat[sel])
    umbral = config.RELEVANTE_MIN if relevante_min is None else float(relevante_min)
    m_als = {"RMSE": rmse_als(modelo, test),
             **metricas_ranking(recs_eval, test_pdf, k, umbral)}

    # Popularidad
    pop, efectivo = popularidad(train, min_votos)
    pop_ids = pop["movieId"].to_numpy(dtype=np.int64)
    pop_scores = pop["n_votos"].to_numpy(dtype=np.float64)
    recs_pop = top_n_sin_vistas(pop_ids, pop_scores, k, vistas, usuarios_test)
    m_pop = {"RMSE": rmse_media_item(train, test),
             **metricas_ranking(recs_pop, test_pdf, k, umbral),
             "min_votos": int(efectivo),
             "top10": [int(x) for x in pop_ids[:10]]}
    if m_als["usuarios_evaluados"] == 0:
        log(f"Aviso: ningún usuario tiene ratings de prueba relevantes (>= {umbral:g}); "
            f"Precision@{k} y NDCG@{k} quedan en 0. Prueba con --relevante más bajo.")

    resultado = {
        "als": m_als, "popularidad": m_pop, "corte": corte, "desde": desde,
        "fraccion": fraccion, "n_train": part["n_train"], "n_test": part["n_test"],
        "descartados": part["descartados"], "params": p, "k": k,
        "relevante_min": umbral,
    }
    if guardar and salida is not None:
        salida = Path(salida)
        salida.mkdir(parents=True, exist_ok=True)
        modelo.write().overwrite().save(str(salida / "modelo_als"))
        items_df.write.mode("overwrite").parquet(str(salida / "item_factors.parquet"))
        users_df.write.mode("overwrite").parquet(str(salida / "user_factors.parquet"))
        recs_all = top_n_sin_vistas(ids, matriz, n_recs, vistas, u_ids, u_mat)
        _escribir_parquet_pandas(recs_all, salida / "recs_top20.parquet")
    resultado["segundos"] = round(time.perf_counter() - inicio, 2)
    resultado["segundos_als"] = round(seg_als, 2)
    if guardar and salida is not None:
        (Path(salida) / "metricas.json").write_text(
            json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    resultado["modelo"] = modelo
    resultado["particion"] = part
    return resultado


def _escribir_parquet_pandas(pdf, destino: Path) -> None:
    """Escribe un parquet con pandas reemplazando lo que hubiera (archivo o carpeta)."""
    import shutil

    if destino.is_dir():
        shutil.rmtree(destino)
    elif destino.exists():
        destino.unlink()
    pdf.to_parquet(destino, index=False)


def tabla_metricas(res: dict, k: int = K) -> str:
    """Tabla de texto con RMSE, Precision@k y NDCG@k para ALS y Popularidad."""
    cols = ["RMSE", f"Precision@{k}", f"NDCG@{k}"]
    lineas = [f"{'Modelo':<13}" + "".join(f"{c:>14}" for c in cols),
              "-" * (13 + 14 * len(cols))]
    for nombre, clave in (("ALS", "als"), ("Popularidad", "popularidad")):
        lineas.append(f"{nombre:<13}" + "".join(f"{res[clave][c]:>14.4f}" for c in cols))
    return "\n".join(lineas)
