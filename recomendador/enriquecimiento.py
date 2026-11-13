"""Enriquecimiento legal de las películas: IMDb (no comercial), TMDb (API v3) y Letterboxd.

Tres piezas independientes, sin scraping:

1. IMDb: une ``title.basics`` y ``title.ratings`` (datasets no comerciales oficiales de
   https://datasets.imdbws.com/) con ``links.csv`` de MovieLens. El ``imdbId`` de MovieLens
   se convierte a ``tconst`` como ``'tt' + 7 dígitos`` (p. ej. 114709 -> tt0114709).
   Resultado: ``peliculas_enriquecidas.parquet`` con
   (movieId, titulo, anio, generos, duracion_min, imdb_rating, imdb_votos, tmdbId).

2. TMDb: ``ClienteTMDb`` pide ``GET /3/movie/{tmdbId}`` con ``language=es-ES`` y, si falta
   la sinopsis o el póster, completa con ``en-US``. Las respuestas quedan en una caché
   JSON (``artefactos/tmdb_cache.json``). Sin ``TMDB_API_KEY`` devuelve None sin errores
   ni llamadas de red.

3. Letterboxd: lee el CSV de exportación (Date, Name, Year, Letterboxd URI, Rating),
   empareja con movies.csv por título normalizado y año (±1), y construye el perfil de
   gusto y las recomendaciones por fold-in de un usuario nuevo (userId >= 1.000.000).

Licencias: IMDb solo permite uso personal y no comercial; TMDb exige atribución
(ver ``ATRIBUCION_TMDB`` e ``ATRIBUCION_IMDB``).
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

URL_IMDB = "https://datasets.imdbws.com/"
ARCHIVOS_IMDB = ("title.basics.tsv.gz", "title.ratings.tsv.gz")
DIR_IMDB = config.RAIZ / "data" / "imdb"
NOMBRE_ENRIQUECIDAS = "peliculas_enriquecidas.parquet"
COLUMNAS_ENRIQUECIDAS = ["movieId", "titulo", "anio", "generos", "duracion_min",
                         "imdb_rating", "imdb_votos", "tmdbId"]

URL_TMDB = "https://api.themoviedb.org/3/movie/{tmdb_id}"
URL_POSTER_TMDB = "https://image.tmdb.org/t/p/w342{ruta}"
CACHE_TMDB = config.ARTEFACTOS / "tmdb_cache.json"
IDIOMAS_TMDB = ("es-ES", "en-US")

ATRIBUCION_TMDB = ("Este producto usa la API de TMDB, pero no está avalado ni certificado "
                   "por TMDB.")
ATRIBUCION_IMDB = ("Información cortesía de IMDb (https://www.imdb.com). "
                   "Usada con permiso, solo para uso personal y no comercial.")

COLUMNAS_LETTERBOXD = ("Date", "Name", "Year", "Letterboxd URI", "Rating")
USERID_MINIMO_LETTERBOXD = 1_000_000
DIR_LETTERBOXD = "letterboxd"            # subcarpeta dentro de los artefactos
N_RECS_LETTERBOXD = 10

# Artículos que MovieLens pospone al final del título ("Matrix, The").
ARTICULOS = ("the", "a", "an", "les", "le", "la", "l'", "el", "los", "las", "il", "lo",
             "der", "die", "das", "de", "het", "den")
# Artículos que se ignoran al comparar (en inglés, el idioma de ambos catálogos).
ARTICULOS_IGNORADOS = ("the", "a", "an")


# --------------------------------------------------------------------------- #
# Utilidades de títulos
# --------------------------------------------------------------------------- #

_RE_ANIO_FINAL = re.compile(r"\s*\((\d{4})(?:[-–]\d{0,4})?\)\s*$")
_RE_PARENTESIS = re.compile(r"\(([^()]*)\)")


def separar_titulo_anio(titulo_ml: str) -> tuple[str, int | None]:
    """'Toy Story (1995)' -> ('Toy Story', 1995). Sin año al final devuelve (titulo, None)."""
    texto = str(titulo_ml).strip()
    m = _RE_ANIO_FINAL.search(texto)
    if not m:
        return texto, None
    return texto[:m.start()].strip(), int(m.group(1))


def anteponer_articulo(titulo: str) -> str:
    """'Usual Suspects, The' -> 'The Usual Suspects' (como se muestra en Letterboxd e IMDb)."""
    t = titulo.strip()
    for art in ARTICULOS:
        sufijo = ", " + art
        if t.lower().endswith(sufijo):
            base = t[: -len(sufijo)].strip()
            articulo = t[-len(art):]
            articulo = articulo[0].upper() + articulo[1:]
            separador = "" if art.endswith("'") else " "
            return f"{articulo}{separador}{base}"
    return t


def normalizar_titulo(titulo: str) -> str:
    """Clave de comparación: sin tildes, minúsculas, sin puntuación ni artículo inicial.

    'The Usual Suspects' y 'Usual Suspects, The' -> 'usual suspects'
    'Amélie' -> 'amelie';  'Se7en' -> 'se7en';  'Pulp Fiction' -> 'pulp fiction'
    """
    t = anteponer_articulo(str(titulo))
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    t = t.replace("&", " and ")
    t = re.sub(r"[’`´']", "", t)                     # don't -> dont
    t = re.sub(r"[^0-9a-z]+", " ", t).strip()
    palabras = t.split()
    if len(palabras) > 1 and palabras[0] in ARTICULOS_IGNORADOS:
        palabras = palabras[1:]
    return " ".join(palabras)


def variantes_titulo(titulo_ml: str) -> tuple[set[str], int | None]:
    """Claves normalizadas de un título de MovieLens y su año.

    MovieLens agrega títulos alternativos entre paréntesis:
    'Twelve Monkeys (a.k.a. 12 Monkeys) (1995)' -> {'twelve monkeys', '12 monkeys'}, 1995
    'Amelie (Fabuleux destin d'Amélie Poulain, Le) (2001)' -> {'amelie',
    'fabuleux destin damelie poulain'}, 2001
    """
    sin_anio, anio = separar_titulo_anio(titulo_ml)
    alternativos = [re.sub(r"^(a\.k\.a\.|aka)\s*", "", a.strip(), flags=re.I)
                    for a in _RE_PARENTESIS.findall(sin_anio)]
    principal = _RE_PARENTESIS.sub(" ", sin_anio)
    claves = {normalizar_titulo(x) for x in [principal, *alternativos] if x.strip()}
    claves.discard("")
    return claves, anio


def titulo_legible(titulo_ml: str) -> str:
    """'Usual Suspects, The (1995)' -> 'The Usual Suspects' (para tarjetas y el chat)."""
    sin_anio, _ = separar_titulo_anio(titulo_ml)
    principal = _RE_PARENTESIS.sub(" ", sin_anio)
    principal = re.sub(r"\s+", " ", principal).strip() or sin_anio
    return anteponer_articulo(principal)


def imdb_a_tconst(imdb_id) -> str | None:
    """imdbId de links.csv (114709, '0114709' o 'tt0114709') -> 'tt0114709'.

    IMDb usa 'tt' + al menos 7 dígitos (los ids nuevos tienen 8).
    """
    if imdb_id is None:
        return None
    texto = str(imdb_id).strip().lower()
    if texto in ("", "nan", "none", "\\n"):
        return None
    texto = texto.removeprefix("tt")
    if texto.endswith(".0"):                 # leído como float por pandas
        texto = texto[:-2]
    if not texto.isdigit():
        return None
    return "tt" + texto.zfill(7)


# --------------------------------------------------------------------------- #
# 1. IMDb
# --------------------------------------------------------------------------- #

def _archivo_imdb(directorio: Path, base: str) -> Path:
    """Acepta 'title.basics.tsv.gz' (oficial) o 'title.basics.tsv' (muestra)."""
    for nombre in (f"{base}.tsv.gz", f"{base}.tsv"):
        if (directorio / nombre).exists():
            return directorio / nombre
    raise FileNotFoundError(
        f"Falta {base}.tsv(.gz) en {directorio}: corre scripts/descargar_imdb.py")


def _leer_tsv_imdb(ruta: Path, columnas: list[str], tconsts: set[str],
                   bloque: int = 1_000_000):
    """Lee un TSV de IMDb por bloques y conserva solo los tconst pedidos (ahorra RAM)."""
    import pandas as pd

    partes = []
    lector = pd.read_csv(ruta, sep="\t", usecols=columnas, dtype=str, na_values=["\\N"],
                         keep_default_na=False, quoting=csv.QUOTE_NONE, chunksize=bloque,
                         encoding="utf-8")
    for trozo in lector:
        partes.append(trozo[trozo["tconst"].isin(tconsts)])
    if not partes:
        return pd.DataFrame(columns=columnas)
    return pd.concat(partes, ignore_index=True)


def enriquecer_imdb(datos, imdb_dir=None, salida=None):
    """Une MovieLens (movies + links) con IMDb y devuelve un DataFrame de pandas.

    datos:    carpeta de MovieLens (ml-25m o muestras/ml-mini).
    imdb_dir: carpeta con title.basics y title.ratings (por defecto data/imdb).
    salida:   carpeta donde escribir ``peliculas_enriquecidas.parquet`` (opcional).

    Columnas: movieId, titulo, anio, generos, duracion_min, imdb_rating, imdb_votos,
    tmdbId. ``anio`` es el startYear de IMDb (o el año del título de MovieLens si falta).
    Las películas sin datos IMDb quedan con valores nulos (unión por la izquierda).
    """
    import pandas as pd

    datos = Path(datos)
    imdb_dir = Path(imdb_dir) if imdb_dir else DIR_IMDB
    movies = pd.read_csv(datos / "movies.csv")
    links = pd.read_csv(datos / "links.csv", dtype={"imdbId": str, "tmdbId": "Int64"})
    base = movies.merge(links, on="movieId", how="left")
    base["tconst"] = base["imdbId"].map(imdb_a_tconst)
    tconsts = set(base["tconst"].dropna())

    basics = _leer_tsv_imdb(_archivo_imdb(imdb_dir, "title.basics"),
                            ["tconst", "startYear", "runtimeMinutes"], tconsts)
    ratings = _leer_tsv_imdb(_archivo_imdb(imdb_dir, "title.ratings"),
                             ["tconst", "averageRating", "numVotes"], tconsts)
    base = base.merge(basics, on="tconst", how="left").merge(ratings, on="tconst", how="left")

    anio_ml = base["title"].map(lambda t: separar_titulo_anio(t)[1])
    anio_imdb = pd.to_numeric(base["startYear"], errors="coerce")
    resultado = pd.DataFrame({
        "movieId": base["movieId"].astype("int64"),
        "titulo": base["title"].map(titulo_legible),
        "anio": anio_imdb.fillna(pd.Series(anio_ml, dtype="float64")).astype("Int64"),
        "generos": base["genres"].astype(str),
        "duracion_min": pd.to_numeric(base["runtimeMinutes"], errors="coerce").astype("Int64"),
        "imdb_rating": pd.to_numeric(base["averageRating"], errors="coerce").astype("float64"),
        "imdb_votos": pd.to_numeric(base["numVotes"], errors="coerce").astype("Int64"),
        "tmdbId": base["tmdbId"].astype("Int64"),
    })[COLUMNAS_ENRIQUECIDAS]
    if salida is not None:
        salida = Path(salida)
        salida.mkdir(parents=True, exist_ok=True)
        resultado.to_parquet(salida / NOMBRE_ENRIQUECIDAS, index=False)
    return resultado


def resumen_enriquecimiento(df) -> dict:
    """Conteos para informar el cruce: total, con duración y con nota IMDb."""
    return {"peliculas": int(len(df)),
            "con_duracion": int(df["duracion_min"].notna().sum()),
            "con_nota_imdb": int(df["imdb_rating"].notna().sum())}


def descargar_imdb(destino=None, archivos=ARCHIVOS_IMDB, forzar: bool = False,
                   log_fn=print) -> list[Path]:
    """Descarga los datasets oficiales de IMDb (descarga directa, sin scraping)."""
    import requests

    destino = Path(destino) if destino else DIR_IMDB
    destino.mkdir(parents=True, exist_ok=True)
    rutas = []
    for nombre in archivos:
        ruta = destino / nombre
        if ruta.exists() and ruta.stat().st_size > 0 and not forzar:
            log_fn(f"Ya existe {ruta} ({ruta.stat().st_size / 1e6:,.1f} MB): se omite")
            rutas.append(ruta)
            continue
        url = URL_IMDB + nombre
        log_fn(f"Descargando {url} ...")
        temporal = ruta.with_suffix(ruta.suffix + ".parcial")
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            hecho, ultimo = 0, time.monotonic()
            with open(temporal, "wb") as f:
                for trozo in r.iter_content(chunk_size=1 << 20):
                    f.write(trozo)
                    hecho += len(trozo)
                    if time.monotonic() - ultimo > 5:
                        ultimo = time.monotonic()
                        avance = f" de {total / 1e6:,.0f}" if total else ""
                        log_fn(f"  {hecho / 1e6:,.0f}{avance} MB")
        temporal.replace(ruta)
        log_fn(f"  listo: {ruta} ({ruta.stat().st_size / 1e6:,.1f} MB)")
        rutas.append(ruta)
    return rutas


# --------------------------------------------------------------------------- #
# 2. TMDb
# --------------------------------------------------------------------------- #

class ClienteTMDb:
    """Póster y sinopsis desde la API v3 de TMDb, con caché local en JSON.

    - La clave se lee de ``TMDB_API_KEY`` (variable de entorno) si no se pasa ``api_key``.
      Acepta la clave v3 (se envía como ``api_key``) o el token de lectura v4 (empieza
      con 'eyJ' y se envía como ``Authorization: Bearer``).
    - Sin clave, ``detalle`` devuelve None sin errores ni llamadas de red.
    - Si la red falla, devuelve None y no guarda nada en la caché (se reintenta después).
    - Una película inexistente (404) se guarda en la caché como no encontrada.
    """

    def __init__(self, api_key: str | None = None, cache=None, timeout: float = 8.0,
                 sesion=None):
        import os

        self.api_key = (api_key if api_key is not None
                        else os.environ.get("TMDB_API_KEY")) or None
        self.cache_path = Path(cache) if cache else CACHE_TMDB
        self.timeout = timeout
        self._sesion = sesion
        self._cache: dict | None = None

    @property
    def disponible(self) -> bool:
        return bool(self.api_key)

    # ---- caché ---------------------------------------------------------- #
    def _leer_cache(self) -> dict:
        if self._cache is None:
            try:
                self._cache = json.loads(self.cache_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._cache = {}
        return self._cache

    def _guardar_cache(self) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        temporal = self.cache_path.with_suffix(".tmp")
        temporal.write_text(json.dumps(self._cache, ensure_ascii=False, indent=1),
                            encoding="utf-8")
        temporal.replace(self.cache_path)       # escritura atómica

    def en_cache(self, tmdb_id) -> dict | None:
        """Lo que haya en la caché (sin red y aunque no haya clave)."""
        dato = self._leer_cache().get(str(int(tmdb_id)))
        return None if not dato or dato.get("no_encontrada") else dato

    # ---- red ------------------------------------------------------------ #
    def _pedir(self, tmdb_id: int, idioma: str) -> dict | None:
        """Una llamada GET /3/movie/{id}. Devuelve el JSON, {} si es 404, None si falla."""
        import requests

        sesion = self._sesion or requests
        params = {"language": idioma}
        cabeceras = {"accept": "application/json"}
        if self.api_key.startswith("eyJ"):
            cabeceras["Authorization"] = f"Bearer {self.api_key}"
        else:
            params["api_key"] = self.api_key
        try:
            r = sesion.get(URL_TMDB.format(tmdb_id=tmdb_id), params=params,
                           headers=cabeceras, timeout=self.timeout)
        except Exception as exc:  # sin internet, DNS, timeout...
            log.warning("TMDb no responde (%s): se sigue sin póster", type(exc).__name__)
            return None
        if r.status_code == 404:
            return {}
        if r.status_code != 200:
            log.warning("TMDb respondió %s para la película %s", r.status_code, tmdb_id)
            return None
        try:
            return r.json()
        except ValueError:
            return None

    def detalle(self, tmdb_id) -> dict | None:
        """{tmdbId, titulo, sinopsis, poster_url, idioma_sinopsis, atribucion} o None."""
        if tmdb_id is None or (isinstance(tmdb_id, float) and np.isnan(tmdb_id)):
            return None
        try:
            tmdb_id = int(tmdb_id)
        except (TypeError, ValueError):
            return None
        cache = self._leer_cache()
        clave = str(tmdb_id)
        if clave in cache:
            return self.en_cache(tmdb_id)
        if not self.disponible:
            return None

        es = self._pedir(tmdb_id, IDIOMAS_TMDB[0])
        if es is None:
            return None
        if es == {}:
            cache[clave] = {"no_encontrada": True, "consultado_en": int(time.time())}
            self._guardar_cache()
            return None
        sinopsis, idioma = (es.get("overview") or "").strip(), IDIOMAS_TMDB[0]
        poster = es.get("poster_path")
        titulo = es.get("title")
        if not sinopsis or not poster:          # respaldo en inglés
            en = self._pedir(tmdb_id, IDIOMAS_TMDB[1]) or {}
            if not sinopsis and (en.get("overview") or "").strip():
                sinopsis, idioma = en["overview"].strip(), IDIOMAS_TMDB[1]
            poster = poster or en.get("poster_path")
            titulo = titulo or en.get("title")
        dato = {
            "tmdbId": tmdb_id,
            "titulo": titulo,
            "sinopsis": sinopsis or None,
            "idioma_sinopsis": idioma if sinopsis else None,
            "poster_url": URL_POSTER_TMDB.format(ruta=poster) if poster else None,
            "atribucion": ATRIBUCION_TMDB,
            "consultado_en": int(time.time()),
        }
        cache[clave] = dato
        self._guardar_cache()
        return dato


def detalle_tmdb(tmdb_id, cliente: ClienteTMDb | None = None) -> dict | None:
    """Atajo: detalle de TMDb con un cliente por defecto (None si no hay clave)."""
    return (cliente or ClienteTMDb()).detalle(tmdb_id)


# --------------------------------------------------------------------------- #
# 3. Letterboxd
# --------------------------------------------------------------------------- #

def leer_letterboxd(ruta) -> list[dict]:
    """Lee el CSV de exportación de Letterboxd y valida sus columnas.

    Devuelve [{fecha, titulo, anio, uri, rating}]; descarta filas sin nota.
    Lanza ValueError si faltan columnas o si no hay ninguna fila calificada.
    """
    ruta = Path(ruta)
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        lector = csv.DictReader(f)
        columnas = [c.strip() for c in (lector.fieldnames or [])]
        faltan = [c for c in COLUMNAS_LETTERBOXD if c not in columnas]
        if faltan:
            raise ValueError(
                f"El CSV {ruta.name} no tiene el formato de Letterboxd: faltan las columnas "
                f"{', '.join(faltan)} (se esperan {', '.join(COLUMNAS_LETTERBOXD)})")
        filas = []
        for fila in lector:
            fila = {(k or "").strip(): (v or "").strip() for k, v in fila.items()}
            try:
                rating = float(fila["Rating"].replace(",", "."))
            except ValueError:
                continue                       # vista sin calificar: no aporta al perfil
            if not 0.5 <= rating <= 5.0:
                continue
            anio = int(fila["Year"]) if fila["Year"].isdigit() else None
            filas.append({"fecha": fila["Date"], "titulo": fila["Name"], "anio": anio,
                          "uri": fila["Letterboxd URI"], "rating": round(rating * 2) / 2})
    if not filas:
        raise ValueError(f"El CSV {ruta.name} no tiene filas con calificación")
    return filas


def indice_titulos(movies) -> dict[str, list[tuple[int, int | None]]]:
    """Mapa título normalizado -> [(movieId, año)] desde movies.csv (DataFrame pandas)."""
    indice: dict[str, list[tuple[int, int | None]]] = {}
    for movie_id, titulo in zip(movies["movieId"], movies["title"]):
        claves, anio = variantes_titulo(titulo)
        for clave in claves:
            indice.setdefault(clave, []).append((int(movie_id), anio))
    return indice


def emparejar(filas: list[dict], movies, tolerancia: int = 1):
    """Empareja filas de Letterboxd con MovieLens por título normalizado y año (±tolerancia).

    Prefiere el año exacto; si hay varias candidatas a igual distancia, toma la de
    movieId menor (la más antigua del catálogo). Sin año, solo acepta una candidata única.
    Devuelve (emparejadas, no_emparejadas); cada emparejada agrega 'movieId'.
    """
    indice = indice_titulos(movies)
    emparejadas, sueltas = [], []
    for fila in filas:
        candidatas = indice.get(normalizar_titulo(fila["titulo"]), [])
        elegida = None
        if fila["anio"] is None:
            if len({m for m, _ in candidatas}) == 1:
                elegida = candidatas[0][0]
        else:
            validas = [(abs(a - fila["anio"]), m) for m, a in candidatas
                       if a is not None and abs(a - fila["anio"]) <= tolerancia]
            if validas:
                elegida = min(validas)[1]
        if elegida is None:
            sueltas.append(fila)
        else:
            emparejadas.append({**fila, "movieId": elegida})
    # Si la misma película aparece varias veces (re-vista), vale la calificación más reciente.
    ultima: dict[int, dict] = {}
    for fila in sorted(emparejadas, key=lambda f: f["fecha"]):
        ultima[fila["movieId"]] = fila
    return list(ultima.values()), sueltas


def fecha_a_timestamp(fecha: str) -> int:
    """'2024-03-15' -> epoch UTC (si no se entiende, la hora actual)."""
    try:
        return int(datetime.strptime(fecha[:10], "%Y-%m-%d")
                   .replace(tzinfo=timezone.utc).timestamp())
    except ValueError:
        return int(time.time())


def huella_csv(ruta) -> str:
    """SHA-256 del CSV: reimportar el mismo archivo reutiliza el mismo userId."""
    return hashlib.sha256(Path(ruta).read_bytes()).hexdigest()


def nuevo_user_id(datos, registro: dict) -> int:
    """max(userId de ratings.csv, usuarios ya importados, 999.999) + 1."""
    import pandas as pd

    maximo = USERID_MINIMO_LETTERBOXD - 1
    ruta = Path(datos) / "ratings.csv"
    if ruta.exists():
        for trozo in pd.read_csv(ruta, usecols=["userId"], chunksize=5_000_000):
            maximo = max(maximo, int(trozo["userId"].max()))
    for u in registro.get("usuarios", {}).values():
        maximo = max(maximo, int(u["userId"]))
    return maximo + 1


def _pesos_tags(ratings: list[tuple[int, float]], media: float, datos: Path,
                rasgos_tag: list[str]) -> tuple[dict[str, float], list[str]]:
    """Pesos de los tags genome igual que en perfiles.vectores_gusto (normalizados por
    la suma de |rating - media| de las películas que tienen genome)."""
    import pandas as pd

    nombres = [r.split(":", 1)[1] for r in rasgos_tag]
    if not nombres or not (datos / "genome-scores.csv").exists():
        return {t: 0.0 for t in nombres}, []
    tags = pd.read_csv(datos / "genome-tags.csv")
    tags["tag"] = tags["tag"].str.lower()
    id_por_tag = {t: int(i) for i, t in zip(tags["tagId"], tags["tag"]) if t in nombres}
    pelis = {m for m, _ in ratings}
    partes = [t[t["movieId"].isin(pelis) & t["tagId"].isin(id_por_tag.values())]
              for t in pd.read_csv(datos / "genome-scores.csv", chunksize=5_000_000)]
    scores = pd.concat(partes) if partes else pd.DataFrame(columns=["movieId", "tagId",
                                                                    "relevance"])
    rel = {(int(m), int(t)): float(r) for m, t, r in
           zip(scores["movieId"], scores["tagId"], scores["relevance"])}
    con_genome = set(scores["movieId"].astype(int))
    den = sum(abs(r - media) for m, r in ratings if m in con_genome)
    pesos = {}
    for nombre in nombres:
        tid = id_por_tag.get(nombre)
        num = sum((r - media) * rel.get((m, tid), 0.0) for m, r in ratings) if tid else 0.0
        pesos[nombre] = round(num / den, 6) if den > 0 else 0.0
    top = [t for t, w in sorted(pesos.items(), key=lambda x: (-x[1], x[0])) if w > 0][:3]
    return pesos, top


def perfil_letterboxd(user_id: int, ratings: list[tuple[int, float]], datos,
                      artefactos) -> dict:
    """Perfil de gusto del usuario importado, con el mismo cálculo que el perfilado batch.

    Géneros: sum(rating - media) por género / sum|rating - media| (exacto, vía
    perfiles.actualizar_perfil_generos). Tags genome: ídem con la relevancia.
    Tipo de espectador: el centroide más cercano de tipos.json (si existe).
    """
    from . import perfiles

    datos, artefactos = Path(datos), Path(artefactos)
    mapa = perfiles.generos_por_pelicula(datos)
    perfil = perfiles.actualizar_perfil_generos(None, ratings, mapa, userId=user_id)
    # El denominador incremental es aproximado; para un usuario nuevo se calcula exacto
    # y se recalculan los pesos (los numeradores ya son exactos).
    media = float(np.mean([r for _, r in ratings]))
    perfil["suma_abs"] = float(sum(abs(r - media) for _, r in ratings))
    perfil = perfiles.actualizar_perfil_generos(perfil, [], mapa)

    ruta_tipos = artefactos / "tipos.json"
    tipos = json.loads(ruta_tipos.read_text(encoding="utf-8")) if ruta_tipos.exists() else {}
    rasgos = tipos.get("rasgos", [])
    pesos_tags, top_tags = _pesos_tags(ratings, media, datos,
                                       [r for r in rasgos if r.startswith("tag:")])
    perfil["pesos_tags"] = pesos_tags
    perfil["top_tags"] = top_tags
    perfil["top_tags_es"] = [perfiles.TAGS_ES.get(t, t) for t in top_tags]
    if rasgos:
        vector = []
        for r in rasgos:
            tipo, nombre = (r.split(":", 1) + [""])[:2]
            if tipo == "genero":
                vector.append(perfil["pesos_generos"].get(nombre, 0.0))
            elif tipo == "tag":
                vector.append(pesos_tags.get(nombre, 0.0))
            else:                               # actividad = log(1 + n_ratings)
                vector.append(float(np.log1p(len(ratings))))
        asignado = perfiles.asignar_tipo(vector, artefactos)
        perfil["tipo"], perfil["cluster"] = asignado["tipo"], asignado["cluster"]
        for t in tipos.get("tipos", []):
            if t["cluster"] == asignado["cluster"]:
                perfil["descripcion_tipo"] = ("Destaca en "
                                              + " y ".join(x["etiqueta"] for x in t["rasgos"]))
    return perfil


def reg_param(artefactos) -> float:
    """regParam del ALS entrenado (metricas.json) o el valor por defecto del fold-in."""
    from .foldin import REG_DEFECTO

    try:
        datos = json.loads((Path(artefactos) / "metricas.json").read_text(encoding="utf-8"))
        return float(datos.get("params", {}).get("regParam", REG_DEFECTO))
    except (OSError, ValueError):
        return REG_DEFECTO


def fichas_peliculas(movie_ids, datos, artefactos) -> dict[int, dict]:
    """Título, año y géneros (y duración/nota IMDb si hay peliculas_enriquecidas.parquet)."""
    import pandas as pd

    ids = {int(m) for m in movie_ids}
    ruta = Path(artefactos) / NOMBRE_ENRIQUECIDAS
    if ruta.exists():
        df = pd.read_parquet(ruta)
        df = df[df["movieId"].isin(ids)]
        fichas = {}
        for fila in df.to_dict("records"):
            fichas[int(fila["movieId"])] = {
                k: (None if pd.isna(v) else (v.item() if hasattr(v, "item") else v))
                for k, v in fila.items()}
        return fichas
    movies = pd.read_csv(Path(datos) / "movies.csv")
    movies = movies[movies["movieId"].isin(ids)]
    return {int(m): {"movieId": int(m), "titulo": titulo_legible(t),
                     "anio": separar_titulo_anio(t)[1], "generos": g}
            for m, t, g in zip(movies["movieId"], movies["title"], movies["genres"])}


def importar_letterboxd(csv_path, datos, artefactos, n: int = N_RECS_LETTERBOXD,
                        user_id: int | None = None) -> dict:
    """Importa un CSV de Letterboxd como usuario nuevo y lo guarda en los artefactos.

    Requiere ``<artefactos>/item_factors.parquet`` (ALS). Escribe en
    ``<artefactos>/letterboxd/``:
      - ``usuario_<userId>.json``: emparejadas, no emparejadas, perfil y recomendaciones;
      - ``ratings_letterboxd.csv``: ratings en formato MovieLens (userId, movieId, rating,
        timestamp) de todos los usuarios importados, para un reentrenamiento futuro;
      - ``registro.json``: huella de cada CSV -> userId (reimportar no duplica usuarios).
    """
    import pandas as pd

    from .foldin import recomendar_foldin

    datos, artefactos = Path(datos), Path(artefactos)
    factores = artefactos / "item_factors.parquet"
    if not factores.exists():
        raise FileNotFoundError(f"No existe {factores}: entrena ALS primero")
    filas = leer_letterboxd(csv_path)
    movies = pd.read_csv(datos / "movies.csv")
    emparejadas, sueltas = emparejar(filas, movies)

    carpeta = artefactos / DIR_LETTERBOXD
    carpeta.mkdir(parents=True, exist_ok=True)
    ruta_registro = carpeta / "registro.json"
    registro = (json.loads(ruta_registro.read_text(encoding="utf-8"))
                if ruta_registro.exists() else {"usuarios": {}})
    huella = huella_csv(csv_path)
    if user_id is None:
        previo = registro["usuarios"].get(huella)
        user_id = int(previo["userId"]) if previo else nuevo_user_id(datos, registro)
    if user_id < USERID_MINIMO_LETTERBOXD:
        raise ValueError(f"El userId debe ser >= {USERID_MINIMO_LETTERBOXD:,}")

    ratings = [(f["movieId"], f["rating"]) for f in emparejadas]
    perfil = perfil_letterboxd(user_id, ratings, datos, artefactos) if ratings else None
    recs = recomendar_foldin(factores, ratings, n=n, reg=reg_param(artefactos)) if ratings \
        else []
    fichas = fichas_peliculas([m for m, _ in recs] + [m for m, _ in ratings], datos,
                              artefactos)
    recomendaciones = [{"rank": i, "movieId": m, "score": round(s, 4),
                        **{k: v for k, v in fichas.get(m, {}).items() if k != "movieId"}}
                       for i, (m, s) in enumerate(recs, 1)]

    resultado = {
        "userId": user_id,
        "origen": "letterboxd",
        "csv": Path(csv_path).name,
        "huella": huella,
        "importado_en": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": len(filas),
        "n_emparejadas": len(emparejadas),
        "emparejadas": [{"movieId": f["movieId"], "titulo_letterboxd": f["titulo"],
                         "anio": f["anio"], "rating": f["rating"], "fecha": f["fecha"],
                         "titulo": fichas.get(f["movieId"], {}).get("titulo")}
                        for f in emparejadas],
        "no_emparejadas": [{"titulo": f["titulo"], "anio": f["anio"], "uri": f["uri"]}
                           for f in sueltas],
        "perfil": perfil,
        "recomendaciones": recomendaciones,
    }
    (carpeta / f"usuario_{user_id}.json").write_text(
        json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")

    # ratings_letterboxd.csv: se reemplazan las filas previas de este usuario.
    ruta_ratings = carpeta / "ratings_letterboxd.csv"
    nuevas = pd.DataFrame([{"userId": user_id, "movieId": f["movieId"], "rating": f["rating"],
                            "timestamp": fecha_a_timestamp(f["fecha"])} for f in emparejadas],
                          columns=["userId", "movieId", "rating", "timestamp"])
    if ruta_ratings.exists():
        previas = pd.read_csv(ruta_ratings)
        nuevas = pd.concat([previas[previas["userId"] != user_id], nuevas], ignore_index=True)
    nuevas.to_csv(ruta_ratings, index=False)

    registro["usuarios"][huella] = {"userId": user_id, "csv": Path(csv_path).name,
                                    "emparejadas": len(emparejadas), "total": len(filas)}
    ruta_registro.write_text(json.dumps(registro, ensure_ascii=False, indent=2),
                             encoding="utf-8")
    return resultado


def guardar_en_estado(resultado: dict, ruta_db) -> None:
    """Copia el usuario importado al estado en vivo (SQLite) que lee la app."""
    from . import estado

    con = estado.conectar(ruta_db)
    try:
        uid = resultado["userId"]
        estado.guardar_eventos(con, [{"userId": uid, "movieId": f["movieId"],
                                      "rating": f["rating"],
                                      "timestamp": fecha_a_timestamp(f["fecha"])}
                                     for f in resultado["emparejadas"]], commit=False)
        if resultado["perfil"]:
            estado.guardar_perfil(con, resultado["perfil"], commit=False)
        estado.guardar_recomendaciones(con, uid, [(r["movieId"], r["score"])
                                                  for r in resultado["recomendaciones"]],
                                       commit=False)
        con.commit()
    finally:
        con.close()


__all__ = ["imdb_a_tconst", "normalizar_titulo", "variantes_titulo", "titulo_legible",
           "enriquecer_imdb", "descargar_imdb", "ClienteTMDb", "detalle_tmdb",
           "leer_letterboxd", "emparejar", "importar_letterboxd", "guardar_en_estado",
           "ATRIBUCION_TMDB", "ATRIBUCION_IMDB"]
