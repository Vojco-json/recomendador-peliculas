"""Capa batch de la app (recomendador/precalculo.py) sobre la muestra mini.

Valores conocidos (muestras/ml-mini/LEEME.md y muestras/imdb-mini/LEEME.md):
- 210 ratings antes del corte 2016-01-01 (de 300); Toy Story: 15 votos, promedio 3.6.
- The Dark Knight (58559 -> tt0468569) lo dirige Christopher Nolan.
- Breaking Bad (tt0903747) está en los TSV de IMDb pero no en links.csv.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

import numpy as np
import pandas as pd
import pytest

from base_mini import CORTE, crear_base_mini
from recomendador import config, llm, precalculo
from recomendador.datos import fecha_a_epoch

IMDB_MINI = config.RAIZ / "muestras" / "imdb-mini"


@pytest.fixture(scope="module")
def base(spark, ruta_mini, tmp_path_factory):
    """Artefactos base (ALS + perfiles) y los 4 parquets de la app en una carpeta temporal."""
    salida = crear_base_mini(spark, ruta_mini, tmp_path_factory.mktemp("precalculo"))
    rutas = precalculo.ejecutar(spark, ruta_mini, salida, CORTE, imdb_dir=IMDB_MINI,
                                log=lambda *_: None)
    return salida, rutas


def _leer(rutas, nombre) -> pd.DataFrame:
    return pd.read_parquet(rutas[nombre])


def test_crea_los_cuatro_parquets(base):
    salida, rutas = base
    assert set(rutas) == {"peliculas", "personas", "populares_por_tipo", "vecinos"}
    for nombre, ruta in rutas.items():
        assert ruta.parent == salida / "app"
        assert ruta.name == precalculo.NOMBRES[nombre]
        assert len(_leer(rutas, nombre)) > 0, nombre


def test_precalculo_no_importa_pandas():
    fuente = (config.RAIZ / "recomendador" / "precalculo.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+pandas", fuente, re.M)
    assert "toPandas" not in fuente and "import pandas" not in fuente
    assert "pyspark.ml" in fuente and "pyspark.sql" in fuente


def test_peliculas_valores_conocidos_antes_del_corte(base, ruta_mini):
    _, rutas = base
    p = _leer(rutas, "peliculas").set_index("movieId")
    assert len(p) == 25
    assert p["votos"].sum() == 210                      # 300 ratings, 90 desde el corte
    toy = p.loc[1]
    assert toy["votos"] == 15 and toy["promedio"] == pytest.approx(3.6)
    assert toy["titulo"] == "Toy Story" and toy["anio"] == 1995 and toy["duracion_min"] == 81
    assert toy["imdb_rating"] == pytest.approx(8.3) and toy["tmdbId"] == 862
    assert list(toy["generos"])[:2] == ["Adventure", "Animation"]
    assert "pixar" in list(toy["tags_usuarios"]) and len(toy["tags"]) == precalculo.N_TAGS
    assert p.loc[50, "titulo"] == "The Usual Suspects"

    # Histograma y promedio calculados aparte con pandas, solo antes del corte.
    r = pd.read_csv(ruta_mini / "ratings.csv")
    previos = r[r["timestamp"] < fecha_a_epoch(CORTE)]
    for mid, grupo in previos.groupby("movieId"):
        esperado = [int((grupo["rating"] == v).sum()) for v in precalculo.CASILLAS]
        assert list(p.loc[mid, "histograma"]) == esperado, mid
        assert p.loc[mid, "promedio"] == pytest.approx(grupo["rating"].mean(), abs=1e-3)
        assert p.loc[mid, "votos"] == len(grupo)


def test_corte_excluye_timestamps_iguales_o_posteriores(spark):
    """Un rating justo en el corte (o después) no cuenta en votos, promedio ni histograma."""
    epoch = fecha_a_epoch(CORTE)
    filas_sql = ", ".join(f"({u}, {m}, CAST({r} AS DOUBLE), CAST({t} AS BIGINT))" for u, m, r, t in
                          [(1, 7, 4.0, epoch - 1), (2, 7, 2.0, epoch - 100), (3, 7, 0.5, epoch),
                           (4, 7, 5.0, epoch + 10), (5, 8, 3.0, epoch)])
    df = spark.sql(f"SELECT * FROM VALUES {filas_sql} AS t(userId, movieId, rating, timestamp)")
    filas = {f["movieId"]: f for f in precalculo.estadisticas_ratings(df, CORTE).collect()}
    assert set(filas) == {7}                             # la 8 solo tiene ratings del corte
    assert filas[7]["votos"] == 2 and filas[7]["promedio"] == pytest.approx(3.0)
    assert list(filas[7]["histograma"]) == [0, 0, 0, 1, 0, 0, 0, 1, 0, 0]


def test_populares_por_tipo_usa_cluster_y_corte(base, ruta_mini):
    salida, rutas = base
    q = _leer(rutas, "populares_por_tipo")
    perf = pd.read_parquet(salida / "perfiles.parquet")[["userId", "cluster", "tipo"]]
    # Mismos tipos (cluster y nombre) que perfilar_usuarios.
    assert set(map(tuple, q[["cluster", "tipo"]].drop_duplicates().values)) == \
        set(map(tuple, perf[["cluster", "tipo"]].drop_duplicates().values))
    # Conteos esperados (pandas) solo con ratings anteriores al corte.
    r = pd.read_csv(ruta_mini / "ratings.csv")
    r = r[r["timestamp"] < fecha_a_epoch(CORTE)].merge(perf, on="userId")
    esperado = r.groupby(["cluster", "movieId"])["rating"].agg(["count", "mean"])
    assert q["votos"].sum() == 210
    for _, fila in q.iterrows():
        e = esperado.loc[(fila["cluster"], fila["movieId"])]
        assert fila["votos"] == e["count"]
        assert fila["promedio"] == pytest.approx(e["mean"], abs=1e-3)
    # Toy Story entre los 3 primeros del tipo de los usuarios 12-15 (familia).
    familia = int(perf.loc[perf["userId"] == 12, "cluster"].iloc[0])
    top = q[q["cluster"] == familia].sort_values("rank")["movieId"].head(4).tolist()
    assert 1 in top
    assert (q.groupby("cluster")["rank"].apply(lambda s: sorted(s) == list(range(1, len(s) + 1)))
            .all())


def test_vecinos_lsh(base, spark):
    salida, rutas = base
    v = _leer(rutas, "vecinos")
    assert (v["movieId"] != v["vecino"]).all()            # nunca la propia película
    assert v.groupby("movieId").size().max() <= precalculo.N_VECINOS
    assert not v.duplicated(["movieId", "vecino"]).any()
    assert v.groupby("movieId")["distancia"].apply(lambda s: s.is_monotonic_increasing).all()
    # La similitud coincide con el coseno de los factores ALS.
    items = pd.read_parquet(salida / "item_factors.parquet").set_index("movieId")
    fila = v.iloc[0]
    a = np.asarray(items.loc[fila["movieId"], "features"], dtype=float)
    b = np.asarray(items.loc[fila["vecino"], "features"], dtype=float)
    coseno = a @ b / (np.linalg.norm(a) * np.linalg.norm(b))
    assert fila["similitud"] == pytest.approx(coseno, abs=1e-3)
    # Con n=3 hay a lo sumo 3 por película.
    v3 = precalculo.construir_vecinos(spark, salida / "item_factors.parquet", n=3).toPandas()
    assert v3.groupby("movieId").size().max() <= 3 and (v3["movieId"] != v3["vecino"]).all()


def test_vecinos_respaldo_exacto_cuando_lsh_no_alcanza(base, spark):
    """Con baldes y umbral diminutos el LSH casi no encuentra pares (como puede pasar en
    25M con umbral 1): el respaldo exacto completa a todas las películas."""
    salida, _ = base
    ruta = salida / "item_factors.parquet"
    total = len(pd.read_parquet(ruta))
    sin = precalculo.construir_vecinos(spark, ruta, n=5, bucket_length=1e-4, umbral=1e-3,
                                       respaldo=False).toPandas()
    con = precalculo.construir_vecinos(spark, ruta, n=5, bucket_length=1e-4, umbral=1e-3).toPandas()
    assert sin["movieId"].nunique() < total                   # el LSH solo deja huecos
    por_peli = con.groupby("movieId").size()
    assert len(por_peli) == total and (por_peli == min(5, total - 1)).all()
    assert (con["movieId"] != con["vecino"]).all()
    assert not con.duplicated(["movieId", "vecino"]).any()
    assert set(con["metodo"]) <= {"lsh", "exacto"} and (con["metodo"] == "exacto").any()
    # El respaldo es exacto: coincide con el top-5 por coseno calculado con numpy.
    items = pd.read_parquet(ruta).set_index("movieId")
    m = np.stack([np.asarray(f, dtype=float) for f in items["features"]])
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    sim = m @ m.T
    ids = list(items.index)
    i0 = 0
    orden = [ids[j] for j in np.argsort(-sim[i0]) if j != i0][:5]
    obtenido = con[con["movieId"] == ids[i0]].sort_values("rank")
    assert set(obtenido["vecino"]) == set(orden)
    # Resumen de cobertura que imprime el script.
    destino = salida / "vecinos_respaldo.parquet"
    spark.createDataFrame(con).write.mode("overwrite").parquet(str(destino))
    c = precalculo.cobertura_vecinos(spark, destino, ruta, n=5)
    assert c["peliculas"] == total and c["completas"] == total and c["sin_vecinos"] == 0


def test_personas_nolan_y_filtro_por_links(base, spark, ruta_mini):
    _, rutas = base
    pe = _leer(rutas, "personas")
    nolan = pe[pe["nombre_norm"] == "christopher nolan"]
    assert list(zip(nolan["movieId"], nolan["rol"])) == [(58559, "director")]
    assert set(pe.loc[pe["nombre"] == "Tom Hanks", "movieId"]) == {1, 356}
    # Nombre sin tildes para buscar.
    assert "penelope cruz sanchez" in set(pe["nombre_norm"])
    # Breaking Bad (fuera de links.csv) no aparece: ni su tconst ni su reparto.
    assert "tt0903747" not in set(pe["tconst"])
    assert "Bryan Cranston" not in set(pe["nombre"]) and "Vince Gilligan" not in set(pe["nombre"])
    links = pd.read_csv(ruta_mini / "links.csv")
    assert set(pe["movieId"]) <= set(links["movieId"])
    # El filtro por los tconst de links.csv ocurre antes del cruce (semi join en el plan).
    links_df = spark.read.option("header", True).schema(
        "movieId INT, imdbId STRING, tmdbId INT").csv(str(ruta_mini / "links.csv"))
    plan = precalculo.construir_personas(spark, links_df, IMDB_MINI)._jdf \
        .queryExecution().optimizedPlan().toString()
    assert "LeftSemi" in plan


def test_imdb_mini_trae_director_conocido():
    crew = pd.read_csv(IMDB_MINI / "title.crew.tsv", sep="\t")
    nombres = pd.read_csv(IMDB_MINI / "name.basics.tsv", sep="\t")
    assert "nm0634240" in crew.loc[crew["tconst"] == "tt0468569", "directors"].iloc[0]
    assert "Christopher Nolan" in set(nombres["primaryName"])


def test_sin_archivos_de_personas_devuelve_tabla_vacia(spark, ruta_mini, tmp_path):
    links_df = spark.read.option("header", True).schema(
        "movieId INT, imdbId STRING, tmdbId INT").csv(str(ruta_mini / "links.csv"))
    vacia = precalculo.construir_personas(spark, links_df, tmp_path)
    assert vacia.count() == 0 and "nombre_norm" in vacia.columns


def test_llm_sin_codigo_muerto():
    for simbolo in ("_validar", "completar_args", "como_dict", "_como_dict",
                    "MAX_RONDAS_HERRAMIENTAS"):
        assert not hasattr(llm, simbolo), simbolo
    fuente = (config.RAIZ / "recomendador" / "llm.py").read_text(encoding="utf-8")
    for simbolo in ("_validar", "completar_args", "como_dict", "MAX_RONDAS_HERRAMIENTAS"):
        assert simbolo not in fuente, simbolo


@pytest.mark.lento
def test_script_mini_menos_de_3_minutos(base, tmp_path):
    """C1: scripts/precalcular_app.py --perfil mini termina en < 3 min y crea los 4 parquets."""
    import conftest

    conftest._armar_temporizador(240)     # el script levanta su propia JVM
    salida, _ = base
    destino = tmp_path / "perfil"
    destino.mkdir()
    for nombre in ("perfiles.parquet", "item_factors.parquet"):
        (destino / nombre).write_bytes((salida / nombre).read_bytes())
    inicio = time.monotonic()
    r = subprocess.run([sys.executable, str(config.RAIZ / "scripts" / "precalcular_app.py"),
                        "--perfil", "mini", "--artefactos", str(destino)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace",
                       timeout=180, cwd=config.RAIZ)
    duracion = time.monotonic() - inicio
    assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
    assert duracion < 180
    for nombre in precalculo.NOMBRES.values():
        assert (destino / "app" / nombre).is_dir() or (destino / "app" / nombre).is_file()
        assert len(pd.read_parquet(destino / "app" / nombre)) > 0, nombre
