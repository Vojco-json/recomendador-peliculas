"""Herramientas de datos que usan el chat LLM y la app (sin Spark, solo pandas/numpy).

Cada herramienta devuelve un dict serializable a JSON con datos REALES del sistema;
el LLM solo puede recomendar películas que salgan de ``recomendar``.

Herramientas
------------
- peliculas_calificadas(userId, n)      películas que calificó (historial + eventos en vivo)
- tags_usuario(userId)                  tags que escribió en MovieLens
- perfil_gustos(userId)                 tipo de espectador, géneros y tags del perfil
- recomendar(userId, n, max_duracion_min, generos, tags, excluir_vistas=True)
                                        top-n por niveles de coincidencia y, dentro
                                        de cada nivel, por afinidad ALS (fold-in)
- buscar_pelicula(titulo)               búsqueda aproximada de títulos (ES/EN)
- info_pelicula(movieId)                ficha: título, año, géneros, duración, nota IMDb,
                                        sinopsis/póster de TMDb si están en la caché

Prioridad de fuentes: el estado en vivo (estado.db, escrito por el streaming) manda
sobre los artefactos precalculados: perfil en vivo, eventos nuevos, recomendaciones
vigentes y el puntero a los factores ALS reentrenados (meta 'item_factors').
"""

from __future__ import annotations

import itertools
import json
import logging
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, estado, foldin, perfiles, titulos
from .datos import fecha_a_epoch
from .enriquecimiento import (NOMBRE_ENRIQUECIDAS, ClienteTMDb, separar_titulo_anio,
                              titulo_legible)

log = logging.getLogger(__name__)

N_DEFECTO = 5
N_MAXIMO = 20
# Géneros que el chat entiende como «liviano». MovieLens no tiene 'Family': su
# equivalente es 'Children', que se incluye para que el filtro funcione con los datos.
GENEROS_LIVIANOS = ["Comedy", "Animation", "Family", "Children"]
MIN_VOTOS = 50        # votos antes del corte para ser candidato (igual que batch_als)
PESO_HIBRIDO = 0.5    # peso ALS del híbrido si metricas.json no lo trae
AFINIDAD_ALTA = 1.0   # puntaje híbrido (z) desde el que la afinidad se muestra «alta»
DURACION_CORTA_MIN = 100
# Relevancia genome mínima para que una película «tenga» un tag pedido.
UMBRAL_GENOME = 0.5


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


@lru_cache(maxsize=4)
def _votos(ruta: str, _mtime: float, corte_epoch: int) -> dict[int, int]:
    """{movieId: número de ratings anteriores al corte}."""
    _, m, _, t = _ratings_ordenados(ruta, _mtime)
    ids, conteos = np.unique(m[t < corte_epoch], return_counts=True)
    return {int(i): int(c) for i, c in zip(ids, conteos)}


@lru_cache(maxsize=8)
def _genome(ruta_tags: str, ruta_scores: str, tags: tuple[str, ...], _m1: float,
            _m2: float) -> dict[str, dict[int, float]]:
    """{tag: {movieId: relevancia}} solo para los tags pedidos (lee genome por partes,
    porque en 25M genome-scores.csv tiene ~15 millones de filas)."""
    import pandas as pd

    if not Path(ruta_tags).exists() or not Path(ruta_scores).exists():
        return {}
    gt = pd.read_csv(ruta_tags)
    ids = {int(i): str(t).lower() for i, t in zip(gt["tagId"], gt["tag"])
           if str(t).lower() in tags}
    salida: dict[str, dict[int, float]] = {t: {} for t in ids.values()}
    for parte in pd.read_csv(ruta_scores, chunksize=5_000_000):
        parte = parte[parte["tagId"].isin(list(ids))]
        for m, t, r in zip(parte["movieId"], parte["tagId"], parte["relevance"]):
            salida[ids[int(t)]][int(m)] = float(r)
    return salida


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

    def __init__(self, datos=None, artefactos=None, estado_db=None, tmdb=None, corte=None,
                 min_votos: int = MIN_VOTOS):
        self.min_votos = int(min_votos)
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

    def _votos(self) -> dict[int, int]:
        """{movieId: número de ratings antes del corte}."""
        ruta = self.datos / "ratings.csv"
        if not ruta.exists():
            return {}
        return _votos(str(ruta), _mtime(ruta), self.corte_epoch)

    def _peso_hibrido(self) -> float:
        """Peso ALS del híbrido elegido en la validación (metricas.json), o 0.5."""
        def buscar(d):
            if isinstance(d, dict):
                h = d.get("hibrido")
                if isinstance(h, dict) and "peso_als" in h:
                    return h["peso_als"]
                if isinstance(h, dict) and "peso" in h:
                    return h["peso"]
                for v in d.values():
                    r = buscar(v)
                    if r is not None:
                        return r
            return None
        try:
            datos = json.loads((self.artefactos / "metricas.json").read_text(encoding="utf-8"))
            peso = buscar(datos)
            return PESO_HIBRIDO if peso is None else float(peso)
        except (OSError, ValueError, TypeError):
            return PESO_HIBRIDO

    def orden_hibrido(self, ratings: dict, ids, matriz) -> list[tuple[int, float]]:
        """Orden híbrido original: peso·z(ALS fold-in) + (1-peso)·z(log(1+votos)).

        Es la misma fórmula de batch_als.recs_hibrido. Los z-scores se calculan sobre
        las películas con >= min_votos (los candidatos del batch); si ninguna llega al
        mínimo (muestra mini), sobre todas.
        """
        orden = foldin.recomendar_foldin(
            (ids, matriz), [(m, f["rating"]) for m, f in ratings.items()],
            n=len(ids), reg=self._reg())
        if not orden:
            return []
        votos = self._votos()
        mids = np.array([m for m, _ in orden], dtype=np.int64)
        als = np.array([s for _, s in orden], dtype=np.float64)
        pop = np.log1p(np.array([votos.get(int(m), 0) for m in mids], dtype=np.float64))
        base = np.array([votos.get(int(m), 0) >= self.min_votos for m in mids])
        if not base.any():
            base = np.ones(len(mids), dtype=bool)

        def z(x):
            return (x - x[base].mean()) / (x[base].std() + 1e-9)

        peso = self._peso_hibrido()
        hib = peso * z(als) + (1 - peso) * z(pop)
        idx = np.argsort(-hib, kind="stable")
        return [(int(mids[i]), float(hib[i])) for i in idx]

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
                   tags=None, excluir_vistas: bool = True, personas=None) -> dict:
        """Top-n para el usuario, ordenado por niveles de coincidencia con el pedido.

        Orden híbrido de candidatos: primero el top-N vigente en estado.db (en vivo) y
        luego el ranking completo por fold-in con los factores vigentes y todos sus
        ratings (historial + eventos en vivo). Sin historial, se usa la popularidad.

        Condiciones (ninguna descarta películas, solo ordenan):
        - generos: lista (o texto 'A|B') en inglés o español; 1 condición, se cumple con
          un género en común.
        - tags: tags del genome (p. ej. 'twist ending'); 1 condición por tag, se cumple
          con relevancia >= UMBRAL_GENOME.
        - max_duracion_min: 1 condición, duración IMDb <= este valor.
        - personas: directores o actores (nombres o dicts de ``pedido.detectar_personas``);
          1 condición por persona, se cumple si participa en la película.
        nivel = condiciones pedidas - incumplidas (lo no evaluable, como una duración
        sin dato, no baja el nivel). Se ordena por nivel de mayor a menor y, dentro de
        cada nivel, por el orden híbrido. Sin condiciones el orden es el híbrido exacto.
        La lista solo queda vacía si no hay películas sin ver en el catálogo.
        """
        uid = _uid(userId)
        if uid is None:
            return {"userId": userId, "recomendaciones": [], "error": "userId no válido"}
        n = _limitar(n, N_MAXIMO)
        generos = _lista_generos(generos)
        tags = _lista_tags(tags)
        try:
            max_dur = None if max_duracion_min in (None, "", 0) else float(max_duracion_min)
        except (TypeError, ValueError):
            max_dur = None
        ratings = self._ratings_usuario(uid)
        vistas = set(ratings) if excluir_vistas else set()
        avisos: list[str] = []
        votos = self._votos()

        # Candidatos en el orden híbrido original (ALS + popularidad, como recs_hibrido).
        # Solo entran las películas con >= min_votos antes del corte; las demás quedan
        # como último respaldo, después de todo lo anterior.
        candidatos: list[tuple[int, float | None, str]] = []
        poco_votadas: list[tuple[int, float | None, str]] = []
        con = self._con()
        if con is not None:
            try:
                for f in estado.leer_recomendaciones(con, uid):
                    candidatos.append((int(f["movieId"]), float(f["score"]), "estado en vivo"))
            finally:
                con.close()
        ids, matriz, origen = self._factores_vigentes()
        if ratings and ids is not None:
            candidatos += [(m, s, f"híbrido ALS + popularidad ({origen})")
                           for m, s in self.orden_hibrido(ratings, ids, matriz)]
        if not candidatos:
            avisos.append("Usuario sin historial: se usa la popularidad")
            candidatos = [(m, None, "popularidad") for m in self._popularidad()]
        poco_votadas = [c for c in candidatos if votos.get(c[0], 0) < self.min_votos]
        candidatos = [c for c in candidatos if votos.get(c[0], 0) >= self.min_votos]

        catalogo = self.catalogo()
        if max_dur is not None and not any(f.get("duracion_min") for f in catalogo.values()):
            avisos.append("No hay duraciones IMDb (corre scripts/descargar_imdb.py): "
                          "no se puede filtrar por duración")
        genome = self._genome(tags) if tags else {}
        grupos = _grupos_generos(generos)
        gente = self._personas_pedidas(personas)
        n_cond = len(grupos) + len(tags) + int(max_dur is not None) + len(gente)

        # Recorre los candidatos en el orden híbrido y evalúa cada condición. El orden
        # de llegada ES la afinidad. Respaldo para que la lista nunca quede vacía: las
        # poco votadas, la popularidad y el catálogo.
        def respaldo():
            yield from poco_votadas
            yield from ((m, None, "popularidad") for m in self._popularidad())
            yield from ((m, None, "catálogo") for m in catalogo)

        evaluadas: list[tuple[int, int, float | None, str, dict]] = []
        usados: set[int] = set()
        en_maximo = 0
        for mid, score, fuente in itertools.chain(candidatos, respaldo()):
            if mid in usados or mid in vistas or mid not in catalogo:
                continue
            usados.add(mid)
            ev = _evaluar(catalogo[mid], grupos, tags, genome, max_dur)
            for p in gente:   # la persona es una condición más del orden por niveles
                (ev["cumple"] if mid in p["ids"] else ev["no_cumple"]).append(p["texto"])
            # Nivel = condiciones pedidas - incumplidas - tags sin datos genome (cuentan
            # como no cumplidos). Solo la duración sin dato IMDb queda neutral.
            nivel = n_cond - len(ev["no_cumple"]) - sum(
                c.startswith("tag") for c in ev["no_evaluables"])
            evaluadas.append((nivel, mid, score, fuente, ev))
            # Con n películas en el nivel máximo (o sin condiciones, con n películas) no
            # hace falta seguir: nada posterior puede adelantarlas.
            en_maximo += nivel == n_cond
            if en_maximo >= n:
                break
        # Orden estable: primero todas las bien votadas (>= min_votos) y recién después
        # las poco votadas (último respaldo); dentro de cada bloque, nivel de mayor a
        # menor y, dentro del nivel, el orden ALS de llegada.
        evaluadas.sort(key=lambda e: (votos.get(e[1], 0) < self.min_votos, -e[0]))

        recs = []
        for nivel, mid, score, fuente, ev in evaluadas[:n]:
            recs.append({**self.ficha(mid), "rank": len(recs) + 1,
                         "score": None if score is None else round(float(score), 4),
                         "afinidad": _afinidad(score), "votos": int(votos.get(mid, 0)),
                         "fuente": fuente, "nivel": nivel, "condiciones_pedidas": n_cond,
                         "coincidencia": 1.0 if not n_cond else round(nivel / n_cond, 3),
                         **ev})
        if n_cond and recs and recs[0]["nivel"] < n_cond:
            avisos.append("Ninguna película cumple todas las condiciones: van primero las "
                          "que cumplen más")
        if any("duración" in c for r in recs for c in r["no_evaluables"]):
            avisos.append("Algunas películas no tienen duración en IMDb: esa condición no se "
                          "pudo evaluar y no baja su nivel")
        if any(c.startswith("tag") for r in recs for c in r["no_evaluables"]):
            avisos.append("Algunas películas no tienen datos del tag genome: esa condición "
                          "no se pudo evaluar y cuenta como no cumplida para el orden")
        return {"userId": uid, "existe": bool(ratings),
                "filtros": {"n": n, "max_duracion_min": max_dur, "generos": generos,
                            "tags": tags, "personas": [p["nombre"] for p in gente],
                            "excluir_vistas": bool(excluir_vistas)},
                "recomendaciones": recs, "avisos": avisos}

    def _personas_pedidas(self, personas) -> list[dict]:
        """Normaliza ``personas``: [{'nombre', 'texto', 'ids'}]. Acepta los dicts de
        pedido.py (con movieIds) o nombres sueltos, que se buscan en personas.parquet."""
        from . import pedido

        salida = []
        for p in personas or []:
            if isinstance(p, dict) and "movieIds" in p:
                salida.append({"nombre": p["nombre"], "texto": p.get("texto") or p["nombre"],
                               "ids": {int(m) for m in p["movieIds"]}})
                continue
            nombre = p.get("nombre") if isinstance(p, dict) else p
            if not nombre:
                continue
            hallada, _ = pedido.detectar_personas(
                "de " + pedido.normalizar(nombre),
                pedido.cargar_personas(self.artefactos / "app" / "personas.parquet"))
            for h in hallada[:1]:
                salida.append({"nombre": h["nombre"], "texto": h["texto"],
                               "ids": set(h["movieIds"])})
        return salida

    def _genome(self, tags: list[str]) -> dict[str, dict[int, float]]:
        """Relevancias genome de los tags pedidos (vacío si la carpeta no trae genome)."""
        rt, rs = self.datos / "genome-tags.csv", self.datos / "genome-scores.csv"
        return _genome(str(rt), str(rs), tuple(sorted(tags)), _mtime(rt), _mtime(rs))

    def buscar_pelicula(self, titulo) -> dict:
        """Busca un título (español o inglés, sin importar tildes ni mayúsculas, con
        errores de tipeo) en MovieLens y, si hay clave TMDb, en sus títulos en español."""
        r = titulos.buscar(str(titulo or ""), self.datos, self.tmdb)
        if not r.get("encontrada"):
            return {"encontrada": False, "consulta": r.get("consulta", titulo),
                    "mensaje": r.get("mensaje") or "No encontré esa película en el catálogo."}
        ficha = self.ficha(r["movieId"]) or {}
        salida = {"encontrada": True, "movieId": r["movieId"],
                  "titulo": ficha.get("titulo") or r["titulo_legible"],
                  "anio": r.get("anio"), "generos": ficha.get("generos", []),
                  "coincidencia": r["coincidencia"], "fuente": r["fuente"]}
        tmdb_id = ficha.get("tmdbId") or r.get("tmdbId")
        if tmdb_id and getattr(self.tmdb, "disponible", False):
            try:
                titulo_es = self.tmdb.titulos_es().get(int(tmdb_id))
            except Exception as exc:  # TMDb es opcional: nunca rompe la búsqueda
                log.warning("TMDb falló: %s", exc)
                titulo_es = None
            if titulo_es:
                salida["titulo_es"] = titulo_es
        return salida

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
                     "recomendar": self.recomendar, "info_pelicula": self.info_pelicula,
                     "buscar_pelicula": self.buscar_pelicula}
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
              "tags": {"type": "array", "items": {"type": "string"},
                       "description": "tags del genome en inglés, p. ej. 'twist ending', "
                                      "'feel-good', 'sad'"},
              "excluir_vistas": {"type": "boolean", "description": "excluir ya vistas"}},
             ["userId"]),
    _esquema("info_pelicula", "Ficha de una película: año, géneros, duración, nota IMDb.",
             {"movieId": {"type": "integer"}}, ["movieId"]),
    _esquema("buscar_pelicula",
             "Busca una película por título (español o inglés; ignora tildes, mayúsculas "
             "y errores de tipeo). Devuelve movieId y título, o encontrada=false.",
             {"titulo": {"type": "string", "description": "título tal como lo escribió "
                                                          "el usuario"}},
             ["titulo"]),
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


def _lista_tags(tags) -> list[str]:
    """Tags genome en minúsculas, sin repetir (acepta lista o texto 'a|b' / 'a,b')."""
    if not tags:
        return []
    if isinstance(tags, str):
        tags = tags.replace(",", "|").split("|")
    salida = []
    for t in tags:
        t = str(t).strip().lower()
        if t and t not in salida:
            salida.append(t)
    return salida


def _grupos_generos(generos: list[str]) -> list[list[str]]:
    """Una condición por género pedido ('drama policial' = Drama Y Crime).

    Excepciones que valen como UNA condición cumplida con cualquiera de sus géneros:
    Family/Children (MovieLens usa Children) y, si vienen los cuatro, el grupo
    «liviano» (Comedy/Animation/Family/Children).
    """
    restantes = list(generos)
    grupos: list[list[str]] = []
    if all(g in restantes for g in GENEROS_LIVIANOS):
        grupos.append(list(GENEROS_LIVIANOS))
        restantes = [g for g in restantes if g not in GENEROS_LIVIANOS]
    familia = [g for g in restantes if g in ("Family", "Children")]
    if familia:
        grupos.append(familia)
    grupos += [[g] for g in restantes if g not in ("Family", "Children")]
    return grupos


def _afinidad(score) -> str | None:
    """Texto de afinidad para mostrar en vez del número: 'alta' o 'media'."""
    if score is None:
        return None
    return "afinidad alta" if float(score) >= AFINIDAD_ALTA else "afinidad media"


def _evaluar(ficha: dict, grupos: list[list[str]], tags: list[str],
             genome: dict[str, dict[int, float]], max_dur: float | None) -> dict:
    """Clasifica cada condición pedida en cumple / no_cumple / no_evaluables (textos).

    grupos: una lista por condición de género (se cumple con cualquiera del grupo)."""
    cumple, no_cumple, no_eval = [], [], []
    for grupo in grupos:
        visibles = [g for g in grupo if g != "Family"] or grupo
        texto = "género: " + "/".join(perfiles.GENEROS_ES.get(g, g) for g in visibles)
        (cumple if any(g in ficha["generos"] for g in grupo) else no_cumple).append(texto)
    for t in tags:
        relevancia = genome.get(t, {}).get(ficha["movieId"])
        if relevancia is None:
            no_eval.append(f"tag: {t}")
        elif relevancia >= UMBRAL_GENOME:
            cumple.append(f"tag: {t}")
        else:
            no_cumple.append(f"tag: {t}")
    if max_dur is not None:
        texto = f"duración ≤ {max_dur:g} min"
        dur = ficha.get("duracion_min")
        if dur is None:
            no_eval.append(texto)
        elif dur <= max_dur:
            cumple.append(texto)
        else:
            no_cumple.append(texto)
    return {"cumple": cumple, "no_cumple": no_cumple, "no_evaluables": no_eval}


__all__ = ["Herramientas", "ESQUEMAS", "GENEROS_LIVIANOS", "DURACION_CORTA_MIN"]
