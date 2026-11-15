"""Búsqueda aproximada de títulos (recomendador/titulos.py) con y sin TMDb.

Nunca sale a la red: sin clave se verifica que no haya llamadas y con clave se usa
una sesión HTTP falsa y una caché temporal.
"""

import csv
import json

import pytest

from recomendador import config
from recomendador.enriquecimiento import CLAVE_BUSQUEDAS, ClienteTMDb
from recomendador.titulos import UMBRAL, BuscadorTitulos

MINI = config.MUESTRA_MINI


def _tmdb_de(movie_id: int) -> int:
    with open(MINI / "links.csv", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            if int(fila["movieId"]) == movie_id:
                return int(float(fila["tmdbId"]))
    raise AssertionError(f"movieId {movie_id} sin tmdbId en la muestra")


class SesionFalsa:
    """Imita requests: registra las llamadas y responde /search/movie."""

    def __init__(self, resultados=None, falla=False):
        self.llamadas = []
        self.resultados = resultados or []
        self.falla = falla

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append((url, dict(params or {})))
        if self.falla:
            raise ConnectionError("sin red")
        resultados = self.resultados

        class R:
            status_code = 200

            @staticmethod
            def json():
                return {"results": resultados}
        return R()


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover - si se llama, la prueba falla
        raise AssertionError("no debe haber llamadas de red sin TMDB_API_KEY")


@pytest.fixture
def sin_clave(tmp_path):
    cliente = ClienteTMDb(api_key="", cache=tmp_path / "cache.json", sesion=SesionProhibida())
    return BuscadorTitulos(MINI, cliente)


# --------------------------------------------------------------------------- #
# Solo MovieLens (C6, C7)
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("consulta, movie_id", [
    ("toy story", 1),
    ("TOY STORY (1995)", 1),
    ("The Usual Suspects", 50),       # ', The' final de MovieLens
    ("usual suspects", 50),
    ("Matrix", 2571),
    ("the matrix (1999)", 2571),
    ("Se7en", 47),                    # título alternativo a.k.a.
    ("12 monkeys", 32),
    ("Shawshank Redemtion", 318),     # error de tipeo
    ("forest gump", 356),
    ("Schindlers List", 527),
    ("Términator 2: Judgment Day", 589),  # tildes ignoradas
])
def test_movielens_aproximado(sin_clave, consulta, movie_id):
    r = sin_clave.buscar(consulta)
    assert r["encontrada"] is True, r
    assert r["movieId"] == movie_id
    assert r["fuente"] == "movielens"
    assert r["coincidencia"] >= UMBRAL


@pytest.mark.parametrize("consulta", ["Casablanca", "El señor de los anillos", "xyz", "Toy"])
def test_sin_resultados_razonables(sin_clave, consulta):
    r = sin_clave.buscar(consulta)
    assert r["encontrada"] is False
    assert r["mensaje"].endswith(".") and r["mensaje"].count(".") == 1  # una frase


def test_umbral_es_0_8():
    assert UMBRAL == 0.8


def test_sin_clave_no_usa_cache_ni_red(tmp_path):
    # Aunque la caché tenga un título en español, sin clave solo se usa MovieLens.
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({str(_tmdb_de(318)): {"titulo": "Cadena perpetua"}}),
                     encoding="utf-8")
    b = BuscadorTitulos(MINI, ClienteTMDb(api_key="", cache=cache, sesion=SesionProhibida()))
    assert not b.usa_tmdb
    assert b.buscar("Cadena perpetua")["encontrada"] is False


# --------------------------------------------------------------------------- #
# Con clave: títulos en español de la caché y /search/movie (C8)
# --------------------------------------------------------------------------- #

def test_titulo_es_desde_cache(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({str(_tmdb_de(318)): {"tmdbId": _tmdb_de(318),
                                                      "titulo": "Cadena perpetua"}}),
                     encoding="utf-8")
    sesion = SesionFalsa()
    b = BuscadorTitulos(MINI, ClienteTMDb(api_key="clave", cache=cache, sesion=sesion))
    r = b.buscar("cadena perpétua")
    assert r["encontrada"] and r["movieId"] == 318 and r["fuente"] == "tmdb_cache"
    assert sesion.llamadas == []          # coincidencia exacta en caché: sin red


def test_busqueda_tmdb_es_y_cache(tmp_path):
    cache = tmp_path / "cache.json"
    tmdb_lion = _tmdb_de(364)
    sesion = SesionFalsa([{"id": tmdb_lion, "title": "El rey león",
                           "original_title": "The Lion King", "release_date": "1994-06-15"},
                          {"id": 999999999, "title": "El rey león 2"}])
    cliente = ClienteTMDb(api_key="clave", cache=cache, sesion=sesion)
    r = BuscadorTitulos(MINI, cliente).buscar("El Rey Leon")
    assert r["encontrada"] and r["movieId"] == 364 and r["fuente"] == "tmdb_busqueda"
    url, params = sesion.llamadas[0]
    assert url.endswith("/search/movie") and params["language"] == "es-ES"
    assert params["query"] == "El Rey Leon"
    # El resultado quedó en la caché: una segunda búsqueda no sale a la red.
    guardado = json.loads(cache.read_text(encoding="utf-8"))
    assert guardado[CLAVE_BUSQUEDAS]
    sesion2 = SesionProhibida()
    r2 = BuscadorTitulos(MINI, ClienteTMDb(api_key="clave", cache=cache,
                                           sesion=sesion2)).buscar("el rey león")
    assert r2["movieId"] == 364


def test_red_caida_sigue_con_movielens(tmp_path):
    cliente = ClienteTMDb(api_key="clave", cache=tmp_path / "c.json",
                          sesion=SesionFalsa(falla=True))
    b = BuscadorTitulos(MINI, cliente)
    assert b.buscar("Pulp Fiction")["movieId"] == 296
    assert b.buscar("Tiempos violentos")["encontrada"] is False
    assert not (tmp_path / "c.json").exists()     # un fallo de red no se guarda
