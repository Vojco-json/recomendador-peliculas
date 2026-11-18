"""Póster, sinopsis y tráiler de TMDb para la app, con caché JSON y respaldo sin clave.

- Primero ``artefactos/<perfil>/app/tmdb.parquet`` (lo genera
  scripts/precalentar_tmdb.py): póster, título y sinopsis sin esperar a la red.
- Lo que falte, con ``TMDB_API_KEY``: póster y sinopsis vía ``enriquecimiento.ClienteTMDb``
  y tráiler de YouTube vía /movie/{id}/videos, ambos guardados en la caché SQLite segura
  entre hilos ``artefactos/tmdb_cache.sqlite``. Cada película se pide a la red una vez.
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
from .enriquecimiento import ATRIBUCION_TMDB, CACHE_TMDB, CacheSQLite, ClienteTMDb

log = logging.getLogger(__name__)

URL_VIDEOS_TMDB = "https://api.themoviedb.org/3/movie/{tmdb_id}/videos"
URL_YOUTUBE = "https://www.youtube.com/watch?v={clave}"
CACHE_VIDEOS = CACHE_TMDB                 # tráileres: espacio «videos» de la caché SQLite
NOMBRE_PARQUET = "tmdb.parquet"


def ruta_parquet_defecto() -> Path:
    return config.artefactos_activos() / "app" / NOMBRE_PARQUET


def leer_parquet(ruta) -> dict[int, dict]:
    """{tmdbId: ficha} desde tmdb.parquet ({} si no existe o está dañado)."""
    ruta = Path(ruta)
    if not ruta.exists():
        return {}
    try:
        import pandas as pd

        df = pd.read_parquet(ruta)
    except Exception as exc:  # parquet a medio escribir o dañado: se sigue con la red
        log.warning("No se pudo leer %s: %s", ruta, exc)
        return {}
    salida = {}
    for f in df.to_dict("records"):
        f = {k: (None if isinstance(v, float) and v != v else v) for k, v in f.items()}
        salida[int(f["tmdbId"])] = {
            "tmdbId": int(f["tmdbId"]), "titulo": f.get("titulo"),
            "sinopsis": f.get("sinopsis"), "idioma_sinopsis": f.get("idioma_sinopsis"),
            "poster_url": f.get("poster_url"), "atribucion": ATRIBUCION_TMDB}
    return salida
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
    cache:        caché de fichas (por defecto la SQLite de enriquecimiento; una ruta
                  .json usa el formato JSON antiguo).
    cache_videos: caché de tráileres (por defecto la misma SQLite, espacio «videos»).
    parquet:      tmdb.parquet precalentado (por defecto el de los artefactos activos).
    sesion:       objeto con ``get`` (requests o un doble de pruebas).
    """

    def __init__(self, api_key: str | None = None, cache=None, cache_videos=None,
                 sesion=None, timeout: float = 8.0, parquet=None):
        self.api_key = (api_key if api_key is not None
                        else os.environ.get("TMDB_API_KEY")) or None
        self.timeout = timeout
        self._sesion = sesion
        self.cliente = ClienteTMDb(api_key=self.api_key or "", cache=cache or CACHE_TMDB,
                                   timeout=timeout, sesion=sesion)
        self.cliente.api_key = self.api_key           # "" -> None: sin clave
        self.cache_videos = Path(cache_videos) if cache_videos else CACHE_VIDEOS
        self._videos = None
        # tmdb.parquet precalentado: por defecto solo en la configuración de producción
        # (sin caché explícita), para que las pruebas no dependan de artefactos reales.
        if parquet is None and cache is None:
            parquet = ruta_parquet_defecto()
        self.ruta_parquet = Path(parquet) if parquet else None
        self._parquet: dict | None = None
        self._cache_explicita = cache is not None

    def _sin_tmdb(self) -> bool:
        """True si no hay clave y se usa la caché por defecto: todo queda generado."""
        return not self.disponible and not self._cache_explicita

    def precalentado(self) -> dict[int, dict]:
        """Fichas de tmdb.parquet (se leen una vez por proceso)."""
        if self._parquet is None:
            self._parquet = leer_parquet(self.ruta_parquet) if self.ruta_parquet else {}
        return self._parquet

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    # ---- fichas -------------------------------------------------------- #
    def detalle(self, tmdb_id) -> dict | None:
        """Ficha de TMDb: primero tmdb.parquet, luego la caché SQLite y la red.

        Sin TMDB_API_KEY y con la caché por defecto (la de la app) no se usa nada de
        TMDb: la app muestra carátulas generadas, sin sinopsis ni tráiler (requisito
        48), aunque la caché compartida tenga fichas de una corrida anterior con clave.
        Una caché explícita (``cache=``) sí se reutiliza sin clave (uso sin red).
        """
        if self._sin_tmdb():
            return None
        try:
            dato = self.precalentado().get(int(tmdb_id))
            if dato is not None:
                return dato
        except (TypeError, ValueError):
            return None
        try:
            return self.cliente.detalle(tmdb_id)
        except Exception as exc:  # caché dañada, respuesta rara...
            log.warning("TMDb detalle falló (%s)", exc)
            return None

    def precargar(self, peliculas, hilos: int = 8) -> None:
        """Pide en paralelo las fichas que faltan (ni en tmdb.parquet ni en caché).

        Una grilla de 20 carátulas sin precalentar hacía 20 pedidos en serie (~10 s);
        en paralelo queda en lo que tarda el más lento. La caché SQLite es segura entre
        hilos, así que después cada carátula se dibuja sin esperar.
        """
        if not self.disponible:
            return
        try:
            previas = self.precalentado()
            ids = {int(p["tmdbId"]) for p in peliculas
                   if p.get("tmdbId") and int(p["tmdbId"]) not in previas}
        except (TypeError, ValueError, KeyError):
            return
        if len(ids) <= 1:
            return
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=min(hilos, len(ids))) as ex:
            list(ex.map(self.detalle, ids))

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
    def _leer_videos(self):
        if self._videos is None and self.cache_videos.suffix.lower() != ".json":
            self._videos = CacheSQLite(self.cache_videos, espacio="videos")
        if self._videos is None:
            try:
                self._videos = json.loads(self.cache_videos.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._videos = {}
        return self._videos

    def _guardar_videos(self) -> None:
        if isinstance(self._videos, CacheSQLite):
            return                              # SQLite ya escribió la fila
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
        if self._sin_tmdb() and self.cache_videos == CACHE_VIDEOS:
            return None                         # app sin clave: sin tráiler (requisito 48)
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
