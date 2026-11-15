"""Paquete 3: herramienta buscar_pelicula (registrada en ESQUEMAS) sobre la muestra mini.

Nunca sale a la red: sin clave usa un cliente TMDb sin clave y una sesión prohibida;
con clave, una caché temporal con un título en español.
"""

from __future__ import annotations

import csv
import json

import pytest

from recomendador import config
from recomendador.enriquecimiento import ClienteTMDb
from recomendador.herramientas import ESQUEMAS, Herramientas

MINI = config.MUESTRA_MINI


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover - si se llama, la prueba falla
        raise AssertionError("no debe haber llamadas de red")


def _tmdb_de(movie_id: int) -> int:
    with open(MINI / "links.csv", encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            if int(fila["movieId"]) == movie_id:
                return int(float(fila["tmdbId"]))
    raise AssertionError(f"movieId {movie_id} sin tmdbId")


@pytest.fixture
def herr(tmp_path):
    cliente = ClienteTMDb(api_key="", cache=tmp_path / "cache.json", sesion=SesionProhibida())
    return Herramientas(MINI, tmp_path / "art", tmp_path / "estado.db", tmdb=cliente)


def test_esquema_registrado():
    esquema = next(e["function"] for e in ESQUEMAS if e["function"]["name"] == "buscar_pelicula")
    assert esquema["description"]
    assert set(esquema["parameters"]["properties"]) == {"titulo"}
    assert esquema["parameters"]["required"] == ["titulo"]


@pytest.mark.parametrize("consulta, movie_id", [
    ("matrix", 2571), ("TOY STORY", 1), ("Términator 2: judgment day", 589),
    ("Shawshank Redemtion", 318), ("forest gump", 356),
])
def test_encuentra_aproximado(herr, consulta, movie_id):
    r = herr.ejecutar("buscar_pelicula", {"titulo": consulta})
    assert r["encontrada"] is True and r["movieId"] == movie_id, r
    assert r["titulo"] and "titulo_es" not in r          # sin clave no hay título ES
    json.dumps(r)


@pytest.mark.parametrize("consulta", ["Casablanca", "xyz", ""])
def test_inexistente_una_frase(herr, consulta):
    r = herr.buscar_pelicula(consulta)
    assert r["encontrada"] is False
    assert r["mensaje"].endswith(".") and r["mensaje"].count(".") == 1


def test_titulo_en_espanol_con_cache(tmp_path):
    cache = tmp_path / "cache.json"
    cache.write_text(json.dumps({str(_tmdb_de(318)): {"tmdbId": _tmdb_de(318),
                                                      "titulo": "Cadena perpetua"}}),
                     encoding="utf-8")
    cliente = ClienteTMDb(api_key="clave", cache=cache, sesion=SesionProhibida())
    herr = Herramientas(MINI, tmp_path / "art", tmp_path / "estado.db", tmdb=cliente)
    r = herr.buscar_pelicula("cadena perpétua")
    assert r["encontrada"] and r["movieId"] == 318
    assert r["titulo_es"] == "Cadena perpetua"
