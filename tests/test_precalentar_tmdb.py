"""Requisito 89: scripts/precalentar_tmdb.py con un cliente simulado (sin red).

- 8 hilos en paralelo, reintento ante 429, respaldo en inglés;
- solo películas con 20 o más votos antes del corte;
- tmdb.py consulta el parquet antes que la red.
"""

from __future__ import annotations

import threading
import time

import pandas as pd

from recomendador import tmdb
from scripts import precalentar_tmdb as pre


class Resp:
    def __init__(self, codigo, datos=None, cabeceras=None):
        self.status_code, self._datos, self.headers = codigo, datos or {}, cabeceras or {}

    def json(self):
        return self._datos


class SesionSimulada:
    """Responde /movie/{id}: un 429 la primera vez de cada película; sin sinopsis en
    español para los ids pares; registra los hilos concurrentes."""

    def __init__(self):
        self.lock = threading.Lock()
        self.vistos_429: set[int] = set()
        self.llamadas: list[tuple[int, str]] = []
        self.activos = self.max_activos = 0

    def get(self, url, params=None, headers=None, timeout=None):
        tmdb_id = int(url.rstrip("/").split("/")[-1].split("?")[0])
        idioma = params["language"]
        with self.lock:
            self.activos += 1
            self.max_activos = max(self.max_activos, self.activos)
            self.llamadas.append((tmdb_id, idioma))
            primera = tmdb_id not in self.vistos_429
            self.vistos_429.add(tmdb_id)
        time.sleep(0.02)                         # deja que los hilos se solapen
        with self.lock:
            self.activos -= 1
        if primera:
            return Resp(429, cabeceras={"Retry-After": "0"})
        if idioma == "es-ES":
            return Resp(200, {"title": f"Título {tmdb_id}", "poster_path": f"/p{tmdb_id}.jpg",
                              "overview": "" if tmdb_id % 2 == 0 else f"Sinopsis {tmdb_id}"})
        return Resp(200, {"title": f"Title {tmdb_id}", "poster_path": f"/p{tmdb_id}.jpg",
                          "overview": f"Overview {tmdb_id}"})


def test_candidatas_solo_20_votos_o_mas(tmp_path):
    ruta = tmp_path / "peliculas.parquet"
    pd.DataFrame({"movieId": [1, 2, 3, 4], "tmdbId": [11, 12, None, 14],
                  "votos": [100, 19, 50, 20]}).to_parquet(ruta)
    assert pre.candidatas(ruta, 20) == [(1, 11), (4, 14)]


def test_precalentar_hilos_429_y_respaldo_ingles(tmp_path):
    sesion, esperas = SesionSimulada(), []
    pares = [(m, 100 + m) for m in range(1, 25)]
    salida = tmp_path / "app" / "tmdb.parquet"
    n = pre.precalentar(pares, "clave", salida, sesion=sesion, hilos=8,
                        dormir=esperas.append)
    assert n == 24 and salida.exists()
    assert 1 < sesion.max_activos <= 8                   # paralelo, con 8 hilos como tope
    assert len(esperas) >= 24                            # un 429 por película, reintentado
    df = pd.read_parquet(salida).set_index("tmdbId")
    assert df.loc[101, "sinopsis"] == "Sinopsis 101" and df.loc[101, "idioma_sinopsis"] == "es-ES"
    assert df.loc[102, "sinopsis"] == "Overview 102" and df.loc[102, "idioma_sinopsis"] == "en-US"
    assert df.loc[102, "titulo"] == "Título 102"         # el título sigue en español
    assert str(df.loc[101, "poster_url"]).endswith("/p101.jpg")

    # Reanudable: una segunda corrida no vuelve a pedir lo que ya está.
    sesion2 = SesionSimulada()
    assert pre.precalentar(pares, "clave", salida, sesion=sesion2, dormir=lambda s: None) == 0
    assert sesion2.llamadas == []


def test_tmdb_lee_el_parquet_antes_que_la_red(tmp_path):
    salida = tmp_path / "tmdb.parquet"
    pre.precalentar([(1, 101)], "clave", salida, sesion=SesionSimulada(),
                    dormir=lambda s: None)

    class SinRed:
        def get(self, *a, **k):
            raise AssertionError("no debía consultar la red")

    cliente = tmdb.TMDb(api_key="clave", cache=tmp_path / "cache.db",
                               sesion=SinRed(), parquet=salida)
    assert cliente.sinopsis(101) == "Sinopsis 101"
