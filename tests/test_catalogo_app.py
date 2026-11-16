"""Capa de lectura de la app (recomendador/catalogo_app.py) sobre la muestra mini.

Los parquets se generan una vez con precalculo.ejecutar (Spark) en una carpeta
temporal; catalogo_app solo los lee con pandas. Valores conocidos de la muestra:
The Dark Knight (58559) lo dirige Christopher Nolan; Tom Hanks actúa en 1 y 356;
Toy Story (1): 1995, 81 min, IMDb 8.3, 15 votos antes del corte, promedio 3.6.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time

import pandas as pd
import pytest

from base_mini import CORTE, crear_base_mini
from recomendador import catalogo_app, config, estado, perfiles, precalculo
from recomendador.datos import fecha_a_epoch

IMDB_MINI = config.RAIZ / "muestras" / "imdb-mini"
NUEVO = 162542
ONBOARDING_ACCION = [(6, 5.0), (1036, 5.0), (2571, 4.5), (589, 5.0), (47, 4.5),
                     (1721, 1.5), (539, 1.0), (597, 1.5), (1307, 2.0), (356, 2.0)]
GIRO_ROMANCE = [(318, 5.0), (527, 5.0), (1265, 5.0), (4306, 5.0),
                (32, 0.5), (50, 0.5), (110, 0.5), (260, 0.5), (480, 0.5), (593, 0.5),
                (58559, 0.5), (296, 0.5), (1, 0.5), (364, 3.0), (6377, 3.0)]


@pytest.fixture(scope="module")
def base(spark, ruta_mini, tmp_path_factory):
    salida = crear_base_mini(spark, ruta_mini, tmp_path_factory.mktemp("catalogo"))
    precalculo.ejecutar(spark, ruta_mini, salida, CORTE, imdb_dir=IMDB_MINI,
                        log=lambda *_: None)
    return salida


@pytest.fixture
def cat(base, ruta_mini, tmp_path):
    """Catálogo con un estado.db vacío propio de cada prueba."""
    return catalogo_app.Catalogo(artefactos=base, datos=ruta_mini,
                                 estado_db=tmp_path / "estado.db", corte=CORTE)


def _eventos(db, userId, pares, t0=1_700_000_000):
    con = estado.conectar(db)
    estado.guardar_eventos(con, [{"userId": userId, "movieId": m, "rating": r,
                                  "timestamp": t0 + i} for i, (m, r) in enumerate(pares)])
    con.close()


def test_no_importa_pyspark():
    """C1: importar catalogo_app (y tmdb) no carga pyspark (proceso limpio)."""
    codigo = ("import sys; import recomendador.catalogo_app, recomendador.tmdb; "
              "m = [x for x in sys.modules if x.startswith('pyspark')]; "
              "print(m); sys.exit(1 if m else 0)")
    r = subprocess.run([sys.executable, "-c", codigo], cwd=config.RAIZ,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    fuente = (config.RAIZ / "recomendador" / "catalogo_app.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+pyspark", fuente, re.M)


def test_buscar_por_persona_y_titulo_sin_tildes(cat):
    """C2: «nolan» y «NOLÁN» encuentran sus películas; títulos con tildes y mayúsculas."""
    for q in ("nolan", "NOLÁN", "Christopher  Nolán"):
        r = cat.buscar(q)
        assert [x["movieId"] for x in r] == [58559], q
        assert r[0]["coincidencia"] == "director" and r[0]["persona"] == "Christopher Nolan"
    assert {x["movieId"] for x in cat.buscar("tom hanks")} == {1, 356}
    assert cat.buscar("tom hanks")[0]["coincidencia"] == "actor"
    assert cat.buscar("TÓY STÓRY")[0]["movieId"] == 1
    assert cat.buscar("toy story")[0]["coincidencia"] == "título"
    assert cat.buscar("mátrix")[0]["movieId"] == 2571
    assert cat.buscar("suspects usual")[0]["movieId"] == 50     # palabras en otro orden
    assert cat.buscar("Toi Stori")[0]["movieId"] == 1           # error de tipeo
    assert cat.buscar("") == [] and cat.buscar("zzzqqq") == []


def test_ficha_completa_y_resenas_ocultas(cat, tmp_path):
    """C3: año, géneros, duración, IMDb, director, reparto, histograma, tags y reseñas."""
    con = estado.conectar(tmp_path / "estado.db")
    estado.guardar_resenas(con, [
        {"uid": "a", "userId": 7, "movieId": 1, "texto": "Una joya", "timestamp": 10},
        {"uid": "b", "userId": 8, "movieId": 1, "texto": "Spam ofensivo", "timestamp": 20}])
    oculta = next(r["id"] for r in estado.listar_resenas(con) if r["uid"] == "b")
    estado.marcar_oculta(con, oculta)
    con.close()

    f = cat.ficha(1)
    assert f["titulo"] == "Toy Story" and f["anio"] == 1995 and f["duracion_min"] == 81
    assert f["generos"][:2] == ["Adventure", "Animation"] and f["generos_es"]
    assert f["imdb_rating"] == pytest.approx(8.3)
    assert "Tom Hanks" in f["reparto"] and f["director"]
    assert len(f["histograma"]) == 10 and sum(f["histograma"]) == f["votos"] == 15
    assert f["promedio"] == pytest.approx(3.6)
    assert len(f["tags"]) == precalculo.N_TAGS
    assert [r["texto"] for r in f["resenas"]] == ["Una joya"]           # la oculta no sale
    assert f["resenas"][0]["autor"] == "Usuario 7"
    assert "Christopher Nolan" in cat.ficha(58559)["directores"]
    assert cat.ficha(999999) is None


def test_filas_inicio_usuario_de_movielens(cat, ruta_mini):
    filas = {f["clave"]: f for f in cat.filas_inicio(1)}
    assert set(filas) == {"para_ti", "populares_tipo", "porque_te_gusto"}
    vistas = set(cat.ratings_usuario(1))
    for f in filas.values():
        ids = [p["movieId"] for p in f["peliculas"]]
        assert not (set(ids) & vistas), f["clave"]
    assert filas["para_ti"]["peliculas"] and filas["porque_te_gusto"]["peliculas"]
    # Populares: el cluster de perfiles.parquet y el orden de populares_por_tipo (en la
    # muestra de 25 películas el usuario 1 ya vio casi todas las de su tipo).
    p = perfiles.perfil_usuario(1, cat.artefactos)
    assert filas["populares_tipo"]["cluster"] == p["cluster"]
    pop = pd.read_parquet(cat.artefactos / "app" / "populares_por_tipo.parquet")
    assert [x["movieId"] for x in filas["populares_tipo"]["peliculas"]] == \
        [m for m in pop[pop["cluster"] == p["cluster"]].sort_values("rank")["movieId"]
         if m not in vistas][:catalogo_app.N_FILA]
    # Porque te gustó X: X calificada >= 4 y la fila son sus vecinos LSH.
    semilla = filas["porque_te_gusto"]["semilla"]
    assert cat.ratings_usuario(1)[semilla] >= 4
    assert filas["porque_te_gusto"]["titulo"] == f"Porque te gustó {cat.ficha(semilla)['titulo']}"
    v = pd.read_parquet(cat.artefactos / "app" / "vecinos.parquet")
    esperados = v[v["movieId"] == semilla].sort_values("rank")["vecino"].tolist()
    assert [x["movieId"] for x in filas["porque_te_gusto"]["peliculas"]] == \
        [m for m in esperados if m not in vistas]
    # Sin usuario: solo 'Para ti' con las más votadas.
    anon = cat.filas_inicio(None)
    assert [f["clave"] for f in anon] == ["para_ti"] and anon[0]["peliculas"]


def test_populares_como_tu_usa_tipo_usuario_del_nuevo(cat, ruta_mini, tmp_path):
    """C4: la fila del usuario nuevo sigue a tipo_usuario y cambia al calificar más."""
    db = tmp_path / "estado.db"
    pop = pd.read_parquet(cat.artefactos / "app" / "populares_por_tipo.parquet")
    for lote in (ONBOARDING_ACCION, GIRO_ROMANCE[:4]):
        _eventos(db, NUEVO, lote, t0=1_700_000_000 + 1000 * (lote is not ONBOARDING_ACCION))
        t = perfiles.tipo_usuario(NUEVO, cat.artefactos, estado_db=db, datos=ruta_mini)
        fila = next(f for f in cat.filas_inicio(NUEVO) if f["clave"] == "populares_tipo")
        assert fila["cluster"] == t["cluster"] and fila["tipo"] == t["tipo"]
        vistas = set(cat.ratings_usuario(NUEVO))
        esperado = [m for m in pop[pop["cluster"] == t["cluster"]].sort_values("rank")["movieId"]
                    if m not in vistas][:catalogo_app.N_FILA]
        assert [p["movieId"] for p in fila["peliculas"]] == esperado
    # Con el giro completo se recalcula y la fila sigue al tipo nuevo.
    _eventos(db, NUEVO, GIRO_ROMANCE[4:], t0=1_700_002_000)
    fila = next(f for f in cat.filas_inicio(NUEVO) if f["clave"] == "populares_tipo")
    t = perfiles.tipo_usuario(NUEVO, cat.artefactos, estado_db=db, datos=ruta_mini)
    assert t["n_ratings"] == 25 and fila["cluster"] == t["cluster"]


def test_explicar_solo_ratings_antes_del_corte_y_en_vivo(cat, ruta_mini, tmp_path):
    """C5: la explicación usa ratings < corte + eventos en vivo; texto con títulos y tags."""
    r = pd.read_csv(ruta_mini / "ratings.csv")
    mios = r[r["userId"] == 1]
    antes = dict(zip(mios.loc[mios["timestamp"] < fecha_a_epoch(CORTE), "movieId"],
                     mios.loc[mios["timestamp"] < fecha_a_epoch(CORTE), "rating"]))
    despues = set(mios.loc[mios["timestamp"] >= fecha_a_epoch(CORTE), "movieId"]) - set(antes)
    assert cat.ratings_usuario(1) == antes

    # Un evento en vivo entra; un rating posterior al corte no.
    vivo = next(m for m in cat._peliculas()["fichas"] if m not in antes and m not in despues)
    _eventos(tmp_path / "estado.db", 1, [(vivo, 5.0)])
    permitidas = {**antes, vivo: 5.0}
    assert cat.ratings_usuario(1) == permitidas

    con_tags = 0
    for mid in cat._peliculas()["fichas"]:
        e = cat.explicar(1, mid)
        assert e["veredicto"] in (catalogo_app.PROBABLE, catalogo_app.IMPROBABLE)
        for clave in ("parecida_alta", "parecida_baja"):
            if e[clave]:
                m = e[clave]["movieId"]
                assert m in permitidas and m not in despues and m != mid
                assert e[clave]["rating"] == permitidas[m]
                assert f"«{e[clave]['titulo']}»" in e["texto"]
        if e["parecida_alta"]:
            assert e["parecida_alta"]["rating"] >= 4
        if e["parecida_baja"]:
            assert e["parecida_baja"]["rating"] <= 2.5
        for t in e["tags_a_favor"] + e["tags_en_contra"]:
            assert t in e["texto"]
        con_tags += bool(e["tags_a_favor"] or e["tags_en_contra"])
        # Sin números de afinidad ni estrellas (los títulos pueden traer dígitos).
        sin_titulos = re.sub(r"«[^»]*»", "", e["texto"])
        assert not re.search(r"\d|★|estrella|afinidad", sin_titulos), e["texto"]
    assert con_tags > 0


def test_explicar_persona_en_comun(cat, tmp_path):
    """Un usuario que amó Forrest Gump ve a Tom Hanks como persona en común en Toy Story."""
    _eventos(tmp_path / "estado.db", NUEVO, [(356, 5.0), (539, 1.0), (2571, 3.0)])
    e = cat.explicar(NUEVO, 1)
    p = e["persona_comun"]
    assert p is not None and p["nombre"] == "Tom Hanks" and p["le_gusto"]
    assert "Tom Hanks" in e["texto"] and "Forrest Gump" in e["texto"]


def test_tarjeta_para_ti(cat):
    t = cat.tarjeta_para_ti(1)
    assert t["probable"] and t["improbable"]
    assert t["probable"]["movieId"] != t["improbable"]["movieId"]
    assert t["probable"]["veredicto"] == catalogo_app.PROBABLE
    assert t["improbable"]["veredicto"] == catalogo_app.IMPROBABLE
    assert t["probable"]["texto"].startswith(catalogo_app.PROBABLE)
    vistas = set(cat.ratings_usuario(1))
    assert t["probable"]["movieId"] not in vistas and t["improbable"]["movieId"] not in vistas


def test_cache_caliente_rapida(cat):
    """C7: 20 llamadas a ficha() y filas_inicio() con caché caliente < 2 s en total."""
    cat.ficha(1)
    cat.filas_inicio(1)
    ids = list(cat._peliculas()["fichas"])
    inicio = time.perf_counter()
    for i in range(20):
        cat.ficha(ids[i % len(ids)])
        cat.filas_inicio(1 + i % 3)
    assert time.perf_counter() - inicio < 2.0
