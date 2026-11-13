"""Pruebas del paquete 4: perfiles de gusto y tipos de espectador (KMeans).

Usan la SparkSession compartida (conftest.py) y la muestra ml-mini (valores
conocidos en muestras/ml-mini/LEEME.md). El perfilado con k=3 se corre una sola
vez por módulo, con artefactos en una carpeta temporal.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from recomendador import perfiles
from recomendador.datos import cargar_movielens


@pytest.fixture(scope="module")
def corrida(spark, ruta_mini, tmp_path_factory):
    salida = tmp_path_factory.mktemp("mini_perfiles")
    tablas = cargar_movielens(ruta_mini, spark,
                              ["ratings", "movies", "genome_tags", "genome_scores"])
    res = perfiles.ejecutar(spark, tablas, salida, k=3, seed=42, log=lambda *_: None)
    return res, salida


# --------------------------------------------------------------------------- #
# Vectores de gusto
# --------------------------------------------------------------------------- #

def test_usuario1_action_en_top2(corrida):
    _, salida = corrida
    p = perfiles.perfil_usuario(1, salida)
    assert "Action" in p["top_generos"][:2], p["top_generos"]
    assert set(p["top_generos"][:2]) == {"Action", "Thriller"}


def test_usuario2_romance_o_drama_en_top2(corrida):
    _, salida = corrida
    p = perfiles.perfil_usuario(2, salida)
    assert {"Romance", "Drama"} & set(p["top_generos"][:2]), p["top_generos"]
    assert p["top_generos"][0] == "Romance"


def test_pesos_coinciden_con_leeme(corrida):
    """Usuario 1: Thriller 6.625 y Action 6.5 (suma cruda de rating - media)."""
    _, salida = corrida
    p = perfiles.perfil_usuario(1, salida)
    assert p["media"] == pytest.approx(3.875)
    crudo = {g: v * p["suma_abs"] for g, v in p["pesos_generos"].items()}
    assert crudo["Thriller"] == pytest.approx(6.625, abs=1e-3)
    assert crudo["Action"] == pytest.approx(6.5, abs=1e-3)


def test_vector_generos_20_y_normalizado(corrida):
    res, _ = corrida
    pdf = res["perfiles"]
    for texto in pdf["pesos_generos"]:
        pesos = json.loads(texto)
        assert len(pesos) == 20 and set(pesos) == set(perfiles.GENEROS)
        assert all(-1.0 - 1e-9 <= v <= 1.0 + 1e-9 for v in pesos.values())


def test_vector_genome_ponderado_por_relevancia(corrida):
    """Los tags del genome se usan (10 en la muestra) y el usuario 1 prefiere acción/violencia."""
    _, salida = corrida
    p1 = perfiles.perfil_usuario(1, salida)
    assert len(p1["pesos_tags"]) == 10
    assert p1["pesos_tags"]["violence"] > 0 > p1["pesos_tags"]["romance"]
    p12 = perfiles.perfil_usuario(12, salida)
    assert p12["pesos_tags"]["animation"] > 0


# --------------------------------------------------------------------------- #
# KMeans y nombrado
# --------------------------------------------------------------------------- #

def test_usuarios_1_y_2_en_clusters_distintos(corrida):
    _, salida = corrida
    assert perfiles.perfil_usuario(1, salida)["cluster"] != perfiles.perfil_usuario(2, salida)["cluster"]


def test_grupos_conocidos_de_la_muestra(corrida):
    """Los 3 grupos del LEEME quedan cada uno en su propio cluster."""
    res, _ = corrida
    cl = res["perfiles"].set_index("userId")["cluster"]
    grupos = [[1, 3, 5, 7, 9, 11], [2, 4, 6, 8, 10], [12, 13, 14, 15]]
    etiquetas = [set(cl[g]) for g in grupos]
    assert all(len(e) == 1 for e in etiquetas)
    assert len({e.pop() for e in etiquetas}) == 3


def test_nombres_unicos_y_tamanos(corrida):
    res, salida = corrida
    tipos = json.loads((salida / "tipos.json").read_text(encoding="utf-8"))
    nombres = [t["nombre"] for t in tipos["tipos"]]
    assert len(nombres) == 3 and len(set(nombres)) == 3
    assert sum(t["tamaño"] for t in tipos["tipos"]) == 15
    for t in tipos["tipos"]:
        assert len(t["rasgos"]) == 2
        assert all(r["desviacion"] > 0 for r in t["rasgos"])
        assert set(t["centroide"]) == set(tipos["rasgos"])
    assert tipos["k"] == 3 and tipos["seed"] == 42


def test_nombrado_no_repite_aunque_los_centroides_coincidan():
    """Dos centroides idénticos reciben nombres distintos (diccionario en español)."""
    rasgos = ["genero:Action", "genero:Thriller", "genero:Comedy", "tag:action", "actividad"]
    centros = np.array([[2.0, 1.5, 0.5, 1.9, -1.0],
                        [2.0, 1.5, 0.5, 1.9, -1.0],
                        [-1.0, -1.0, 1.0, -1.0, 2.0]])
    nombres = perfiles.nombrar_clusters(centros, rasgos, [10, 5, 3])
    lista = [nombres[c]["nombre"] for c in range(3)]
    assert len(set(lista)) == 3
    assert lista[0] == "Acción y suspenso"     # 'tag:action' se omite por repetir «acción»
    assert lista[2] == "Cinéfilo de comedia"


def test_kmeans_reproducible(spark, ruta_mini, corrida):
    """Con la misma semilla, la asignación es la misma."""
    res, _ = corrida
    tablas = cargar_movielens(ruta_mini, spark,
                              ["ratings", "movies", "genome_tags", "genome_scores"])
    otra = perfiles.ejecutar(spark, tablas, None, k=3, seed=42, log=lambda *_: None)
    assert list(otra["perfiles"]["cluster"]) == list(res["perfiles"]["cluster"])


# --------------------------------------------------------------------------- #
# Salidas, consulta y actualización incremental
# --------------------------------------------------------------------------- #

def test_salidas_parquet_y_json(corrida):
    _, salida = corrida
    pdf = pd.read_parquet(salida / "perfiles.parquet")
    for col in ("userId", "top_generos", "top_tags", "cluster", "tipo"):
        assert col in pdf.columns
    assert len(pdf) == 15 and pdf["userId"].is_unique
    tipos = json.loads((salida / "tipos.json").read_text(encoding="utf-8"))
    assert set(pdf["tipo"]) == {t["nombre"] for t in tipos["tipos"]}


def test_perfil_usuario_legible_e_inexistente(corrida):
    _, salida = corrida
    p = perfiles.perfil_usuario(2, salida)
    for clave in ("tipo", "top_generos", "top_generos_es", "top_tags", "pesos_generos",
                  "n_ratings", "descripcion_tipo"):
        assert clave in p
    assert p["n_ratings"] == 20
    assert "romance" in p["top_generos_es"]
    json.dumps(p, ensure_ascii=False)  # serializable (lo usa el LLM)
    assert perfiles.perfil_usuario(999999, salida) is None


def test_actualizacion_incremental_sube_genero(corrida, ruta_mini):
    _, salida = corrida
    p = perfiles.perfil_usuario(2, salida)
    mapa = perfiles.generos_por_pelicula(ruta_mini)
    # El usuario 2 (romance/drama) califica Heat (Action|Crime|Thriller) con 5.
    nuevo = perfiles.actualizar_perfil_generos(p, [{"movieId": 6, "rating": 5.0}], mapa)
    assert nuevo["pesos_generos"]["Action"] > p["pesos_generos"]["Action"]
    assert nuevo["pesos_generos"]["Thriller"] > p["pesos_generos"]["Thriller"]
    assert nuevo["n_ratings"] == 21
    assert p["n_ratings"] == 20  # el original no se modifica
    # Con el género dentro del evento (como llegaría desde el streaming) y en tupla.
    otro = perfiles.actualizar_perfil_generos(p, [{"movieId": 6, "rating": 5.0,
                                                   "genres": "Action|Crime|Thriller"}])
    assert otro["pesos_generos"]["Action"] == pytest.approx(nuevo["pesos_generos"]["Action"])
    tupla = perfiles.actualizar_perfil_generos(p, [(6, 5.0)], mapa)
    assert tupla["pesos_generos"]["Action"] == pytest.approx(nuevo["pesos_generos"]["Action"])


def test_actualizacion_usuario_nuevo(ruta_mini):
    mapa = perfiles.generos_por_pelicula(ruta_mini)
    p = perfiles.actualizar_perfil_generos(None, [(1, 5.0), (6, 2.0)], mapa, userId=1000001)
    assert p["userId"] == 1000001 and p["n_ratings"] == 2
    assert p["top_generos"][0] in {"Adventure", "Animation", "Children", "Comedy", "Fantasy"}
    assert p["pesos_generos"]["Action"] < 0
