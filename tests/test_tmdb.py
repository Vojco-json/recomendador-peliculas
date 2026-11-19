"""recomendador/tmdb.py: caché de TMDb y respaldo sin clave (sin red real)."""

from __future__ import annotations

import json

import pytest

from recomendador import tmdb


class _Resp:
    def __init__(self, codigo, datos):
        self.status_code, self._datos = codigo, datos

    def json(self):
        return self._datos


class SesionFalsa:
    """Doble de requests: responde ficha y videos de TMDb y cuenta las llamadas."""

    def __init__(self):
        self.llamadas = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.llamadas.append(url)
        if url.endswith("/videos"):
            if params["language"] == "es-ES":
                return _Resp(200, {"results": []})
            return _Resp(200, {"results": [
                {"site": "YouTube", "key": "teaser1", "type": "Teaser", "official": True},
                {"site": "Vimeo", "key": "x", "type": "Trailer"},
                {"site": "YouTube", "key": "TRAILER42", "type": "Trailer", "official": True}]})
        return _Resp(200, {"title": "Toy Story", "overview": "Un vaquero de juguete...",
                           "poster_path": "/abc.jpg"})


class SesionRota:
    def get(self, *a, **k):
        raise AssertionError("no debe haber llamadas de red")


TOY = {"movieId": 1, "titulo": "Toy Story", "anio": 1995,
       "generos": ["Adventure", "Animation"], "tmdbId": 862}


@pytest.fixture
def sin_clave(monkeypatch):
    monkeypatch.delenv("TMDB_API_KEY", raising=False)


def test_sin_clave_poster_svg_y_sin_trailer(sin_clave, tmp_path):
    t = tmdb.TMDb(cache=tmp_path / "c.json", cache_videos=tmp_path / "v.json",
                  sesion=SesionRota())
    assert not t.disponible
    svg = t.poster(TOY)
    assert tmdb.es_svg(svg) and "Toy Story" in svg
    assert tmdb.COLORES_GENERO["Adventure"] in svg          # color del género principal
    assert t.trailer(862) is None and t.sinopsis(862) is None
    assert t.trailer(None) is None and t.trailer("x") is None
    assert t.poster_src(TOY).startswith("data:image/svg+xml;base64,")
    assert not (tmp_path / "v.json").exists()               # nada que guardar


def test_svg_escapa_y_color_por_genero():
    svg = tmdb.poster_svg("Tom & Jerry <El regreso>", ["Comedy"], 2001)
    assert "Tom &amp; Jerry" in svg and "<El" not in svg
    assert tmdb.COLORES_GENERO["Comedy"] in svg and "2001" in svg
    assert tmdb.color_genero(["(no genres listed)"]) == tmdb.COLOR_DEFECTO
    assert tmdb.color_genero(["IMAX", "Horror"]) == tmdb.COLORES_GENERO["IMAX"]


def test_con_clave_poster_sinopsis_y_trailer_quedan_en_cache(tmp_path):
    s = SesionFalsa()
    t = tmdb.TMDb(api_key="clave", cache=tmp_path / "c.json",
                  cache_videos=tmp_path / "v.json", sesion=s)
    assert t.poster(TOY) == "https://image.tmdb.org/t/p/w342/abc.jpg"
    assert t.sinopsis(862) == "Un vaquero de juguete..."
    url = t.trailer(862)
    assert url == "https://www.youtube.com/watch?v=TRAILER42"    # Trailer antes que Teaser
    n = len(s.llamadas)
    assert t.trailer(862) == url and t.poster(TOY).endswith("/abc.jpg")
    assert len(s.llamadas) == n                                 # segunda vez: caché
    assert json.loads((tmp_path / "v.json").read_text(encoding="utf-8"))["862"]["url"] == url
    # Un cliente nuevo, sin clave ni red, reutiliza la caché del disco.
    otro = tmdb.TMDb(api_key="", cache=tmp_path / "c.json", cache_videos=tmp_path / "v.json",
                     sesion=SesionRota())
    assert otro.trailer(862) == url and otro.poster(TOY).endswith("/abc.jpg")


def test_red_caida_no_lanza_ni_guarda(tmp_path):
    class Caida:
        def get(self, *a, **k):
            raise ConnectionError("sin internet")

    t = tmdb.TMDb(api_key="clave", cache=tmp_path / "c.json",
                  cache_videos=tmp_path / "v.json", sesion=Caida())
    assert t.trailer(862) is None
    assert tmdb.es_svg(t.poster(TOY)) and t.sinopsis(862) is None
    assert not (tmp_path / "v.json").exists()


def test_sin_trailer_se_guarda_como_none(tmp_path):
    class SinVideos(SesionFalsa):
        def get(self, url, **k):
            self.llamadas.append(url)
            return _Resp(200, {"results": []})

    s = SinVideos()
    t = tmdb.TMDb(api_key="clave", cache=tmp_path / "c.json",
                  cache_videos=tmp_path / "v.json", sesion=s)
    assert t.trailer(5) is None and len(s.llamadas) == 2       # es-ES y en-US
    assert t.trailer(5) is None and len(s.llamadas) == 2       # no vuelve a preguntar


def test_atribucion_tmdb():
    assert "TMDB" in tmdb.ATRIBUCION_TMDB
