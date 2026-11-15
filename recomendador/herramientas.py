"""Herramientas de datos que usan el chat LLM y la app (sin Spark, solo pandas/numpy).

Cada herramienta devuelve un dict serializable a JSON con datos REALES del sistema;
el LLM solo puede recomendar películas que salgan de ``recomendar``.

Herramientas
------------
- peliculas_calificadas(userId, n)      películas que calificó (historial + eventos en vivo)
- tags_usuario(userId)                  tags que escribió en MovieLens
- perfil_gustos(userId)                 tipo de espectador, géneros y tags del perfil
- recomendar(userId, n, max_duracion_min, generos, excluir_vistas=True)
                                        top-n del ALS (fold-in) con filtros
- info_pelicula(movieId)                ficha: título, año, géneros, duración, nota IMDb,
                                        sinopsis/póster de TMDb si están en la caché

Prioridad de fuentes: el estado en vivo (estado.db, escrito por el streaming) manda
sobre los artefactos precalculados: perfil en vivo, eventos nuevos, recomendaciones
vigentes y el puntero a los factores ALS reentrenados (meta 'item_factors').
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, estado, foldin, perfiles
from .datos import fecha_a_epoch
from .enriquecimiento import (NOMBRE_ENRIQUECIDAS, ClienteTMDb, separar_titulo_anio,
                              titulo_legible)

log = logging.getLogger(__name__)

N_DEFECTO = 5
N_MAXIMO = 20
# Géneros que el chat entiende como «liviano». MovieLens no tiene 'Family': su
# equivalente es 'Children', que se incluye para que el filtro funcione con los datos.
GENEROS_LIVIANOS = ["Comedy", "Animation", "Family", "Children"]
DURACION_CORTA_MIN = 100


# --------------------------------------------------------------------------- #
# Cargas cacheadas (se leen una vez por proceso; la app de Streamlit las reutiliza)
# --------------------------------------------------------------------------- #

def _mtime(ruta: Path) -> float:
    try:
        return ruta.stat().st_mtime
    except OSError:
        return 0.0


@lru_cache(maxsize=2)
def _ratings_ordenados(ruta: str, _mtime: float):
    """ratings.csv como arreglos numpy ordenados por userId (búsqueda binaria por usuario).

    Con 25M filas ocupa unos 300 MB y se lee en segundos con pyarrow.
    """
    import pyarrow.csv as pacsv

    tabla = pacsv.read_csv(ruta, convert_options=pacsv.ConvertOptions(
        include_columns=["userId", "movieId", "rating", "timestamp"]))
    usuarios = tabla["userId"].to_numpy().astype(np.int64)
    orden = np.argsort(usuarios, kind="stable")
    return (usuarios[orden],
            tabla["movieId"].to_numpy().astype(np.int64)[orden],
            tabla["rating"].to_numpy().astype(np.float32)[orden],
            tabla["timestamp"].to_numpy().astype(np.int64)[orden])


@lru_cache(maxsize=2)
def _tags(ruta: str, _mtime: float):
    import pandas as pd

    if not Path(ruta).exists():
        return pd.DataFrame(columns=["userId", "movieId", "tag", "timestamp"])
    return pd.read_csv(ruta, usecols=["userId", "movieId", "tag", "timestamp"])


@lru_cache(maxsize=2)
def _catalogo(ruta_movies: str, ruta_enriquecidas: str, _m1: float, _m2: float) -> dict:
    """{movieId: ficha} uniendo movies.csv con peliculas_enriquecidas.parquet (si existe)."""
    import pandas as pd

    movies = pd.read_csv(ruta_movies)
    fichas: dict[int, dict] = {}
    for m, t, g in zip(movies["movieId"], movies["title"], movies["genres"]):
        fichas[int(m)] = {"movieId": int(m), "titulo": titulo_legible(t),
                          "anio": separar_titulo_anio(t)[1],
                          "generos": [x for x in str(g).split("|") if x != "(no genres listed)"],
                          "duracion_min": None, "imdb_rating": None, "tmdbId": None}
    if ruta_enriquecidas and Path(ruta_enriquecidas).exists():
        df = pd.read_parquet(ruta_enriquecidas)
        for fila in df.to_dict("records"):
            ficha = fichas.get(int(fila["movieId"]))
            if ficha is None:
                continue
            for clave in ("anio", "duracion_min", "imdb_rating", "tmdbId"):
                v = fila.get(clave)
                if v is not None and not pd.isna(v):
                    ficha[clave] = v.item() if hasattr(v, "item") else v
    return fichas


@lru_cache(maxsize=4)
def _popularidad(ruta: str, _mtime: float, corte_epoch: int) -> list[int]:
    """movieIds por número de ratings anteriores al corte, de mayor a menor."""
    _, m, _, t = _ratings_ordenados(ruta, _mtime)
    ids, conteos = np.unique(m[t < corte_epoch], return_counts=True)
    return [int(x) for x in ids[np.argsort(-conteos, kind="stable")]]


def corte_por_defecto(datos) -> str:
    """Corte temporal que corresponde a una carpeta de datos.

    La muestra mini tiene su propio corte (FECHA_CORTE_MINI); cualquier otra carpeta
    usa config.FECHA_CORTE. No existe un camino «sin corte».
    """
    try:
        if Path(datos).resolve() == config.MUESTRA_MINI.resolve():
            return config.FECHA_CORTE_MINI
    except OSError:
        pass
    return config.FECHA_CORTE


@lru_cache(maxsize=4)
def _factores(ruta: str, _mtime: float):
    return foldin.cargar_item_factors(ruta)


# --------------------------------------------------------------------------- #
# Clase principal
# --------------------------------------------------------------------------- #

class Herramientas:
    """Acceso a los datos de un usuario para el chat y la app.

    datos:      carpeta de MovieLens (ratings.csv, movies.csv, tags.csv).
    artefactos: carpeta con item_factors.parquet, perfiles.parquet, tipos.json,
                metricas.json y (opcional) peliculas_enriquecidas.parquet.
    estado_db:  ruta de estado.db (estado en vivo). Si no existe, se usan solo
                los artefactos (no se crea el archivo).
    tmdb:       ClienteTMDb opcional; sin clave solo usa la caché local.
    corte:      fecha AAAA-MM-DD (fin exclusivo). Siempre hay un corte, en cualquier
                MODO: por defecto config.FECHA_CORTE, o config.FECHA_CORTE_MINI si
                ``datos`` es la muestra mini. Los ratings y tags de MovieLens con
                timestamp >= corte se ignoran (el modelo se entrenó solo hasta ahí);
                los eventos en vivo de estado.db se incluyen siempre.
    """

    def __init__(self, datos=None, artefactos=None, estado_db=None, tmdb=None, corte=None):
        self.datos = Path(datos) if datos else config.DATOS
        self.artefactos = Path(artefactos) if artefactos else config.artefactos_activos()
        self.estado_db = Path(estado_db) if estado_db else config.ESTADO_DB
        self.tmdb = tmdb if tmdb is not None else ClienteTMDb()
        self.corte = corte_por_defecto(self.datos) if not corte else str(corte)
        self.corte_epoch = fecha_a_epoch(self.corte)   # valida el formato AAAA-MM-DD

    # ------------------------------------------------------------------ #
    # Fuentes
    # ------------------------------------------------------------------ #
    def _con(self) -> sqlite3.Connection | None:
        """Conexión de solo lectura al estado en vivo, o None si no existe."""
        if not self.estado_db.exists():
            return None
        try:
            return estado.conectar(self.estado_db)
        except sqlite3.Error as exc:
            log.warning("No se pudo abrir %s: %s", self.estado_db, exc)
            return None

    def catalogo(self) -> dict:
        ruta_enr = self.artefactos / NOMBRE_ENRIQUECIDAS
        if not ruta_enr.exists() and (config.ARTEFACTOS_BASE / NOMBRE_ENRIQUECIDAS).exists():
            ruta_enr = config.ARTEFACTOS_BASE / NOMBRE_ENRIQUECIDAS   # modo demo
        movies = self.datos / "movies.csv"
        return _catalogo(str(movies), str(ruta_enr), _mtime(movies), _mtime(ruta_enr))

    def ficha(self, movie_id: int) -> dict | None:
        ficha = self.catalogo().get(int(movie_id))
        if ficha is None:
            return None
        ficha = dict(ficha)
        ficha["generos_es"] = [perfiles.GENEROS_ES.get(g, g) for g in ficha["generos"]]
        return ficha

    def _historial(self, userId: int) -> list[dict]:
        """Ratings de ratings.csv del usuario anteriores al corte: [{movieId, rating, timestamp}]."""
        ruta = self.datos / "ratings.csv"
        if not ruta.exists():
            return []
        u, m, r, t = _ratings_ordenados(str(ruta), _mtime(ruta))
        i, j = np.searchsorted(u, userId, "left"), np.searchsorted(u, userId, "right")
        return [{"movieId": int(m[k]), "rating": float(r[k]), "timestamp": int(t[k])}
                for k in range(i, j) if t[k] < self.corte_epoch]

    def _ratings_usuario(self, userId: int) -> dict[int, dict]:
        """{movieId: {rating, timestamp, fuente}}: historial + eventos en vivo (estos mandan)."""
        salida = {f["movieId"]: {**f, "fuente": "historial"} for f in self._historial(userId)}
        con = self._con()
        if con is not None:
            try:
                filas = con.execute("SELECT movieId, rating, timestamp FROM eventos "
                                    "WHERE userId=? ORDER BY timestamp", (userId,)).fetchall()
            finally:
                con.close()
            for mid, rating, ts in filas:
                salida[int(mid)] = {"movieId": int(mid), "rating": float(rating),
                                    "timestamp": int(ts), "fuente": "en vivo"}
        return salida

    def _factores_vigentes(self):
        """(ids, matriz, origen): factores del puntero en vivo o los precalculados."""
        ruta, origen = self.artefactos / "item_factors.parquet", "artefactos"
        con = self._con()
        if con is not None:
            try:
                puntero = estado.get_meta(con, "item_factors")
            finally:
                con.close()
            if puntero and Path(puntero).exists():
                ruta, origen = Path(puntero), "estado en vivo"
        if not ruta.exists():
            return None, None, None
        ids, matriz = _factores(str(ruta), _mtime(ruta))
        return ids, matriz, origen

    def _reg(self) -> float:
        try:
            datos = json.loads((self.artefactos / "metricas.json").read_text(encoding="utf-8"))
            return float(datos.get("params", {}).get("regParam", foldin.REG_DEFECTO))
        except (OSError, ValueError):
            return foldin.REG_DEFECTO

    def _popularidad(self) -> list[int]:
        """movieIds ordenados por número de ratings antes del corte (respaldo sin historial)."""
        ruta = self.datos / "ratings.csv"
        if not ruta.exists():
            return []
        return _popularidad(str(ruta), _mtime(ruta), self.corte_epoch)

    def existe_usuario(self, userId) -> bool:
        uid = _uid(userId)
        return uid is not None and bool(self._ratings_usuario(uid))

    def ultima_actualizacion(self, userId) -> float | None:
        """Epoch de la última actualización en vivo del usuario (o None)."""
        uid = _uid(userId)
        con = self._con()
        if con is None or uid is None:
            return None
        try:
            return estado.ultima_actualizacion(con, uid)
        finally:
            con.close()

    # ------------------------------------------------------------------ #
    # Herramientas
    # ------------------------------------------------------------------ #
    def peliculas_calificadas(self, userId, n: int = 20, orden: str = "rating") -> dict:
        """Películas que calificó el usuario (orden 'rating' = mejores primero, o 'reciente')."""
        uid = _uid(userId)
        if uid is None:
            return {"userId": userId, "existe": False, "error": "userId no válido"}
        ratings = self._ratings_usuario(uid)
        clave = (lambda f: (-f["timestamp"],)) if orden == "reciente" else \
            (lambda f: (-f["rating"], -f["timestamp"]))
        filas = sorted(ratings.values(), key=clave)[:_limitar(n, 200)]
        salida = []
        for f in filas:
            ficha = self.ficha(f["movieId"]) or {"titulo": f"movieId {f['movieId']}",
                                                 "anio": None, "generos": []}
            salida.append({"movieId": f["movieId"], "titulo": ficha["titulo"],
                           "anio": ficha["anio"], "generos": ficha["generos"],
                           "rating": f["rating"], "fuente": f["fuente"],
                           "fecha": time.strftime("%Y-%m-%d", time.gmtime(f["timestamp"]))})
        return {"userId": uid, "existe": bool(ratings), "total": len(ratings),
                "media": round(float(np.mean([f["rating"] for f in ratings.values()])), 3)
                if ratings else None,
                "peliculas": salida}

    def tags_usuario(self, userId, n: int = 30) -> dict:
        """Tags que escribió el usuario en MovieLens, con la película a la que los puso."""
        uid = _uid(userId)
        if uid is None:
            return {"userId": userId, "tags": [], "error": "userId no válido"}
        ruta = self.datos / "tags.csv"
        df = _tags(str(ruta), _mtime(ruta))
        df = df[(df["userId"] == uid) & (df["timestamp"] < self.corte_epoch)]
        df = df.sort_values("timestamp", ascending=False)
        tags = []
        for mid, tag in zip(df["movieId"].head(_limitar(n, 200)), df["tag"]):
            ficha = self.ficha(int(mid))
            tags.append({"tag": str(tag), "movieId": int(mid),
                         "titulo": ficha["titulo"] if ficha else None})
        return {"userId": uid, "total": int(len(df)), "tags": tags}

    def perfil_gustos(self, userId) -> dict:
        """Tipo de espectador y gustos (perfil en vivo de estado.db o el precalculado)."""
        uid = _uid(userId)
        if uid is None:
            return {"userId": userId, "existe": False, "error": "userId no válido"}
        perfil, fuente = None, None
        con = self._con()
        if con is not None:
            try:
                perfil = estado.leer_perfil(con, uid)
            finally:
                con.close()
            fuente = "estado en vivo" if perfil else None
        if perfil is None:
            try:
                perfil = perfiles.perfil_usuario(uid, self.artefactos)
                fuente = "artefactos" if perfil else None
            except FileNotFoundError:
                perfil = None
        if perfil is None:
            return {"userId": uid, "existe": False, "tipo": None,
                    "aviso": "El usuario no tiene perfil calculado"}
        pesos = perfil.get("pesos_generos") or {}
        if isinstance(pesos, list):          # formato ya resumido [{genero, peso}]
            pesos = {p["genero"]: p["peso"] for p in pesos if "genero" in p}
        top_pesos = sorted(((g, float(w)) for g, w in pesos.items()
                            if w and g not in perfiles.GENEROS_NO_NOMBRABLES),
                           key=lambda x: -x[1])[:8]
        return {
            "userId": uid, "existe": True, "fuente": fuente,
            "tipo": perfil.get("tipo"), "descripcion_tipo": perfil.get("descripcion_tipo"),
            "top_generos": list(perfil.get("top_generos") or []),
            "top_generos_es": list(perfil.get("top_generos_es") or []),
            "top_tags_es": list(perfil.get("top_tags_es") or [])[:8],
            "pesos_generos": [{"genero": g, "genero_es": perfiles.GENEROS_ES.get(g, g),
                               "peso": round(w, 4)} for g, w in top_pesos],
            "n_ratings": int(perfil.get("n_ratings") or 0),
            "media": round(float(perfil.get("media") or 0.0), 3),
        }

    def recomendar(self, userId, n: int = N_DEFECTO, max_duracion_min=None, generos=None,
                   excluir_vistas: bool = True) -> dict:
        """Top-n para el usuario con filtros opcionales.

        Orden de candidatos: primero el top-N vigente en estado.db (en vivo) y luego el
        ranking completo por fold-in con los factores vigentes y todos sus ratings
        (historial + eventos en vivo). Sin historial, se usa la popularidad.
        max_duracion_min: deja solo películas con duración IMDb conocida y <= este valor.
        generos: lista (o texto 'A|B') en inglés de MovieLens; basta con uno en común.
        """
        uid = _uid(userId)
        if uid is None:
            return {"userId": userId, "recomendaciones": [], "error": "userId no válido"}
        n = _limitar(n, N_MAXIMO)
        generos = _lista_generos(generos)
        try:
            max_dur = None if max_duracion_min in (None, "", 0) else float(max_duracion_min)
        except (TypeError, ValueError):
            max_dur = None
        ratings = self._ratings_usuario(uid)
        vistas = set(ratings) if excluir_vistas else set()
        avisos: list[str] = []

        candidatos: list[tuple[int, float | None, str]] = []
        con = self._con()
        if con is not None:
            try:
                for f in estado.leer_recomendaciones(con, uid):
                    candidatos.append((int(f["movieId"]), float(f["score"]), "estado en vivo"))
            finally:
                con.close()
        ids, matriz, origen = self._factores_vigentes()
        if ratings and ids is not None:
            orden = foldin.recomendar_foldin(
                (ids, matriz), [(m, f["rating"]) for m, f in ratings.items()],
                n=len(ids), reg=self._reg())
            candidatos += [(m, s, f"ALS fold-in ({origen})") for m, s in orden]
        if not candidatos:
            avisos.append("Usuario sin historial: se usa la popularidad")
            candidatos = [(m, None, "popularidad") for m in self._popularidad()]

        catalogo = self.catalogo()
        if max_dur is not None and not any(f.get("duracion_min") for f in catalogo.values()):
            avisos.append("No hay duraciones IMDb (corre scripts/descargar_imdb.py): "
                          "no se puede filtrar por duración")
        recs, usados = [], set()
        for mid, score, fuente in candidatos:
            if mid in usados or mid in vistas:
                continue
            usados.add(mid)
            ficha = self.ficha(mid)
            if ficha is None:
                continue
            if generos and not set(ficha["generos"]) & set(generos):
                continue
            if max_dur is not None:
                dur = ficha.get("duracion_min")
                if dur is None or dur > max_dur:
                    continue
            recs.append({**ficha, "rank": len(recs) + 1,
                         "score": None if score is None else round(float(score), 4),
                         "fuente": fuente})
            if len(recs) >= n:
                break
        if not recs:
            avisos.append("Ninguna película cumple los filtros pedidos")
        return {"userId": uid, "existe": bool(ratings),
                "filtros": {"n": n, "max_duracion_min": max_dur, "generos": generos,
                            "excluir_vistas": bool(excluir_vistas)},
                "recomendaciones": recs, "avisos": avisos}

    def info_pelicula(self, movieId) -> dict:
        """Ficha de la película (y sinopsis/póster de TMDb si hay clave o caché)."""
        mid = _uid(movieId)
        ficha = self.ficha(mid) if mid is not None else None
        if ficha is None:
            return {"movieId": movieId, "existe": False, "error": "Película no encontrada"}
        ficha["existe"] = True
        if ficha.get("tmdbId"):
            try:
                detalle = self.tmdb.detalle(ficha["tmdbId"])
            except Exception as exc:  # TMDb es opcional: nunca rompe la app
                log.warning("TMDb falló: %s", exc)
                detalle = None
            if detalle:
                ficha.update({"sinopsis": detalle.get("sinopsis"),
                              "poster_url": detalle.get("poster_url"),
                              "atribucion": detalle.get("atribucion")})
        return ficha

    # ------------------------------------------------------------------ #
    # Despacho para el LLM
    # ------------------------------------------------------------------ #
    def ejecutar(self, nombre: str, argumentos: dict | None) -> dict:
        """Ejecuta una herramienta por nombre; los errores vuelven como {'error': ...}."""
        funciones = {"peliculas_calificadas": self.peliculas_calificadas,
                     "tags_usuario": self.tags_usuario, "perfil_gustos": self.perfil_gustos,
                     "recomendar": self.recomendar, "info_pelicula": self.info_pelicula}
        funcion = funciones.get(nombre)
        if funcion is None:
            return {"error": f"Herramienta desconocida: {nombre}"}
        argumentos = dict(argumentos or {})
        if isinstance(argumentos.get("userId"), str) and argumentos["userId"].isdigit():
            argumentos["userId"] = int(argumentos["userId"])
        try:
            return funcion(**argumentos)
        except TypeError as exc:
            return {"error": f"Argumentos no válidos para {nombre}: {exc}"}
        except Exception as exc:  # una herramienta nunca rompe el chat
            log.exception("Falló la herramienta %s", nombre)
            return {"error": f"{type(exc).__name__}: {exc}"}


# --------------------------------------------------------------------------- #
# Esquemas de herramientas (formato de tool calling de Ollama / OpenAI)
# --------------------------------------------------------------------------- #

def _esquema(nombre: str, descripcion: str, propiedades: dict, requeridos: list[str]) -> dict:
    return {"type": "function", "function": {
        "name": nombre, "description": descripcion,
        "parameters": {"type": "object", "properties": propiedades, "required": requeridos}}}


_USER = {"userId": {"type": "integer", "description": "Id del usuario de MovieLens"}}

ESQUEMAS = [
    _esquema("peliculas_calificadas", "Películas que el usuario calificó, mejores primero.",
             {**_USER, "n": {"type": "integer", "description": "cuántas (máx. 200)"}},
             ["userId"]),
    _esquema("tags_usuario", "Tags que el usuario escribió sobre películas.", _USER, ["userId"]),
    _esquema("perfil_gustos", "Tipo de espectador y géneros/tags preferidos del usuario.",
             _USER, ["userId"]),
    _esquema("recomendar",
             "Recomendaciones ALS para el usuario. Únicas películas que se pueden recomendar.",
             {**_USER,
              "n": {"type": "integer", "description": "cuántas (máx. 20)"},
              "max_duracion_min": {"type": "integer",
                                   "description": "duración máxima en minutos (corto = 100)"},
              "generos": {"type": "array", "items": {"type": "string"},
                          "description": "géneros MovieLens en inglés, p. ej. Comedy, "
                                         "Animation, Children (liviano)"},
              "excluir_vistas": {"type": "boolean", "description": "excluir ya vistas"}},
             ["userId"]),
    _esquema("info_pelicula", "Ficha de una película: año, géneros, duración, nota IMDb.",
             {"movieId": {"type": "integer"}}, ["movieId"]),
]


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def _uid(valor) -> int | None:
    try:
        v = int(valor)
    except (TypeError, ValueError):
        return None
    return v if v >= 0 else None


def _limitar(n, maximo: int) -> int:
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = N_DEFECTO
    return max(1, min(n, maximo))


def _lista_generos(generos) -> list[str]:
    """Normaliza géneros a los nombres de MovieLens (acepta español o inglés)."""
    if not generos:
        return []
    if isinstance(generos, str):
        generos = generos.replace(",", "|").split("|")
    inverso = {v.lower(): k for k, v in perfiles.GENEROS_ES.items()}
    validos = {g.lower(): g for g in perfiles.GENEROS_ES}
    salida = []
    for g in generos:
        clave = str(g).strip().lower()
        if clave == "family":
            nombres = ["Family", "Children"]
        else:
            nombre = validos.get(clave) or inverso.get(clave)
            nombres = [nombre] if nombre else []
        salida += [x for x in nombres if x not in salida]
    return salida


__all__ = ["Herramientas", "ESQUEMAS", "GENEROS_LIVIANOS", "DURACION_CORTA_MIN"]
