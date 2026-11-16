"""Póster, sinopsis y tráiler de TMDb para la app, con caché JSON y respaldo sin clave.

- Con ``TMDB_API_KEY``: póster y sinopsis vía ``enriquecimiento.ClienteTMDb``
  (caché ``tmdb_cache.json``) y tráiler de YouTube vía /movie/{id}/videos
  (caché ``tmdb_videos.json``). Cada película se consulta a la red una sola vez.
- Sin clave (o sin red): ``poster`` devuelve una carátula SVG generada con el título y
  el color del género principal, ``sinopsis`` y ``trailer`` devuelven None sin errores.
- La app muestra siempre ``ATRIBUCION_TMDB`` al pie.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import textwrap
import time
from html import escape
from pathlib import Path

from . import config
from .enriquecimiento import ATRIBUCION_TMDB, CACHE_TMDB, ClienteTMDb

log = logging.getLogger(__name__)

URL_VIDEOS_TMDB = "https://api.themoviedb.org/3/movie/{tmdb_id}/videos"
URL_YOUTUBE = "https://www.youtube.com/watch?v={clave}"
CACHE_VIDEOS = config.ARTEFACTOS / "tmdb_videos.json"
IDIOMAS_VIDEOS = ("es-ES", "en-US")

# Paleta de MoviePredict por género (fondo de la carátula generada).
COLORES_GENERO = {
    "Action": "#C2412D", "Adventure": "#D9822B", "Animation": "#E3B23C",
    "Children": "#7FB069", "Comedy": "#F2C14E", "Crime": "#5B4B8A",
    "Documentary": "#4F7C82", "Drama": "#3D5A80", "Fantasy": "#8E5EA2",
    "Film-Noir": "#2F2F38", "Horror": "#7A1E2C", "IMAX": "#2E6F95",
    "Musical": "#C8577E", "Mystery": "#46467A", "Romance": "#B5485D",
    "Sci-Fi": "#2A8C8C", "Thriller": "#4A4E69", "War": "#6B705C",
    "Western": "#A0663A",
}
COLOR_DEFECTO = "#3A3F4B"


# --------------------------------------------------------------------------- #
# Carátula generada (sin red)
# --------------------------------------------------------------------------- #

def color_genero(generos) -> str:
    """Color del primer género con color propio, o el gris de respaldo."""
    for g in generos or []:
        if g in COLORES_GENERO:
            return COLORES_GENERO[g]
    return COLOR_DEFECTO


def _aclarar(hex_color: str, factor: float) -> str:
    """Mezcla el color con blanco (factor > 0) o con negro (factor < 0)."""
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    destino = 255 if factor > 0 else 0
    f = abs(factor)
    return "#{:02X}{:02X}{:02X}".format(*(round(c + (destino - c) * f) for c in (r, g, b)))


def poster_svg(titulo: str, generos=None, anio=None) -> str:
    """Carátula SVG 2:3 con degradado del color del género, título y año."""
    color = color_genero(generos)
    lineas = textwrap.wrap(str(titulo or "Sin título"), width=14)[:5] or ["Sin título"]
    tam = 26 if len(lineas) <= 3 else 22
    y0 = 190 - (len(lineas) - 1) * tam * 0.6
    texto = "".join(
        f'<tspan x="20" y="{y0 + i * tam * 1.2:.0f}">{escape(l)}</tspan>'
        for i, l in enumerate(lineas))
    genero = escape(" · ".join([g for g in (generos or []) if g != "(no genres listed)"][:2]))
    anio_txt = escape(str(anio)) if anio else ""
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 300" width="200" '
        f'height="300" role="img" aria-label="{escape(str(titulo or ""))}">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1">'
        f'<stop offset="0" stop-color="{_aclarar(color, 0.15)}"/>'
        f'<stop offset="1" stop-color="{_aclarar(color, -0.55)}"/></linearGradient></defs>'
        f'<rect width="200" height="300" rx="8" fill="url(#g)" data-color="{color}"/>'
        '<rect x="12" y="12" width="176" height="276" rx="5" fill="none" '
        'stroke="#FFFFFF" stroke-opacity="0.18"/>'
        f'<text font-family="Georgia, serif" font-size="{tam}" font-weight="700" '
        f'fill="#FFFFFF">{texto}</text>'
        f'<text x="20" y="262" font-family="Helvetica, Arial, sans-serif" font-size="12" '
        f'fill="#FFFFFF" fill-opacity="0.8">{genero}</text>'
        f'<text x="20" y="280" font-family="Helvetica, Arial, sans-serif" font-size="12" '
        f'fill="#FFFFFF" fill-opacity="0.6">{anio_txt}</text></svg>')


def svg_a_data_uri(svg: str) -> str:
    """SVG -> data URI (para <img src> o st.image)."""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


def es_svg(valor) -> bool:
    return isinstance(valor, str) and valor.lstrip().startswith("<svg")


# --------------------------------------------------------------------------- #
# Cliente de la app
# --------------------------------------------------------------------------- #

class TMDb:
    """Póster, sinopsis y tráiler con caché; nunca lanza excepciones a la app.

    api_key:      por defecto TMDB_API_KEY del entorno (vacía = sin clave).
    cache:        caché de fichas (la misma de enriquecimiento: tmdb_cache.json).
    cache_videos: caché de tráileres (tmdb_videos.json).
    sesion:       objeto con ``get`` (requests o un doble de pruebas).
    """

    def __init__(self, api_key: str | None = None, cache=None, cache_videos=None,
                 sesion=None, timeout: float = 8.0):
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("TMDB_API_KEY")) or None
        self.timeout = timeout
        self._sesion = sesion
        self.cliente = ClienteTMDb(api_key=self.api_key or "", cache=cache or CACHE_TMDB,
                                   timeout=timeout, sesion=sesion)
        self.cliente.api_key = self.api_key           # "" -> None: sin clave
        self.cache_videos = Path(cache_videos) if cache_videos else CACHE_VIDEOS
        self._videos: dict | None = None

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    # ---- fichas -------------------------------------------------------- #
    def detalle(self, tmdb_id) -> dict | None:
        try:
            return self.cliente.detalle(tmdb_id)
        except Exception as exc:  # caché dañada, respuesta rara...
            log.warning("TMDb detalle falló (%s)", exc)
            return None

    def poster(self, pelicula: dict) -> str:
        """URL del póster de TMDb o, si no hay, una carátula SVG generada.

        ``pelicula``: dict con titulo, generos, anio y tmdbId (p. ej. ficha_corta).
        """
        dato = self.detalle(pelicula.get("tmdbId")) if pelicula.get("tmdbId") else None
        if dato and dato.get("poster_url"):
            return dato["poster_url"]
        return poster_svg(pelicula.get("titulo"), pelicula.get("generos"), pelicula.get("anio"))

    def poster_src(self, pelicula: dict) -> str:
        """Igual que ``poster`` pero siempre usable como src de <img>."""
        p = self.poster(pelicula)
        return svg_a_data_uri(p) if es_svg(p) else p

    def sinopsis(self, tmdb_id) -> str | None:
        dato = self.detalle(tmdb_id) if tmdb_id else None
        return (dato or {}).get("sinopsis") or None

    # ---- tráiler ------------------------------------------------------- #
    def _leer_videos(self) -> dict:
        if self._videos is None:
            try:
                self._videos = json.loads(self.cache_videos.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._videos = {}
        return self._videos

    def _guardar_videos(self) -> None:
        try:
            self.cache_videos.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_videos.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._videos, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            tmp.replace(self.cache_videos)          # escritura atómica
        except OSError as exc:
            log.warning("No se pudo guardar %s: %s", self.cache_videos, exc)

    def _pedir_videos(self, tmdb_id: int, idioma: str) -> list | None:
        """Lista de videos de TMDb; [] si 404; None si la red falla."""
        try:
            import requests

            sesion = self._sesion or requests
            params, cab = {"language": idioma}, {"accept": "application/json"}
            if self.api_key.startswith("eyJ"):
                cab["Authorization"] = f"Bearer {self.api_key}"
            else:
                params["api_key"] = self.api_key
            r = sesion.get(URL_VIDEOS_TMDB.format(tmdb_id=tmdb_id), params=params,
                           headers=cab, timeout=self.timeout)
            if r.status_code == 404:
                return []
            if r.status_code != 200:
                return None
            return list((r.json() or {}).get("results") or [])
        except Exception as exc:  # sin internet, timeout, JSON inválido...
            log.warning("TMDb videos no responde (%s)", type(exc).__name__)
            return None

    @staticmethod
    def _elegir(videos: list) -> str | None:
        """El mejor tráiler de YouTube: oficial y de tipo Trailer primero."""
        yt = [v for v in videos if v.get("site") == "YouTube" and v.get("key")]
        if not yt:
            return None
        yt.sort(key=lambda v: (v.get("type") != "Trailer", not v.get("official", False)))
        return URL_YOUTUBE.format(clave=yt[0]["key"])

    def trailer(self, tmdb_id) -> str | None:
        """URL de YouTube del tráiler, o None (sin clave, sin red o sin tráiler)."""
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        cache = self._leer_videos()
        clave = str(tmdb_id)
        if clave in cache:
            return cache[clave].get("url")
        if not self.disponible:
            return None
        url = None
        for idioma in IDIOMAS_VIDEOS:
            videos = self._pedir_videos(tmdb_id, idioma)
            if videos is None:
                return None                         # red caída: no se guarda nada
            url = self._elegir(videos)
            if url:
                break
        cache[clave] = {"url": url, "consultado_en": int(time.time())}
        self._guardar_videos()
        return url


__all__ = ["TMDb", "ATRIBUCION_TMDB", "poster_svg", "color_genero", "svg_a_data_uri",
           "es_svg", "COLORES_GENERO"]
