"""Paquete 7: tipo de espectador de los usuarios nuevos (centroide más cercano).

Se perfila ml-mini con Spark (k=3: acción/thriller, romance/drama y familia) en una
carpeta temporal y se comprueba que:
- vector_crudo (numpy) reproduce las features de vectores_gusto (Spark) usuario a usuario;
- tipo_usuario con 10 calificaciones de onboarding == asignar_tipo(vector_crudo) == el
  centroide más cercano calculado a mano con media/std de tipos.json;
- el tipo cambia al agregar calificaciones que mueven el vector hacia otro centro, y se
  recalcula desde los eventos de estado.db.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd
import pytest

from recomendador import estado, perfiles
from recomendador.datos import cargar_movielens

NUEVO = 162542

# Onboarding de un fan de acción/thriller: 10 calificaciones.
ONBOARDING_ACCION = [(6, 5.0), (1036, 5.0), (2571, 4.5), (589, 5.0), (47, 4.5),
                     (1721, 1.5), (539, 1.0), (597, 1.5), (1307, 2.0), (356, 2.0)]
# Después califica las 15 películas restantes de la muestra (todas nuevas para él):
# romance/drama altas, acción y thriller muy bajas. La muestra solo tiene 25 películas,
# así que hacen falta todas para compensar los 5 de acción del onboarding.
GIRO_ROMANCE = [(318, 5.0), (527, 5.0), (1265, 5.0), (4306, 5.0),
                (32, 0.5), (50, 0.5), (110, 0.5), (260, 0.5), (480, 0.5), (593, 0.5),
                (58559, 0.5), (296, 0.5), (1, 0.5), (364, 3.0), (6377, 3.0)]


@pytest.fixture(scope="module")
def corrida(spark, ruta_mini, tmp_path_factory):
    salida = tmp_path_factory.mktemp("tipos_nuevos")
    tablas = cargar_movielens(ruta_mini, spark,
                              ["ratings", "movies", "genome_tags", "genome_scores"])
    res = perfiles.ejecutar(spark, tablas, salida, k=3, seed=42, log=lambda *_: None)
    tipos = json.loads((salida / "tipos.json").read_text(encoding="utf-8"))
    feats = perfiles.features_tipo(ruta_mini, tipos["rasgos"])
    return res, salida, tipos, feats


def _a_mano(vector, tipos) -> int:
    """Centroide más cercano con el escalado del KMeans (media y std de tipos.json)."""
    media, std = np.array(tipos["media_features"]), np.array(tipos["std_features"])
    x = np.where(std > 0, (vector - media) / np.where(std > 0, std, 1), 0.0)
    dist = {t["cluster"]: float(np.sum((x - np.array([t["centroide"][r]
                                                      for r in tipos["rasgos"]])) ** 2))
            for t in tipos["tipos"]}
    return min(dist, key=dist.get)


def test_vector_crudo_igual_a_las_features_de_spark(corrida, ruta_mini):
    res, _, tipos, feats = corrida
    ratings = pd.read_csv(ruta_mini / "ratings.csv")
    rasgos = tipos["rasgos"]
    tags = [r[4:] for r in rasgos if r.startswith("tag:")]
    assert tags, "la muestra trae genome: debe haber rasgos de tags"
    for fila in res["perfiles"].itertuples():
        mios = ratings[ratings["userId"] == fila.userId]
        v = perfiles.vector_crudo(list(zip(mios["movieId"], mios["rating"])), feats, rasgos)
        pg, pt = json.loads(fila.pesos_generos), json.loads(fila.pesos_tags)
        esperado = ([pg[r[7:]] for r in rasgos if r.startswith("genero:")]
                    + [pt[t] for t in tags] + [np.log1p(fila.n_ratings)])
        np.testing.assert_allclose(v, esperado, atol=1e-5, err_msg=f"usuario {fila.userId}")


def test_usuarios_de_la_muestra_recuperan_su_cluster(corrida, ruta_mini):
    """Con sus propias calificaciones, cada usuario cae en el cluster que le dio el KMeans."""
    res, salida, tipos, feats = corrida
    ratings = pd.read_csv(ruta_mini / "ratings.csv")
    for fila in res["perfiles"].itertuples():
        mios = ratings[ratings["userId"] == fila.userId]
        t = perfiles.tipo_usuario(fila.userId, salida, features=feats,
                                  ratings=list(zip(mios["movieId"], mios["rating"])))
        assert t["cluster"] == fila.cluster, fila.userId


def test_onboarding_de_10_coincide_con_asignar_tipo(corrida, tmp_path):
    _, salida, tipos, feats = corrida
    db = tmp_path / "estado.db"
    con = estado.conectar(db)
    estado.guardar_eventos(con, [{"userId": NUEVO, "movieId": m, "rating": r,
                                  "timestamp": 1_700_000_000 + i}
                                 for i, (m, r) in enumerate(ONBOARDING_ACCION)])
    con.close()

    t = perfiles.tipo_usuario(NUEVO, salida, estado_db=db, features=feats)
    vector = perfiles.vector_crudo(ONBOARDING_ACCION, feats, tipos["rasgos"])
    assert t["n_ratings"] == 10
    assert t == {**perfiles.asignar_tipo(list(vector), salida), "n_ratings": 10}
    assert t["cluster"] == _a_mano(vector, tipos)
    # Es el tipo de los fans de acción de la muestra (usuario 1).
    assert t["cluster"] == perfiles.perfil_usuario(1, salida)["cluster"]
    assert t["tipo"] == next(x["nombre"] for x in tipos["tipos"] if x["cluster"] == t["cluster"])


def test_tipo_cambia_al_calificar_mas(corrida, tmp_path):
    _, salida, tipos, feats = corrida
    db = tmp_path / "estado.db"
    con = estado.conectar(db)
    estado.guardar_eventos(con, [{"userId": NUEVO, "movieId": m, "rating": r,
                                  "timestamp": 1_700_000_000 + i}
                                 for i, (m, r) in enumerate(ONBOARDING_ACCION)])
    antes = perfiles.tipo_usuario(NUEVO, salida, estado_db=db, features=feats)
    estado.guardar_eventos(con, [{"userId": NUEVO, "movieId": m, "rating": r,
                                  "timestamp": 1_700_001_000 + i}
                                 for i, (m, r) in enumerate(GIRO_ROMANCE)])
    con.close()
    despues = perfiles.tipo_usuario(NUEVO, salida, estado_db=db, features=feats)
    assert despues["n_ratings"] == 25
    assert despues["cluster"] != antes["cluster"]
    todos = ONBOARDING_ACCION + GIRO_ROMANCE
    assert despues["cluster"] == _a_mano(
        perfiles.vector_crudo(todos, feats, tipos["rasgos"]), tipos)
    # Se mueve hacia el centro de los fans de romance/drama (usuario 2).
    assert despues["cluster"] == perfiles.perfil_usuario(2, salida)["cluster"]


def test_sin_calificaciones_no_hay_tipo(corrida, tmp_path):
    _, salida, _, feats = corrida
    assert perfiles.tipo_usuario(NUEVO + 1, salida, estado_db=tmp_path / "e.db",
                                 features=feats) is None


def test_pelicula_sin_genome_no_suma_al_denominador_de_tags():
    rasgos = ["genero:Action", "tag:dark", "actividad"]
    feats = {1: (frozenset({"Action"}), {"dark": 0.8}), 2: (frozenset({"Drama"}), None)}
    v = perfiles.vector_crudo([(1, 5.0), (2, 3.0)], feats, rasgos)
    # media 4: pesos +1 y -1; suma_abs 2; suma_abs_genome 1 (solo la película 1).
    np.testing.assert_allclose(v, [0.5, 0.8, np.log1p(2)])
