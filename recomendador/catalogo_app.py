"""Capa de lectura de la app MoviePredict (solo lectura, con caché, SIN pyspark).

Lee lo que dejó la capa batch (``<artefactos>/app/*.parquet``, ver precalculo.py) y el
estado en vivo (``estado.db``: eventos y reseñas). Nunca importa pyspark: así cada
página carga rápido sin levantar una JVM.

Funciones principales (métodos de ``Catalogo``)
-----------------------------------------------
- buscar(consulta)          por título, director o actor, sin tildes ni mayúsculas
- ficha(movieId)            datos de la película + reparto + histograma + reseñas visibles
- filas_inicio(userId)      'Para ti' (fold-in), 'Populares entre espectadores como tú'
                            (tipo_usuario) y 'Porque te gustó X' (vecinos LSH)
- explicar(userId, movieId) veredicto con la película más parecida calificada alto y la
                            calificada baja, tags a favor/en contra y persona en común
- tarjeta_para_ti(userId)   una película probable y una improbable, ambas explicadas

Las calificaciones de un usuario son SIEMPRE las de ratings.csv anteriores al corte más
los eventos en vivo de estado.db (estos mandan si repite película).
"""

from __future__ import annotations

import difflib
import logging
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config, cuentas, estado, foldin, perfiles
from .herramientas import Herramientas

log = logging.getLogger(__name__)

CARPETA_APP = "app"          # igual que precalculo.CARPETA_APP (sin importar pyspark)
N_FILA = 12                  # películas por fila de carátulas
NOTA_ALTA = 4.0              # «calificada alto»
NOTA_BAJA = 2.5              # «calificada baja»
N_TAGS_EXPLICACION = 3

FILA_PARA_TI = "para_ti"
FILA_POPULARES_TIPO = "populares_tipo"
FILA_PORQUE = "porque_te_gusto"

PROBABLE = "Probablemente te guste"
IMPROBABLE = "Puede que no"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def normalizar(texto) -> str:
    """Minúsculas, sin tildes y con la puntuación como espacios («NOLÁN» -> «nolan»)."""
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^0-9a-z]+", " ", t)).strip()


def _mtime(ruta: Path) -> float:
    """Fecha de modificación (la de la carpeta part-* de Spark o del archivo)."""
    try:
        if ruta.is_dir():
            return max([p.stat().st_mtime for p in ruta.iterdir()] or [ruta.stat().st_mtime])
        return ruta.stat().st_mtime
    except OSError:
        return 0.0


def _lista(valor) -> list:
    """Arreglo de parquet (numpy/None) -> lista de Python."""
    if valor is None:
        return []
    try:
        return [v.item() if hasattr(v, "item") else v for v in list(valor)]
    except TypeError:
        return []


def _num(valor):
    """Escalar de pandas/numpy -> int/float de Python, o None si es nulo."""
    if valor is None:
        return None
    try:
        if np.isnan(valor):
            return None
    except TypeError:
        pass
    return valor.item() if hasattr(valor, "item") else valor


# --------------------------------------------------------------------------- #
# Cargas cacheadas (clave: ruta + fecha de modificación)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=4)
def _cargar_peliculas(ruta: str, _m: float) -> dict:
    """{movieId: ficha base} + índice de títulos normalizados para buscar."""
    import pandas as pd

    df = pd.read_parquet(ruta)
    fichas = {}
    for f in df.to_dict("records"):
        mid = int(f["movieId"])
        fichas[mid] = {
            "movieId": mid, "titulo": str(f["titulo"]), "anio": _num(f.get("anio")),
            "generos": _lista(f.get("generos")), "duracion_min": _num(f.get("duracion_min")),
            "imdb_rating": _num(f.get("imdb_rating")), "imdb_votos": _num(f.get("imdb_votos")),
            "tmdbId": _num(f.get("tmdbId")), "tconst": f.get("tconst"),
            "votos": int(_num(f.get("votos")) or 0), "promedio": _num(f.get("promedio")),
            "histograma": [int(x) for x in _lista(f.get("histograma"))],
            "tags": [str(t) for t in _lista(f.get("tags"))],
        }
    ids = list(fichas)
    titulos = pd.Series([normalizar(fichas[m]["titulo"]) for m in ids], index=ids)
    populares = sorted(ids, key=lambda m: (-fichas[m]["votos"], -(fichas[m]["promedio"] or 0), m))
    return {"fichas": fichas, "titulos_norm": titulos, "populares": populares}


@lru_cache(maxsize=4)
def _cargar_personas(ruta: str, _m: float) -> dict:
    """{movieId: {'directores', 'reparto'}} + tabla para buscar por nombre."""
    import pandas as pd

    if not Path(ruta).exists():
        df = pd.DataFrame(columns=["movieId", "nombre", "nombre_norm", "rol", "orden"])
    else:
        df = pd.read_parquet(ruta, columns=["movieId", "nombre", "nombre_norm", "rol", "orden"])
    df = df.sort_values(["movieId", "rol", "orden"])
    por_peli: dict[int, dict] = {}
    for (mid, rol), g in df.groupby(["movieId", "rol"], sort=False):
        d = por_peli.setdefault(int(mid), {"directores": [], "reparto": []})
        d["directores" if rol == "director" else "reparto"] = list(dict.fromkeys(g["nombre"]))
    return {"tabla": df.reset_index(drop=True), "por_pelicula": por_peli}


@lru_cache(maxsize=4)
def _cargar_vecinos(ruta: str, _m: float) -> dict[int, list[int]]:
    """{movieId: [vecinos ordenados por rank]} de vecinos.parquet (LSH sobre ALS)."""
    import pandas as pd

    if not Path(ruta).exists():
        return {}
    df = pd.read_parquet(ruta, columns=["movieId", "vecino", "rank"]).sort_values(
        ["movieId", "rank"])
    return {int(m): [int(v) for v in g["vecino"]] for m, g in df.groupby("movieId")}


@lru_cache(maxsize=4)
def _cargar_populares_tipo(ruta: str, _m: float) -> dict[int, dict]:
    """{cluster: {'tipo': nombre, 'peliculas': [movieId por rank]}}."""
    import pandas as pd

    if not Path(ruta).exists():
        return {}
    df = pd.read_parquet(ruta).sort_values(["cluster", "rank"])
    return {int(c): {"tipo": str(g["tipo"].iloc[0]), "peliculas": [int(m) for m in g["movieId"]]}
            for c, g in df.groupby("cluster")}


@lru_cache(maxsize=4)
def _factores_normalizados(ruta: str, _m: float):
    """(ids, matriz, matriz unitaria, índice movieId -> fila) de los factores ALS."""
    ids, matriz = foldin.cargar_item_factors(ruta)
    normas = np.linalg.norm(matriz, axis=1, keepdims=True)
    unit = matriz / np.where(normas > 0, normas, 1.0)
    return ids, matriz, unit, {int(m): i for i, m in enumerate(ids)}


# --------------------------------------------------------------------------- #
# Catálogo
# --------------------------------------------------------------------------- #

class Catalogo:
    """Acceso de solo lectura a lo precalculado y al estado en vivo.

    artefactos: carpeta del perfil (item_factors.parquet, perfiles.parquet, tipos.json,
                metricas.json y app/*.parquet).
    datos:      carpeta de MovieLens (ratings.csv para el historial antes del corte).
    estado_db:  estado en vivo (eventos y reseñas); si no existe se usa solo lo batch.
    cuentas_db: base de cuentas para mostrar el nombre del autor de cada reseña.
    corte:      fecha AAAA-MM-DD (por defecto la que corresponde a ``datos``).
    """

    def __init__(self, artefactos=None, datos=None, estado_db=None, corte=None,
                 cuentas_db=None):
        self.artefactos = Path(artefactos) if artefactos else config.artefactos_activos()
        self.datos = Path(datos) if datos else config.DATOS
        self.estado_db = Path(estado_db) if estado_db else config.ESTADO_DB
        self.cuentas_db = Path(cuentas_db) if cuentas_db else None
        # Herramientas ya resuelve historial + eventos en vivo y los factores vigentes.
        self._h = Herramientas(datos=self.datos, artefactos=self.artefactos,
                               estado_db=self.estado_db, corte=corte)
        self.corte = self._h.corte

    # ---- fuentes ------------------------------------------------------- #
    def _ruta_app(self, nombre: str) -> Path:
        return self.artefactos / CARPETA_APP / nombre

    def _peliculas(self) -> dict:
        ruta = self._ruta_app("peliculas.parquet")
        if not ruta.exists():
            raise FileNotFoundError(f"No existe {ruta}: corre scripts/precalcular_app.py")
        return _cargar_peliculas(str(ruta), _mtime(ruta))

    def _personas(self) -> dict:
        ruta = self._ruta_app("personas.parquet")
        return _cargar_personas(str(ruta), _mtime(ruta))

    def _vecinos(self) -> dict:
        ruta = self._ruta_app("vecinos.parquet")
        return _cargar_vecinos(str(ruta), _mtime(ruta))

    def _populares_tipo(self) -> dict:
        ruta = self._ruta_app("populares_por_tipo.parquet")
        return _cargar_populares_tipo(str(ruta), _mtime(ruta))

    def _factores(self):
        """Factores vigentes (puntero en vivo de estado.db o los precalculados)."""
        ruta = self.artefactos / "item_factors.parquet"
        con = self._h._con()
        if con is not None:
            try:
                puntero = estado.get_meta(con, "item_factors")
            finally:
                con.close()
            if puntero and Path(puntero).exists():
                ruta = Path(puntero)
        if not ruta.exists():
            return None
        return _factores_normalizados(str(ruta), _mtime(ruta))

    def ratings_usuario(self, userId: int) -> dict[int, float]:
        """{movieId: rating}: ratings.csv ANTES del corte + eventos en vivo (estos mandan)."""
        if userId is None:
            return {}
        return {m: float(d["rating"]) for m, d in self._h._ratings_usuario(int(userId)).items()}

    def ficha_corta(self, movieId: int) -> dict | None:
        """Lo mínimo para una carátula: id, título, año, géneros y tmdbId."""
        f = self._peliculas()["fichas"].get(int(movieId))
        if f is None:
            return None
        return {k: f[k] for k in ("movieId", "titulo", "anio", "generos", "tmdbId")}

    # ---- buscador ------------------------------------------------------ #
    def buscar(self, consulta: str, n: int = 20) -> list[dict]:
        """Búsqueda híbrida por título, director o actor (sin tildes ni mayúsculas).

        Orden: título exacto, título que empieza igual, título que lo contiene,
        director, actor y, si nada coincide, títulos parecidos (difflib). Dentro de
        cada grupo, las películas con más votos primero.
        """
        q = normalizar(consulta)
        if not q:
            return []
        pel = self._peliculas()
        fichas, titulos = pel["fichas"], pel["titulos_norm"]
        puntos: dict[int, tuple[int, str, str | None]] = {}

        def anotar(mid: int, p: int, como: str, persona: str | None = None):
            if mid in fichas and (mid not in puntos or puntos[mid][0] < p):
                puntos[mid] = (p, como, persona)

        # Títulos: coincidencia de texto (vectorizada con pandas).
        for mid in titulos.index[titulos.str.contains(q, regex=False)]:
            t = titulos[mid]
            anotar(int(mid), 100 if t == q else 80 if t.startswith(q) else 60, "título")
        # Todas las palabras de la consulta aparecen en el título (en cualquier orden).
        palabras = q.split()
        if len(palabras) > 1:
            for mid, t in titulos.items():
                if all(p in t.split() for p in palabras):
                    anotar(int(mid), 55, "título")
        # Personas: «nolan», «penelope cruz»...
        tabla = self._personas()["tabla"]
        if len(tabla):
            hits = tabla[tabla["nombre_norm"].fillna("").str.contains(q, regex=False)]
            for f in hits.itertuples():
                es_dir = f.rol == "director"
                anotar(int(f.movieId), 50 if es_dir else 45,
                       "director" if es_dir else "actor", str(f.nombre))
        # Respaldo con errores de tipeo: títulos parecidos.
        if not puntos:
            parecidos = set(difflib.get_close_matches(q, list(set(titulos)), n=n, cutoff=0.7))
            for mid, t in titulos.items():
                if t in parecidos:
                    anotar(int(mid), 20, "título parecido")

        orden = sorted(puntos, key=lambda m: (-puntos[m][0], -fichas[m]["votos"], m))[:n]
        return [{**self.ficha_corta(m), "coincidencia": puntos[m][1], "persona": puntos[m][2]}
                for m in orden]

    # ---- ficha --------------------------------------------------------- #
    def ficha(self, movieId: int, n_resenas: int = 50) -> dict | None:
        """Ficha completa estilo Letterboxd (sin TMDb: eso lo agrega tmdb.py)."""
        base = self._peliculas()["fichas"].get(int(movieId))
        if base is None:
            return None
        ficha = dict(base)
        ficha["generos_es"] = [perfiles.GENEROS_ES.get(g, g) for g in ficha["generos"]]
        ficha["tags_es"] = [perfiles.TAGS_ES.get(t, t) for t in ficha["tags"]]
        creditos = self._personas()["por_pelicula"].get(int(movieId), {})
        ficha["directores"] = list(creditos.get("directores", []))
        ficha["director"] = ", ".join(ficha["directores"]) or None
        ficha["reparto"] = list(creditos.get("reparto", []))
        ficha["resenas"] = self.resenas(movieId, n_resenas)
        return ficha

    def resenas(self, movieId: int, limite: int = 50) -> list[dict]:
        """Reseñas públicas (no ocultas) de estado.db, de la más nueva a la más vieja."""
        if not self.estado_db.exists():
            return []
        con = estado.conectar(self.estado_db)
        try:
            filas = estado.resenas_pelicula(con, int(movieId), limite)
        finally:
            con.close()
        for r in filas:
            r["autor"] = self._autor(r["userId"])
        return filas

    def _autor(self, userId: int) -> str:
        """Nombre de la cuenta (si hay cuentas.db) o «Usuario N»."""
        if self.cuentas_db is not None and self.cuentas_db.exists():
            try:
                nombre = cuentas.nombre_de(int(userId), self.cuentas_db)
                if nombre:
                    return nombre
            except Exception as exc:  # una base de cuentas dañada no rompe la ficha
                log.warning("No se pudo leer el autor %s: %s", userId, exc)
        return f"Usuario {userId}"

    # ---- tipo de espectador ------------------------------------------- #
    def tipo(self, userId: int, ratings: dict | None = None) -> dict | None:
        """Tipo de espectador vigente.

        - Usuario de MovieLens sin eventos en vivo: su cluster de perfiles.parquet.
        - Usuario nuevo (o con eventos): perfiles.tipo_usuario con sus calificaciones
          actuales (centroide más cercano con el escalado de tipos.json).
        """
        ratings = self.ratings_usuario(userId) if ratings is None else ratings
        if not ratings:
            return None
        en_vivo = self._h._con()
        hay_eventos = False
        if en_vivo is not None:
            try:
                hay_eventos = bool(estado.eventos_usuario(en_vivo, int(userId)))
            finally:
                en_vivo.close()
        if not hay_eventos:
            try:
                p = perfiles.perfil_usuario(int(userId), self.artefactos)
            except FileNotFoundError:
                p = None
            if p is not None:
                return {"cluster": p["cluster"], "tipo": p["tipo"], "n_ratings": len(ratings)}
        if not (self.artefactos / "tipos.json").exists():
            return None
        return perfiles.tipo_usuario(int(userId), self.artefactos, ratings=ratings,
                                     datos=self.datos)

    # ---- filas de inicio ---------------------------------------------- #
    def _con_votos(self, m: int) -> bool:
        """True si la película tiene >= min_votos antes del corte (candidata del batch)."""
        return self._h._votos().get(int(m), 0) >= self._h.min_votos

    def orden_para_ti(self, ratings: dict, excluir: set | None = None) -> list[tuple[int, float]]:
        """Orden del chat (Herramientas.orden_hibrido: z del fold-in + z de log(1+votos)).

        Solo películas del catálogo y no vistas. Primero las candidatas con >= min_votos
        antes del corte, en el orden híbrido; las de menos votos van al final como respaldo.
        """
        fac = self._factores()
        if fac is None or not ratings:
            return []
        ids, matriz, _, _ = fac
        fichas = self._peliculas()["fichas"]
        excluir = set(ratings) | set(excluir or ())
        orden = self._h.orden_hibrido({int(m): {"rating": float(r)} for m, r in ratings.items()},
                                      ids, matriz)
        orden = [(m, s) for m, s in orden if m in fichas and m not in excluir]
        return ([x for x in orden if self._con_votos(x[0])]
                + [x for x in orden if not self._con_votos(x[0])])

    def _para_ti(self, ratings: dict, n: int, excluir: set) -> list[tuple[int, float]]:
        """Top-n de la fila 'Para ti': el mismo orden híbrido del chat (sin vistas)."""
        return self.orden_para_ti(ratings, excluir)[:n]

    def filas_inicio(self, userId: int | None, n: int = N_FILA) -> list[dict]:
        """Filas de carátulas de la página de inicio.

        Cada fila: {'clave', 'titulo', 'peliculas': [ficha_corta], ...}. Sin
        calificaciones, 'Para ti' cae a las más votadas antes del corte.
        """
        pel = self._peliculas()
        ratings = self.ratings_usuario(userId) if userId is not None else {}
        vistas = set(ratings)
        filas = []

        # 1. Para ti (fold-in ALS con historial + eventos en vivo).
        recs = [m for m, _ in self._para_ti(ratings, n, vistas)]
        if not recs:
            recs = [m for m in pel["populares"] if m not in vistas][:n]
        filas.append({"clave": FILA_PARA_TI, "titulo": "Para ti",
                      "peliculas": [self.ficha_corta(m) for m in recs]})

        # 2. Populares entre espectadores como tú (agregación Spark por tipo).
        t = self.tipo(userId, ratings) if ratings else None
        if t is not None:
            grupo = self._populares_tipo().get(int(t["cluster"]), {"peliculas": []})
            ids = [m for m in grupo["peliculas"] if m not in vistas and m in pel["fichas"]][:n]
            filas.append({"clave": FILA_POPULARES_TIPO,
                          "titulo": "Populares entre espectadores como tú",
                          "cluster": int(t["cluster"]), "tipo": t["tipo"],
                          "peliculas": [self.ficha_corta(m) for m in ids]})

        # 3. Porque te gustó X (vecinos LSH de la mejor calificada con vecinos).
        semilla = self._semilla_porque(ratings)
        if semilla is not None:
            ids = [m for m in self._vecinos().get(semilla, [])
                   if m not in vistas and m in pel["fichas"]][:n]
            if ids:
                titulo = pel["fichas"][semilla]["titulo"]
                filas.append({"clave": FILA_PORQUE, "titulo": f"Porque te gustó {titulo}",
                              "semilla": semilla,
                              "peliculas": [self.ficha_corta(m) for m in ids]})
        return filas

    def _semilla_porque(self, ratings: dict) -> int | None:
        """La película mejor calificada (>= 4) que tenga vecinos; la última en empates."""
        vecinos = self._vecinos()
        orden = list(ratings.items())          # orden temporal: historial y luego en vivo
        candidatas = [(r, i, m) for i, (m, r) in enumerate(orden)
                      if r >= NOTA_ALTA and vecinos.get(m)]
        return max(candidatas)[2] if candidatas else None

    # ---- explicación «Para ti» ---------------------------------------- #
    def explicar(self, userId: int, movieId: int) -> dict | None:
        """Por qué (no) te podría gustar una película, con títulos y tags concretos.

        Usa solo las calificaciones de ``ratings_usuario`` (antes del corte + en vivo).
        Devuelve {'movieId', 'titulo', 'veredicto', 'probable', 'parecida_alta',
        'parecida_baja', 'tags_a_favor', 'tags_en_contra', 'persona_comun', 'texto'}.
        El texto no trae números de afinidad ni estrellas.
        """
        fichas = self._peliculas()["fichas"]
        destino = fichas.get(int(movieId))
        if destino is None:
            return None
        ratings = {m: r for m, r in self.ratings_usuario(userId).items() if m != int(movieId)}
        media = float(np.mean(list(ratings.values()))) if ratings else 3.0
        fac = self._factores()

        # Similitud (coseno de factores ALS) entre la película y cada calificada.
        sims: dict[int, float] = {}
        prediccion = umbral = None
        if fac is not None and ratings:
            ids, matriz, unit, idx = fac
            j = idx.get(int(movieId))
            if j is not None:
                for m in ratings:
                    if m in idx:
                        sims[m] = float(unit[idx[m]] @ unit[j])
                u = foldin.vector_usuario(ids, matriz, list(ratings.items()), self._h._reg())
                if u is not None:
                    prediccion = float(matriz[j] @ u)
                    # Umbral relativo al usuario: la mediana de su afinidad con las
                    # películas del catálogo que no ha visto.
                    filas = [i for m, i in idx.items() if m in fichas and m not in ratings]
                    umbral = float(np.median(matriz[filas] @ u)) if filas else media

        def mas_parecida(filtro) -> dict | None:
            cand = [m for m, r in ratings.items() if filtro(r) and m in fichas]
            if not cand:
                return None
            m = max(cand, key=lambda x: (sims.get(x, -2.0), ratings[x], -x))
            return {"movieId": m, "titulo": fichas[m]["titulo"], "rating": ratings[m]}

        alta = mas_parecida(lambda r: r >= max(NOTA_ALTA, media))
        baja = mas_parecida(lambda r: r <= min(NOTA_BAJA, media))

        # Tags de la película: suma de (rating - media) de las calificadas que los tienen.
        peso_tag: dict[str, float] = {}
        for m, r in ratings.items():
            for tag in fichas.get(m, {}).get("tags", []):
                if tag in destino["tags"]:
                    peso_tag[tag] = peso_tag.get(tag, 0.0) + (r - media)
        favor = [t for t, p in sorted(peso_tag.items(), key=lambda x: -x[1]) if p > 0]
        contra = [t for t, p in sorted(peso_tag.items(), key=lambda x: x[1]) if p < 0]
        favor, contra = favor[:N_TAGS_EXPLICACION], contra[:N_TAGS_EXPLICACION]

        persona = self._persona_comun(int(movieId), ratings, media)

        # Veredicto: afinidad del fold-in contra la mediana del usuario; sin factores,
        # el balance de tags y de la persona en común.
        if prediccion is not None:
            probable = prediccion >= umbral
        else:
            balance = sum(peso_tag.values()) + (persona["balance"] if persona else 0.0)
            probable = balance >= 0
        veredicto = PROBABLE if probable else IMPROBABLE
        es = lambda t: perfiles.TAGS_ES.get(t, t)  # noqa: E731
        salida = {
            "movieId": int(movieId), "titulo": destino["titulo"], "veredicto": veredicto,
            "probable": probable, "parecida_alta": alta, "parecida_baja": baja,
            "tags_a_favor": [es(t) for t in favor], "tags_en_contra": [es(t) for t in contra],
            "persona_comun": persona,
        }
        salida["texto"] = self._texto_explicacion(salida)
        return salida

    def _persona_comun(self, movieId: int, ratings: dict, media: float) -> dict | None:
        """Director o actor de la película que también está en lo que calificó."""
        por_peli = self._personas()["por_pelicula"]
        fichas = self._peliculas()["fichas"]
        creditos = por_peli.get(movieId)
        if not creditos or not ratings:
            return None
        mejor = None
        for rol, clave in (("director", "directores"), ("actor", "reparto")):
            for nombre in creditos.get(clave, []):
                vistas = [m for m in ratings if m in fichas and
                          nombre in por_peli.get(m, {}).get(clave, [])]
                if not vistas:
                    continue
                balance = float(sum(ratings[m] - media for m in vistas))
                # Prioridad: director antes que actor y luego la que más pesa (|balance|).
                clave_orden = (rol == "director", len(vistas), abs(balance))
                if mejor is None or clave_orden > mejor[0]:
                    mejor = (clave_orden, {
                        "nombre": nombre, "rol": rol, "balance": balance,
                        "le_gusto": balance >= 0,
                        "peliculas": [fichas[m]["titulo"] for m in
                                      sorted(vistas, key=lambda x: -ratings[x])]})
        return mejor[1] if mejor else None

    @staticmethod
    def _texto_explicacion(e: dict) -> str:
        """Texto breve con títulos y tags concretos (sin números ni estrellas)."""
        partes = [f"{e['veredicto']}."]
        alta, baja = e["parecida_alta"], e["parecida_baja"]
        if alta and baja:
            partes.append(f"Se parece a «{alta['titulo']}», que calificaste alto, "
                          f"y también a «{baja['titulo']}», que calificaste bajo.")
        elif alta:
            partes.append(f"Se parece a «{alta['titulo']}», que calificaste alto.")
        elif baja:
            partes.append(f"Se parece a «{baja['titulo']}», que calificaste bajo.")
        if e["tags_a_favor"]:
            partes.append("A favor: " + ", ".join(e["tags_a_favor"]) + ".")
        if e["tags_en_contra"]:
            partes.append("En contra: " + ", ".join(e["tags_en_contra"]) + ".")
        p = e["persona_comun"]
        if p:
            rol = "Dirige" if p["rol"] == "director" else "Actúa"
            gusto = "que te gustó" if p["le_gusto"] else "que no te convenció"
            partes.append(f"{rol} {p['nombre']}, como en «{p['peliculas'][0]}», {gusto}.")
        return " ".join(partes)

    def tarjeta_para_ti(self, userId: int, candidatas: int = 50) -> dict:
        """Tarjeta 'Para ti': una película probable y una improbable, ambas explicadas.

        - Probable: la primera del orden híbrido del chat (orden_para_ti), que ya pone
          primero las candidatas con >= min_votos antes del corte.
        - Improbable: solo entre las candidatas con >= min_votos (si ninguna película
          del catálogo llega al mínimo, como en la muestra mini, entre todas). Se toma
          el grupo de las ``candidatas`` de MENOR afinidad fold-in bajo la mediana del
          usuario (el mismo umbral que usa explicar(), así su veredicto es siempre
          «Puede que no») y, dentro del grupo, la más votada.
        """
        ratings = self.ratings_usuario(userId)
        fac = self._factores()
        fichas = self._peliculas()["fichas"]
        probable = improbable = None
        orden = self.orden_para_ti(ratings) if ratings else []
        if orden:
            probable = orden[0][0]
            ids, matriz, _, _ = fac
            u = foldin.vector_usuario(ids, matriz, list(ratings.items()), self._h._reg())
            if u is not None:
                scores = matriz @ u
                cand = [(float(s), int(m)) for m, s in zip(ids, scores)
                        if int(m) in fichas and int(m) not in ratings]
                hay_con_votos = any(self._con_votos(m) for m in fichas)
                if cand:
                    # Mismo umbral que explicar(): mediana de la afinidad con lo no visto.
                    umbral = float(np.median([s for s, _ in cand]))
                    pool = [x for x in cand if x[1] != probable
                            and (self._con_votos(x[1]) or not hay_con_votos)]
                    bajo = sorted((x for x in pool if x[0] < umbral),
                                  key=lambda x: (x[0], x[1]))[:max(1, int(candidatas))]
                    if bajo:
                        improbable = max(bajo, key=lambda x: (fichas[x[1]]["votos"],
                                                              -x[0], -x[1]))[1]
        if probable is None:   # sin calificaciones o sin factores: las más votadas
            orden = [m for m in self._peliculas()["populares"] if m not in ratings]
            probable = orden[0] if orden else None
        return {"probable": self.explicar(userId, probable) if probable else None,
                "improbable": self.explicar(userId, improbable) if improbable else None}


@lru_cache(maxsize=4)
def catalogo(artefactos=None, datos=None, estado_db=None, corte=None,
             cuentas_db=None) -> Catalogo:
    """Catálogo compartido por proceso (la app lo envuelve con st.cache_resource)."""
    return Catalogo(artefactos, datos, estado_db, corte, cuentas_db)


__all__ = ["Catalogo", "catalogo", "normalizar", "PROBABLE", "IMPROBABLE",
           "FILA_PARA_TI", "FILA_POPULARES_TIPO", "FILA_PORQUE"]
