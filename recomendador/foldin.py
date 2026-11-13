"""Fold-in: recomendaciones al instante para usuarios nuevos o activos, sin reentrenar ALS.

Con los factores de ítems V fijos, el vector del usuario se obtiene resolviendo el
mismo problema de mínimos cuadrados regularizado que resuelve ALS en cada paso
(regularización ponderada por el número de ratings, como MLlib):

    u = (V_rᵀ V_r + reg · n_r · I)⁻¹ V_rᵀ r

donde V_r son los factores de las películas que calificó y r sus ratings.
El puntaje de cada película es V · u y se excluyen las ya vistas.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

REG_DEFECTO = 0.1


def cargar_item_factors(origen) -> tuple[np.ndarray, np.ndarray]:
    """Normaliza los factores de ítems a (ids, matriz).

    ``origen`` puede ser:
    - una ruta a ``item_factors.parquet`` (carpeta escrita por Spark o archivo);
    - un DataFrame de pandas o Spark con columnas (movieId, features);
    - una tupla (ids, matriz) ya preparada.
    """
    if isinstance(origen, tuple) and len(origen) == 2:
        ids, matriz = origen
        return np.asarray(ids, dtype=np.int64), np.asarray(matriz, dtype=np.float64)
    if isinstance(origen, (str, Path)):
        import pandas as pd

        origen = pd.read_parquet(Path(origen))
    if not hasattr(origen, "iloc"):  # DataFrame de Spark
        origen = origen.toPandas()
    col_id = "movieId" if "movieId" in origen.columns else "id"
    ids = origen[col_id].to_numpy(dtype=np.int64)
    matriz = np.vstack(origen["features"].to_numpy()).astype(np.float64)
    return ids, matriz


def vector_usuario(ids: np.ndarray, matriz: np.ndarray, ratings, reg: float = REG_DEFECTO):
    """Resuelve el vector latente del usuario. Devuelve None si no hay películas conocidas."""
    indice = {int(m): i for i, m in enumerate(ids)}
    filas, valores = [], []
    for movie_id, rating in ratings:
        i = indice.get(int(movie_id))
        if i is not None:
            filas.append(i)
            valores.append(float(rating))
    if not filas:
        return None
    v = matriz[filas]
    r = np.asarray(valores)
    a = v.T @ v + reg * len(filas) * np.eye(matriz.shape[1])
    return np.linalg.solve(a, v.T @ r)


def recomendar_foldin(item_factors, ratings, n: int = 10, excluir=None,
                      reg: float = REG_DEFECTO) -> list[tuple[int, float]]:
    """Top-n de películas para un usuario a partir de sus ratings [(movieId, rating)].

    item_factors: ver ``cargar_item_factors`` (ruta, DataFrame o (ids, matriz)).
    excluir:      ids adicionales a excluir (además de las películas calificadas).
    reg:          regularización (la misma regParam de ALS).
    Devuelve [(movieId, score)] ordenado de mayor a menor, sin películas vistas.
    Si ninguna película calificada tiene factores, devuelve [].
    """
    ids, matriz = cargar_item_factors(item_factors)
    ratings = list(ratings)
    u = vector_usuario(ids, matriz, ratings, reg)
    if u is None or n <= 0:
        return []
    scores = matriz @ u
    vistas = {int(m) for m, _ in ratings} | {int(m) for m in (excluir or [])}
    mascara = np.isin(ids, np.fromiter(vistas, dtype=np.int64, count=len(vistas)))
    scores[mascara] = -np.inf
    m = min(n, int(np.isfinite(scores).sum()))
    if m == 0:
        return []
    top = np.argpartition(-scores, m - 1)[:m]
    top = top[np.lexsort((ids[top], -scores[top]))]
    return [(int(ids[j]), float(scores[j])) for j in top]
