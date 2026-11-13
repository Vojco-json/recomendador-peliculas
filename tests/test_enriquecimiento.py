"""Pruebas del paquete 7: IMDb, TMDb y Letterboxd (sin red; todo con muestras).

- IMDb: cruce de muestras/ml-mini con muestras/imdb-mini (Toy Story = 81 min).
- TMDb: sin clave devuelve None sin red; con una sesión falsa se prueban es-ES, el
  respaldo en-US, la caché JSON y el 404.
- Letterboxd: muestras/letterboxd_ratings.csv tiene 4 películas de ml-mini y 1
  inexistente -> 'Emparejadas: 4 de 5', userId >= 1000000, perfil y fold-in.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from base_mini import crear_base_mini
from recomendador import config, enriquecimiento as enr
from scripts import descargar_imdb, importar_letterboxd

IMDB_MINI = config.RAIZ / "muestras" / "imdb-mini"
CSV_LETTERBOXD = config.RAIZ / "muestras" / "letterboxd_ratings.csv"


# --------------------------------------------------------------------------- #
# IMDb
# --------------------------------------------------------------------------- #

def test_imdb_a_tconst():
    assert enr.imdb_a_tconst("0114709") == "tt0114709"
    assert enr.imdb_a_tconst(114709) == "tt0114709"
    assert enr.imdb_a_tconst("tt0468569") == "tt0468569"
    assert enr.imdb_a_tconst("10872600") == "tt10872600"     # ids nuevos de 8 dígitos
    assert enr.imdb_a_tconst(None) is None


def test_cruce_imdb_toy_story(ruta_mini, tmp_path):
    df = enr.enriquecer_imdb(ruta_mini, IMDB_MINI, tmp_path)
    assert list(df.columns) == enr.COLUMNAS_ENRIQUECIDAS
    assert len(df) == 25
    escrito = pd.read_parquet(tmp_path / enr.NOMBRE_ENRIQUECIDAS)
    assert list(escrito.columns) == enr.COLUMNAS_ENRIQUECIDAS
    toy = escrito.set_index("movieId").loc[1]
    assert toy["titulo"] == "Toy Story"
    assert int(toy["anio"]) == 1995
    assert int(toy["duracion_min"]) == 81
    assert toy["imdb_rating"] == pytest.approx(8.3)
    assert int(toy["imdb_votos"]) > 0
    assert int(toy["tmdbId"]) == 862
    sospechosos = escrito.set_index("movieId").loc[50]
    assert sospechosos["titulo"] == "The Usual Suspects"
    # La Dark Knight no está en title.basics de la muestra: queda sin duración,
    # con el año del título de MovieLens y con su nota (sí está en title.ratings).
    tdk = escrito.set_index("movieId").loc[58559]
    assert pd.isna(tdk["duracion_min"]) and int(tdk["anio"]) == 2008
    assert tdk["imdb_rating"] == pytest.approx(9.0)
    # Groundhog Day no está en title.ratings: tiene duración pero no nota.
    groundhog = escrito.set_index("movieId").loc[1265]
    assert int(groundhog["duracion_min"]) == 101 and pd.isna(groundhog["imdb_rating"])
    # Las filas ajenas (Carmencita, Breaking Bad) no entran.
    assert enr.resumen_enriquecimiento(escrito) == {"peliculas": 25, "con_duracion": 24,
                                                    "con_nota_imdb": 24}


def test_script_imdb_sin_descargar(ruta_mini, tmp_path, capsys):
    codigo = descargar_imdb.main(["--sin-descargar", "--imdb", str(IMDB_MINI),
                                  "--datos", str(ruta_mini), "--salida", str(tmp_path)])
    assert codigo == 0
    assert (tmp_path / enr.NOMBRE_ENRIQUECIDAS).exists()
    assert "con duración IMDb: 24" in capsys.readouterr().out
    assert enr.URL_IMDB == "https://datasets.imdbws.com/"
    assert enr.ARCHIVOS_IMDB == ("title.basics.tsv.gz", "title.ratings.tsv.gz")


# --------------------------------------------------------------------------- #
# TMDb
# --------------------------------------------------------------------------- #

class _Respuesta:
    def __init__(self, codigo, datos):
        self.status_code, self._datos = codigo, datos

    def json(self):
        return self._datos


class _SesionFalsa:
    """Imita requests: registra las llamadas y responde según el idioma."""

    def __init__(self, respuestas):
        self.respuestas, self.llamadas = respuestas, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append((url, dict(params or {})))
        codigo, datos = self.respuestas[params["language"]]
        return _Respuesta(codigo, datos)


def test_tmdb_sin_clave_degrada_sin_error(monkeypatch, tmp_path):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    sesion = _SesionFalsa({})
    cliente = enr.ClienteTMDb(cache=tmp_path / "tmdb_cache.json", sesion=sesion)
    assert not cliente.disponible
    assert cliente.detalle(862) is None
    assert cliente.detalle(None) is None
    assert sesion.llamadas == []                      # sin red
    assert not (tmp_path / "tmdb_cache.json").exists()


def test_tmdb_es_con_respaldo_en_y_cache(monkeypatch, tmp_path):
    monkeypatch.setenv("TMDB_API_KEY", "clave_de_prueba")
    sesion = _SesionFalsa({
        "es-ES": (200, {"title": "Toy Story", "overview": "", "poster_path": "/es.jpg"}),
        "en-US": (200, {"title": "Toy Story", "overview": "Woody, a cowboy doll...",
                        "poster_path": "/en.jpg"}),
    })
    cache = tmp_path / "tmdb_cache.json"
    cliente = enr.ClienteTMDb(cache=cache, sesion=sesion)
    dato = cliente.detalle(862)
    assert dato["sinopsis"] == "Woody, a cowboy doll..."
    assert dato["idioma_sinopsis"] == "en-US"
    assert dato["poster_url"] == "https://image.tmdb.org/t/p/w342/es.jpg"
    assert [p["language"] for _, p in sesion.llamadas] == ["es-ES", "en-US"]
    assert sesion.llamadas[0][0] == "https://api.themoviedb.org/3/movie/862"
    assert sesion.llamadas[0][1]["api_key"] == "clave_de_prueba"
    # Caché en JSON: un cliente nuevo no vuelve a llamar.
    assert "862" in json.loads(cache.read_text(encoding="utf-8"))
    sesion2 = _SesionFalsa({})
    assert enr.ClienteTMDb(cache=cache, sesion=sesion2).detalle(862)["tmdbId"] == 862
    assert sesion2.llamadas == []


def test_tmdb_404_y_error_de_red(monkeypatch, tmp_path):
    monkeypatch.setenv("TMDB_API_KEY", "clave")
    cache = tmp_path / "c.json"
    sesion = _SesionFalsa({"es-ES": (404, {})})
    assert enr.ClienteTMDb(cache=cache, sesion=sesion).detalle(1) is None

    class _SinRed:
        def get(self, *a, **k):
            raise ConnectionError("sin internet")

    assert enr.ClienteTMDb(cache=tmp_path / "d.json", sesion=_SinRed()).detalle(2) is None
    assert not (tmp_path / "d.json").exists()       # el fallo de red no se cachea


# --------------------------------------------------------------------------- #
# Letterboxd
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("letterboxd, movielens", [
    ("The Usual Suspects", "Usual Suspects, The (1995)"),
    ("Se7en", "Seven (a.k.a. Se7en) (1995)"),
    ("12 Monkeys", "Twelve Monkeys (a.k.a. 12 Monkeys) (1995)"),
    ("Amélie", "Amelie (Fabuleux destin d'Amélie Poulain, Le) (2001)"),
    ("When Harry Met Sally...", "When Harry Met Sally... (1989)"),
    ("Star Wars: Episode IV – A New Hope", "Star Wars: Episode IV - A New Hope (1977)"),
])
def test_normalizacion_de_titulos(letterboxd, movielens):
    claves, _ = enr.variantes_titulo(movielens)
    assert enr.normalizar_titulo(letterboxd) in claves


def test_titulo_legible_y_articulos():
    assert enr.titulo_legible("Usual Suspects, The (1995)") == "The Usual Suspects"
    assert enr.titulo_legible("Seven (a.k.a. Se7en) (1995)") == "Seven"
    assert enr.anteponer_articulo("Avventura, L'") == "L'Avventura"


def test_emparejar_anio_tolerancia():
    movies = pd.DataFrame({"movieId": [1, 2], "title": ["Heat (1995)", "Heat (1986)"]})
    filas = [{"fecha": "2024-01-01", "titulo": "Heat", "anio": 1996, "uri": "", "rating": 4},
             {"fecha": "2024-01-02", "titulo": "Heat", "anio": 1990, "uri": "", "rating": 3}]
    emparejadas, sueltas = enr.emparejar(filas, movies)
    assert [f["movieId"] for f in emparejadas] == [1]       # 1996 -> 1995 (±1)
    assert [f["anio"] for f in sueltas] == [1990]           # 1990 no está a ±1


def test_columnas_letterboxd_invalidas(tmp_path):
    malo = tmp_path / "malo.csv"
    malo.write_text("Fecha,Titulo\n2024-01-01,Heat\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Letterboxd URI"):
        enr.leer_letterboxd(malo)


@pytest.fixture(scope="module")
def artefactos_mini(spark, ruta_mini, tmp_path_factory):
    """ALS + perfiles (k=3) de la muestra en un directorio temporal."""
    return crear_base_mini(spark, ruta_mini, tmp_path_factory.mktemp("letterboxd"))


def test_importar_letterboxd_emparejadas_4_de_5(artefactos_mini, ruta_mini, capsys):
    codigo = importar_letterboxd.main(["--csv", str(CSV_LETTERBOXD), "--datos", str(ruta_mini),
                                       "--artefactos", str(artefactos_mini),
                                       "--estado", str(artefactos_mini / "estado.db")])
    salida = capsys.readouterr().out
    assert codigo == 0, salida
    assert "Emparejadas: 4 de 5" in salida
    assert "Una Película Que No Existe" in salida

    carpeta = artefactos_mini / enr.DIR_LETTERBOXD
    archivos = list(carpeta.glob("usuario_*.json"))
    assert len(archivos) == 1
    res = json.loads(archivos[0].read_text(encoding="utf-8"))
    uid = res["userId"]
    assert uid >= 1_000_000
    assert f"userId nuevo: {uid}" in salida
    assert sorted(f["movieId"] for f in res["emparejadas"]) == [1, 47, 50, 58559]
    # Perfil: con Toy Story 5★ y Se7en 3.5★, Animation pesa más que Mystery.
    perfil = res["perfil"]
    assert perfil["userId"] == uid and perfil["n_ratings"] == 4
    assert perfil["tipo"]
    assert perfil["pesos_generos"]["Animation"] > perfil["pesos_generos"]["Mystery"]
    # Fold-in: 10 recomendaciones, ninguna ya vista, con título.
    recs = res["recomendaciones"]
    assert len(recs) == 10
    assert not {r["movieId"] for r in recs} & {1, 47, 50, 58559}
    assert all(r["titulo"] for r in recs)
    # Ratings en formato MovieLens y estado en vivo.
    ratings = pd.read_csv(carpeta / "ratings_letterboxd.csv")
    assert list(ratings.columns) == ["userId", "movieId", "rating", "timestamp"]
    assert len(ratings) == 4 and set(ratings["userId"]) == {uid}
    from recomendador import estado
    con = estado.conectar(artefactos_mini / "estado.db")
    try:
        assert len(estado.eventos_usuario(con, uid)) == 4
        assert len(estado.leer_recomendaciones(con, uid)) == 10
        assert estado.leer_perfil(con, uid)["userId"] == uid
    finally:
        con.close()

    # Reimportar el mismo CSV reutiliza el userId (no duplica usuarios).
    assert importar_letterboxd.main(["--csv", str(CSV_LETTERBOXD), "--datos", str(ruta_mini),
                                     "--artefactos", str(artefactos_mini)]) == 0
    assert f"userId nuevo: {uid}" in capsys.readouterr().out
    assert len(list(carpeta.glob("usuario_*.json"))) == 1


def test_importador_columnas_invalidas_termina_con_error(tmp_path, ruta_mini, capsys):
    malo = tmp_path / "malo.csv"
    malo.write_text("Date,Name\n2024-01-01,Heat\n", encoding="utf-8")
    codigo = importar_letterboxd.main(["--csv", str(malo), "--datos", str(ruta_mini),
                                       "--artefactos", str(tmp_path / "art")])
    assert codigo == 1
    assert "faltan las columnas" in capsys.readouterr().err
    assert not (tmp_path / "art").exists()           # no arrancó Spark ni creó nada
