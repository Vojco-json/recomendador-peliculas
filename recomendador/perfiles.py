"""Perfiles de gusto por usuario y «tipos de espectador» con KMeans de MLlib.

Flujo (lo usa scripts/perfilar_usuarios.py):

1. ``peso = rating - media del usuario``: una película gusta más o menos que el
   promedio de ese mismo usuario (corrige a quienes califican todo alto o bajo).
2. Vector de géneros (20 géneros de MovieLens): suma de pesos de las películas de
   cada género, normalizada por la suma de |peso| del usuario (queda en [-1, 1]).
3. Vector genome (unos 40 tags interpretables): suma de ``peso * relevancia`` por
   tag, normalizada por la suma de |peso| de las películas que tienen genome.
4. Features = géneros + tags + log(1 + n_ratings), estandarizadas (StandardScaler)
   y agrupadas con KMeans (k configurable, semilla fija).
5. Cada cluster se nombra en español con los 2 rasgos de mayor desviación positiva
   de su centroide (en unidades estandarizadas), sin repetir nombres.

Todo el cálculo pesado corre en la JVM de Spark (sin UDF de Python, que fallan en
Windows); al final se traen a pandas solo las columnas escalares por usuario.

Salidas: ``perfiles.parquet`` (userId, top_generos, top_tags, cluster, tipo y los
pesos para la actualización incremental) y ``tipos.json``.
"""

from __future__ import annotations

import json
import time
import unicodedata
from functools import lru_cache
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Diccionarios de rasgos y su traducción al español
# --------------------------------------------------------------------------- #

# Los 20 valores de la columna genres de ml-25m (incluido «sin género»).
GENEROS_ES: dict[str, str] = {
    "Action": "acción",
    "Adventure": "aventura",
    "Animation": "animación",
    "Children": "cine infantil",
    "Comedy": "comedia",
    "Crime": "crimen",
    "Documentary": "documental",
    "Drama": "drama",
    "Fantasy": "fantasía",
    "Film-Noir": "cine negro",
    "Horror": "terror",
    "IMAX": "IMAX",
    "Musical": "musicales",
    "Mystery": "misterio",
    "Romance": "romance",
    "Sci-Fi": "ciencia ficción",
    "Thriller": "suspenso",
    "War": "bélico",
    "Western": "western",
    "(no genres listed)": "sin género",
}
GENEROS: list[str] = list(GENEROS_ES)
# Rasgos que no sirven para nombrar ni para el «top» de un usuario.
GENEROS_NO_NOMBRABLES = {"(no genres listed)", "IMAX"}

# Unos 40 tags del tag genome de ml-25m que se entienden sin contexto. Se buscan sin
# distinguir mayúsculas; los que no existan en los datos se omiten con un aviso.
TAGS_ES: dict[str, str] = {
    "action": "acción",
    "romance": "romance",
    "funny": "humor",
    "dark": "tono oscuro",
    "animation": "animación",
    "classic": "clásicos",
    "twist ending": "giros finales",
    "family": "familia",
    "violence": "violencia",
    "feel-good": "optimismo",
    "atmospheric": "atmósfera",
    "thought-provoking": "reflexión",
    "visually appealing": "estética visual",
    "dialogue": "diálogos",
    "superhero": "superhéroes",
    "dystopia": "distopías",
    "time travel": "viajes en el tiempo",
    "space": "espacio",
    "zombies": "zombis",
    "world war ii": "Segunda Guerra Mundial",
    "historical": "historia",
    "true story": "historias reales",
    "biography": "biografías",
    "music": "música",
    "dark comedy": "comedia negra",
    "satire": "sátira",
    "mafia": "mafia",
    "heist": "atracos",
    "serial killer": "asesinos en serie",
    "suspense": "intriga",
    "surreal": "surrealismo",
    "cult film": "cine de culto",
    "quirky": "excentricidad",
    "philosophical": "filosofía",
    "psychological": "psicología",
    "magic": "magia",
    "tearjerker": "lágrimas",
    "coming of age": "crecer",
    "friendship": "amistad",
    "pixar animation": "Pixar",
    "great soundtrack": "bandas sonoras",
    "oscar (best picture)": "ganadoras del Óscar",
    "independent film": "cine independiente",
    "martial arts": "artes marciales",
}

RASGO_ACTIVIDAD = "actividad"   # log(1 + n_ratings)
ETIQUETA_ACTIVIDAD = "Cinéfilo"
K_DEFECTO = 8                   # 8 en 25M; en ml-mini se usa 3
SEMILLA = 42
N_TOP = 3                       # géneros y tags que se guardan como «top»


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def _sin_tildes(texto: str) -> str:
    """Minúsculas sin tildes, para comparar etiquetas («Acción» == «accion»)."""
    base = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in base if not unicodedata.combining(c)).strip()


def _capitalizar(texto: str) -> str:
    return texto[:1].upper() + texto[1:]


def etiqueta_rasgo(rasgo: str) -> str:
    """'genero:Action' -> 'acción'; 'tag:dark' -> 'tono oscuro'; 'actividad' -> 'Cinéfilo'."""
    if rasgo == RASGO_ACTIVIDAD:
        return ETIQUETA_ACTIVIDAD
    tipo, _, nombre = rasgo.partition(":")
    if tipo == "genero":
        return GENEROS_ES.get(nombre, nombre)
    if tipo == "tag":
        return TAGS_ES.get(nombre.lower(), nombre)
    return rasgo


def _top_positivos(pesos: dict[str, float], n: int = N_TOP,
                   excluir: set[str] | frozenset = frozenset()) -> list[str]:
    """Claves con peso > 0 ordenadas de mayor a menor (desempate alfabético)."""
    candidatos = [(k, v) for k, v in pesos.items() if v > 0 and k not in excluir]
    candidatos.sort(key=lambda kv: (-kv[1], kv[0]))
    return [k for k, _ in candidatos[:n]]


# --------------------------------------------------------------------------- #
# Vectores de gusto (Spark)
# --------------------------------------------------------------------------- #

def tags_interpretables(genome_tags) -> list[tuple[int, str]]:
    """[(tagId, tag)] de TAGS_ES presentes en genome-tags (DataFrame Spark o pandas)."""
    pdf = genome_tags if hasattr(genome_tags, "iloc") else genome_tags.toPandas()
    buscados = {t.lower() for t in TAGS_ES}
    encontrados = [(int(r.tagId), str(r.tag).lower()) for r in pdf.itertuples()
                   if str(r.tag).lower() in buscados]
    return sorted(encontrados)


def features_peliculas(movies, genome_scores=None, tags: list[tuple[int, str]] = ()):
    """DataFrame (movieId, g_00..g_19, tiene_genome, t_00..t_NN) con una fila por película.

    g_i = 1 si la película tiene el género i; t_j = relevancia del tag j (0 si falta).
    Los nombres de columna son neutros para evitar caracteres raros ('Sci-Fi', '(no...').
    """
    from pyspark.sql import functions as F

    lista = F.split(F.col("genres"), r"\|")
    cols = [F.col("movieId")]
    cols += [F.array_contains(lista, g).cast("double").alias(f"g_{i:02d}")
             for i, g in enumerate(GENEROS)]
    pelis = movies.select(*cols)
    if genome_scores is None or not tags:
        return pelis.withColumn("tiene_genome", F.lit(0.0))

    ids = [t for t, _ in tags]
    ancho = (genome_scores.filter(F.col("tagId").isin(ids))
             .groupBy("movieId").pivot("tagId", ids).agg(F.first("relevance")))
    ancho = ancho.select("movieId", *[F.coalesce(F.col(str(t)), F.lit(0.0)).alias(f"t_{j:02d}")
                                      for j, t in enumerate(ids)])
    ancho = ancho.withColumn("tiene_genome", F.lit(1.0))
    pelis = pelis.join(ancho, "movieId", "left").fillna(0.0)
    return pelis


def vectores_gusto(ratings, pelis, n_tags: int):
    """Agrega por usuario: n_ratings, media, suma_abs y los pesos normalizados.

    Devuelve un DataFrame con userId, n_ratings, media, suma_abs, suma_abs_genome,
    g_00..g_19 (normalizados), t_00..t_NN (normalizados) y log_n.
    """
    from pyspark.sql import functions as F

    stats = ratings.groupBy("userId").agg(F.count("*").alias("n_ratings"),
                                          F.avg("rating").alias("media"))
    r = (ratings.join(stats, "userId")
         .withColumn("peso", F.col("rating") - F.col("media"))
         .join(F.broadcast(pelis), "movieId", "left")
         .fillna(0.0))
    g_cols = [f"g_{i:02d}" for i in range(len(GENEROS))]
    t_cols = [f"t_{j:02d}" for j in range(n_tags)]
    aggs = [F.first("n_ratings").alias("n_ratings"), F.first("media").alias("media"),
            F.sum(F.abs("peso")).alias("suma_abs"),
            F.sum(F.abs("peso") * F.col("tiene_genome")).alias("suma_abs_genome")]
    aggs += [F.sum(F.col("peso") * F.col(c)).alias(c) for c in g_cols + t_cols]
    # Suma de ratings y conteo por género: permiten la actualización incremental
    # exacta del numerador (sum(rating - media) = S_g - n_g * media) con una media nueva.
    aggs += [F.sum(F.col("rating") * F.col(c)).alias(f"s{c}") for c in g_cols]
    aggs += [F.sum(F.col(c)).alias(f"n{c}") for c in g_cols]
    crudo = r.groupBy("userId").agg(*aggs)

    def _norm(c, den):
        return F.when(F.col(den) > 0, F.col(c) / F.col(den)).otherwise(F.lit(0.0)).alias(c)

    return crudo.select("userId", "n_ratings", "media", "suma_abs", "suma_abs_genome",
                        *[_norm(c, "suma_abs") for c in g_cols],
                        *[_norm(c, "suma_abs_genome") for c in t_cols],
                        *[f"s{c}" for c in g_cols], *[f"n{c}" for c in g_cols],
                        F.log1p(F.col("n_ratings").cast("double")).alias("log_n"))


def agrupar_kmeans(vectores, columnas: list[str], k: int, seed: int = SEMILLA):
    """StandardScaler + KMeans de MLlib. Devuelve (df con 'cluster', centros, media, std)."""
    from pyspark.ml.clustering import KMeans
    from pyspark.ml.feature import StandardScaler, VectorAssembler

    ensamblado = VectorAssembler(inputCols=columnas, outputCol="crudo").transform(vectores)
    escalador = StandardScaler(inputCol="crudo", outputCol="features",
                               withMean=True, withStd=True).fit(ensamblado)
    escalado = escalador.transform(ensamblado)
    modelo = KMeans(k=k, seed=seed, featuresCol="features", predictionCol="cluster",
                    maxIter=50).fit(escalado)
    centros = np.array([np.asarray(c) for c in modelo.clusterCenters()])
    media = np.asarray(escalador.mean.toArray())
    std = np.asarray(escalador.std.toArray())
    return modelo.transform(escalado).drop("crudo", "features"), centros, media, std


# --------------------------------------------------------------------------- #
# Nombrado de los tipos de espectador
# --------------------------------------------------------------------------- #

def _nombre_desde(rasgos: list[str]) -> str:
    """Arma el nombre en español con 1 o 2 rasgos."""
    etiquetas = [etiqueta_rasgo(r) for r in rasgos]
    if not etiquetas:
        return "Ecléctico"
    if RASGO_ACTIVIDAD in rasgos:
        otros = [etiqueta_rasgo(r) for r in rasgos if r != RASGO_ACTIVIDAD]
        return f"{ETIQUETA_ACTIVIDAD} de {otros[0]}" if otros else ETIQUETA_ACTIVIDAD
    if len(etiquetas) == 1:
        return _capitalizar(etiquetas[0])
    return _capitalizar(f"{etiquetas[0]} y {etiquetas[1]}")


def nombrar_clusters(centros: np.ndarray, rasgos: list[str], tamanos: list[int],
                     max_candidatos: int = 6) -> dict[int, dict]:
    """Nombra cada cluster con sus 2 rasgos de mayor desviación positiva (sin repetir).

    ``centros`` está en unidades estandarizadas: la media global de cada rasgo es 0,
    así que el valor del centro ES su desviación respecto del usuario promedio.
    Los clusters más grandes eligen primero. Si un nombre ya está tomado se prueban
    otras parejas entre los rasgos positivos; como último recurso se numera.
    """
    nombrables = [i for i, r in enumerate(rasgos)
                  if not (r.startswith("genero:") and r[7:] in GENEROS_NO_NOMBRABLES)]
    usados: set[str] = set()
    salida: dict[int, dict] = {}
    for c in sorted(range(len(centros)), key=lambda c: (-tamanos[c], c)):
        orden = sorted(nombrables, key=lambda i: (-centros[c][i], rasgos[i]))
        # Positivos sin etiquetas repetidas (p. ej. género Action y tag action).
        positivos, vistas = [], set()
        for i in orden:
            if centros[c][i] <= 0:
                break
            clave = _sin_tildes(etiqueta_rasgo(rasgos[i]))
            if clave not in vistas:
                vistas.add(clave)
                positivos.append(i)
            if len(positivos) >= max_candidatos:
                break
        # Parejas en orden de preferencia: (1,2), (1,3), ..., (2,3), ...
        candidatos = [[a, b] for x, a in enumerate(positivos) for b in positivos[x + 1:]]
        candidatos = candidatos or [positivos[:1]]
        elegido = None
        for par in candidatos:
            nombre = _nombre_desde([rasgos[i] for i in par])
            if _sin_tildes(nombre) not in usados:
                elegido = (nombre, par)
                break
        if elegido is None:  # todo tomado: se numera el primero
            par = candidatos[0]
            base, n = _nombre_desde([rasgos[i] for i in par]), 2
            while _sin_tildes(f"{base} {n}") in usados:
                n += 1
            elegido = (f"{base} {n}", par)
        nombre, par = elegido
        usados.add(_sin_tildes(nombre))
        salida[c] = {
            "nombre": nombre,
            "rasgos": [{"rasgo": rasgos[i], "etiqueta": etiqueta_rasgo(rasgos[i]),
                        "desviacion": round(float(centros[c][i]), 4)} for i in par],
        }
    return salida


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #

def ejecutar(spark, tablas: dict, salida=None, k: int = K_DEFECTO, seed: int = SEMILLA,
             hasta: str | None = None, log=print) -> dict:
    """Vectores de gusto -> KMeans -> nombres -> perfiles.parquet y tipos.json.

    tablas: dict de ``datos.cargar_movielens`` con ratings y movies (genome opcional).
    hasta:  si se indica (AAAA-MM-DD), usa solo ratings anteriores a esa fecha.
    Devuelve {'tipos': [...], 'perfiles': DataFrame pandas, 'segundos': float, ...}.
    """
    import pandas as pd

    from .datos import antes_de

    inicio = time.perf_counter()
    spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
    ratings = tablas["ratings"]
    if hasta:
        ratings = antes_de(ratings, hasta)

    tags = tags_interpretables(tablas["genome_tags"]) if "genome_tags" in tablas else []
    if "genome_tags" in tablas:
        faltan = sorted(set(TAGS_ES) - {t for _, t in tags})
        # Se listan por nombre: en 25M deberían estar todos (los 44 se escribieron tal
        # cual aparecen en genome-tags.csv); si falta alguno, es un error de escritura.
        log(f"Tags genome interpretables: {len(tags)} de {len(TAGS_ES)}"
            + (f" (no están en los datos {len(faltan)}: {', '.join(faltan)})" if faltan else ""))
    else:
        log("Sin genome-tags.csv: el perfil usa solo géneros")
    pelis = features_peliculas(tablas["movies"], tablas.get("genome_scores"), tags)
    vectores = vectores_gusto(ratings, pelis, len(tags)).cache()
    n_usuarios = vectores.count()
    if n_usuarios == 0:
        raise ValueError("No hay ratings para perfilar")
    if n_usuarios < k:
        raise ValueError(f"Hay {n_usuarios} usuarios y se pidieron k={k} clusters")
    log(f"Vectores de gusto de {n_usuarios:,} usuarios "
        f"({len(GENEROS)} géneros + {len(tags)} tags + actividad)")

    g_cols = [f"g_{i:02d}" for i in range(len(GENEROS))]
    t_cols = [f"t_{j:02d}" for j in range(len(tags))]
    columnas = g_cols + t_cols + ["log_n"]
    rasgos = ([f"genero:{g}" for g in GENEROS] + [f"tag:{t}" for _, t in tags]
              + [RASGO_ACTIVIDAD])

    agrupado, centros, media, std = agrupar_kmeans(vectores, columnas, k, seed)
    pdf = agrupado.toPandas().sort_values("userId").reset_index(drop=True)
    vectores.unpersist()

    tamanos = [int((pdf["cluster"] == c).sum()) for c in range(k)]
    nombres = nombrar_clusters(centros, rasgos, tamanos)

    # Perfil legible por usuario.
    nombres_tags = [t for _, t in tags]
    filas = []
    for fila in pdf.itertuples(index=False):
        d = fila._asdict()
        pg = {g: round(float(d[c]), 6) for g, c in zip(GENEROS, g_cols)}
        pt = {t: round(float(d[c]), 6) for t, c in zip(nombres_tags, t_cols)}
        sg = {g: [float(d[f"s{c}"]), int(d[f"n{c}"])] for g, c in zip(GENEROS, g_cols)
              if d[f"n{c}"] > 0}
        filas.append({
            "userId": int(d["userId"]),
            "top_generos": _top_positivos(pg, excluir=GENEROS_NO_NOMBRABLES),
            "top_tags": _top_positivos(pt),
            "cluster": int(d["cluster"]),
            "tipo": nombres[int(d["cluster"])]["nombre"],
            "n_ratings": int(d["n_ratings"]),
            "media": float(d["media"]),
            "suma_abs": float(d["suma_abs"]),
            "suma_abs_genome": float(d["suma_abs_genome"]),
            "pesos_generos": json.dumps(pg, ensure_ascii=False),
            "pesos_tags": json.dumps(pt, ensure_ascii=False),
            "sumas_generos": json.dumps(sg, ensure_ascii=False),
        })
    perfiles = pd.DataFrame(filas)

    tipos = []
    for c in sorted(nombres, key=lambda c: (-tamanos[c], c)):
        tipos.append({
            "cluster": c,
            "nombre": nombres[c]["nombre"],
            "tamaño": tamanos[c],
            "proporcion": round(tamanos[c] / n_usuarios, 4),
            "rasgos": nombres[c]["rasgos"],
            # Centroide estandarizado (desviaciones) y en unidades originales.
            "centroide": {r: round(float(v), 4) for r, v in zip(rasgos, centros[c])},
            "centroide_original": {r: round(float(v * s + m), 4)
                                   for r, v, s, m in zip(rasgos, centros[c], std, media)},
        })
    segundos = time.perf_counter() - inicio
    resumen = {"k": k, "seed": seed, "n_usuarios": n_usuarios, "hasta": hasta,
               "rasgos": rasgos, "media_features": [round(float(x), 6) for x in media],
               "std_features": [round(float(x), 6) for x in std],
               "segundos": round(segundos, 2), "tipos": tipos}
    if salida is not None:
        guardar(perfiles, resumen, Path(salida))
    return {**resumen, "perfiles": perfiles}


def guardar(perfiles, resumen: dict, salida: Path) -> None:
    """Escribe perfiles.parquet (con pandas/pyarrow) y tipos.json."""
    import shutil

    salida.mkdir(parents=True, exist_ok=True)
    destino = salida / "perfiles.parquet"
    if destino.is_dir():
        shutil.rmtree(destino)
    perfiles.to_parquet(destino, index=False)
    (salida / "tipos.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2),
                                       encoding="utf-8")
    _leer_perfiles.cache_clear()


def tabla_tipos(resumen: dict) -> str:
    """Tabla de texto: tipo de espectador, tamaño y rasgos principales."""
    filas = [(t["nombre"], f"{t['tamaño']:,}", f"{100 * t['proporcion']:.1f}%",
              ", ".join(f"{r['etiqueta']} (+{r['desviacion']:.2f})" for r in t["rasgos"]))
             for t in resumen["tipos"]]
    enc = ("Tipo de espectador", "Usuarios", "%", "Rasgos (desviación estandarizada)")
    anchos = [max(len(enc[i]), *(len(f[i]) for f in filas)) for i in range(4)]
    lineas = ["  ".join(enc[i].ljust(anchos[i]) for i in range(4)),
              "  ".join("-" * a for a in anchos)]
    lineas += ["  ".join(f[i].ljust(anchos[i]) if i != 1 else f[i].rjust(anchos[i])
                         for i in range(4)) for f in filas]
    return "\n".join(linea.rstrip() for linea in lineas)


# --------------------------------------------------------------------------- #
# Consulta y actualización incremental (sin Spark)
# --------------------------------------------------------------------------- #

@lru_cache(maxsize=4)
def _leer_perfiles(ruta: str, _mtime: float):
    import pandas as pd

    return pd.read_parquet(ruta).set_index("userId")


@lru_cache(maxsize=4)
def _leer_tipos(ruta: str, _mtime: float) -> dict:
    return json.loads(Path(ruta).read_text(encoding="utf-8"))


def perfil_usuario(userId: int, artefactos) -> dict | None:
    """Perfil legible del usuario desde ``<artefactos>/perfiles.parquet``.

    Devuelve None si el usuario no tiene perfil. Las claves son:
    userId, tipo, cluster, descripcion_tipo, top_generos, top_generos_es, top_tags,
    top_tags_es, pesos_generos, pesos_tags, n_ratings, media, suma_abs y sumas_generos.
    """
    artefactos = Path(artefactos)
    ruta = artefactos / "perfiles.parquet"
    if not ruta.exists():
        raise FileNotFoundError(f"No existe {ruta}: corre scripts/perfilar_usuarios.py")
    tabla = _leer_perfiles(str(ruta), ruta.stat().st_mtime)
    try:
        fila = tabla.loc[int(userId)]
    except (KeyError, ValueError, TypeError):
        return None
    descripcion = None
    ruta_tipos = artefactos / "tipos.json"
    if ruta_tipos.exists():
        tipos = _leer_tipos(str(ruta_tipos), ruta_tipos.stat().st_mtime)
        for t in tipos.get("tipos", []):
            if t["cluster"] == int(fila["cluster"]):
                descripcion = "Destaca en " + " y ".join(r["etiqueta"] for r in t["rasgos"])
    perfil = {
        "userId": int(userId),
        "tipo": str(fila["tipo"]),
        "cluster": int(fila["cluster"]),
        "descripcion_tipo": descripcion,
        "top_tags": [str(t) for t in fila["top_tags"]],
        "pesos_generos": json.loads(fila["pesos_generos"]),
        "pesos_tags": json.loads(fila["pesos_tags"]),
        "n_ratings": int(fila["n_ratings"]),
        "media": float(fila["media"]),
        "suma_abs": float(fila["suma_abs"]),
        # {género: [suma de ratings, n]}: base de la actualización incremental.
        "sumas_generos": (json.loads(fila["sumas_generos"])
                          if "sumas_generos" in fila.index else {}),
    }
    perfil["top_tags_es"] = [TAGS_ES.get(t, t) for t in perfil["top_tags"]]
    _completar_top_generos(perfil)
    return perfil


def _completar_top_generos(perfil: dict) -> None:
    perfil["top_generos"] = _top_positivos(perfil["pesos_generos"], excluir=GENEROS_NO_NOMBRABLES)
    perfil["top_generos_es"] = [GENEROS_ES.get(g, g) for g in perfil["top_generos"]]


def perfil_vacio(userId: int) -> dict:
    """Perfil inicial para un usuario nuevo (p. ej. llegado por el streaming)."""
    perfil = {"userId": int(userId), "tipo": None, "cluster": None, "descripcion_tipo": None,
              "top_tags": [], "top_tags_es": [], "pesos_generos": {g: 0.0 for g in GENEROS},
              "pesos_tags": {}, "n_ratings": 0, "media": 0.0, "suma_abs": 0.0,
              "sumas_generos": {}}
    _completar_top_generos(perfil)
    return perfil


def _generos_de(evento, generos_por_pelicula) -> list[str]:
    """Géneros del evento: campo 'genres'/'generos' o el mapa movieId -> géneros."""
    if isinstance(evento, dict):
        g = evento.get("generos", evento.get("genres"))
        movie_id = evento.get("movieId")
    else:
        g, movie_id = None, evento[0]
    if g is None and generos_por_pelicula is not None:
        g = generos_por_pelicula.get(int(movie_id))
    if g is None:
        return []
    if isinstance(g, str):
        g = g.split("|")
    return [x for x in g if x in GENEROS_ES]


def actualizar_perfil_generos(perfil: dict | None, nuevos_ratings,
                              generos_por_pelicula: dict | None = None,
                              userId: int | None = None) -> dict:
    """Actualiza de forma incremental el vector de géneros con ratings nuevos.

    perfil: dict de ``perfil_usuario`` (o None para un usuario nuevo; entonces se
            requiere ``userId``). No se modifica: se devuelve una copia.
    nuevos_ratings: iterable de dicts {movieId, rating[, genres]} o tuplas (movieId, rating).
    generos_por_pelicula: mapa movieId -> 'Action|Thriller' o lista, si los eventos no
            traen los géneros.

    Por género se acumulan la suma de ratings S_g y su conteo n_g, de modo que el
    numerador ``sum(rating - media) = S_g - n_g * media`` es exacto con la media nueva
    del usuario. El denominador (suma de |rating - media|) se actualiza en línea
    sumando |rating - media nueva| de cada rating nuevo (aproximación que corrige el
    próximo perfilado batch).
    """
    if perfil is None:
        if userId is None:
            raise ValueError("Para un usuario nuevo indica userId")
        perfil = perfil_vacio(userId)
    nuevo = dict(perfil)
    n, media = int(nuevo.get("n_ratings") or 0), float(nuevo.get("media") or 0.0)
    suma_abs = float(nuevo.get("suma_abs") or 0.0)
    sumas = {g: list(v) for g, v in (nuevo.get("sumas_generos") or {}).items()}
    if not sumas and n > 0:
        # Perfil sin sumas (versión anterior): se reconstruyen aproximadas desde los pesos.
        sumas = {g: [w * suma_abs + media, 1] for g, w in nuevo["pesos_generos"].items() if w}
    for evento in nuevos_ratings:
        rating = float(evento["rating"] if isinstance(evento, dict) else evento[1])
        n += 1
        media += (rating - media) / n
        suma_abs += abs(rating - media)
        for g in _generos_de(evento, generos_por_pelicula):
            s_g, n_g = sumas.get(g, [0.0, 0])
            sumas[g] = [s_g + rating, n_g + 1]
    numeradores = {g: sumas[g][0] - sumas[g][1] * media if g in sumas else 0.0
                   for g in GENEROS}
    if suma_abs <= 0:  # todos sus ratings son iguales: se usa el tamaño del numerador
        suma_abs = sum(abs(v) for v in numeradores.values())
    nuevo.update({
        "n_ratings": n, "media": media, "suma_abs": suma_abs, "sumas_generos": sumas,
        "pesos_generos": {g: (round(numeradores[g] / suma_abs, 6) if suma_abs > 0 else 0.0)
                          for g in GENEROS},
    })
    _completar_top_generos(nuevo)
    return nuevo


def generos_por_pelicula(ruta_datos) -> dict[int, list[str]]:
    """Mapa movieId -> lista de géneros leyendo movies.csv con pandas."""
    import pandas as pd

    movies = pd.read_csv(Path(ruta_datos) / "movies.csv", usecols=["movieId", "genres"])
    return {int(m): str(g).split("|") for m, g in zip(movies["movieId"], movies["genres"])}


def asignar_tipo(perfil_vector: list[float], artefactos) -> dict:
    """Asigna el tipo más cercano a un vector de features crudas (orden de tipos.json['rasgos'])."""
    tipos = json.loads((Path(artefactos) / "tipos.json").read_text(encoding="utf-8"))
    media = np.array(tipos["media_features"])
    std = np.array(tipos["std_features"])
    x = (np.asarray(perfil_vector, dtype=float) - media) / np.where(std > 0, std, 1.0)
    x = np.where(std > 0, x, 0.0)
    mejor = min(tipos["tipos"], key=lambda t: float(np.sum(
        (x - np.array([t["centroide"][r] for r in tipos["rasgos"]])) ** 2)))
    return {"cluster": mejor["cluster"], "tipo": mejor["nombre"]}


# --------------------------------------------------------------------------- #
# Tipo de espectador de los usuarios nuevos (sin Spark)
# --------------------------------------------------------------------------- #
#
# Replica en numpy exactamente las features de ``vectores_gusto`` (las que ve el KMeans):
#   peso_i   = rating_i - media del usuario
#   g_k      = sum(peso_i * [película i tiene el género k]) / sum(|peso_i|)
#   t_j      = sum(peso_i * relevancia_j(i)) / sum(|peso_i| * tiene_genome(i))
#   log_n    = log(1 + n_ratings)
# y luego ``asignar_tipo`` aplica el mismo escalado (media/std de tipos.json) y busca el
# centroide más cercano. Una película sin datos de genome aporta 0 a los t_j y no suma
# en su denominador, igual que en Spark (left join + fillna(0)).

@lru_cache(maxsize=4)
def _features_cache(ruta_datos: str, tags: tuple[str, ...], _mtime: float):
    import pandas as pd

    ruta = Path(ruta_datos)
    movies = pd.read_csv(ruta / "movies.csv", usecols=["movieId", "genres"])
    feats: dict[int, tuple[frozenset, dict | None]] = {
        int(m): (frozenset(str(g).split("|")), None)
        for m, g in zip(movies["movieId"], movies["genres"])}
    if not tags or not (ruta / "genome-tags.csv").exists() \
            or not (ruta / "genome-scores.csv").exists():
        return feats
    # genome-scores de 25M tiene ~15M filas: se usa la matriz compacta genome.npy
    # (construida una sola vez con pyarrow) y solo las columnas de los tags usados.
    from . import genome as genome_npy

    g = genome_npy.cargar(ruta)
    relevancias = g.por_pelicula([t.lower() for t in tags]) if g is not None else {}
    for m, rel in relevancias.items():
        generos = feats.get(m, (frozenset(), None))[0]
        feats[m] = (generos, rel)
    return feats


def features_tipo(ruta_datos, rasgos: list[str]) -> dict[int, tuple[frozenset, dict | None]]:
    """{movieId: (géneros, {tag: relevancia} o None si no tiene genome)} con pandas.

    Solo lee los tags que aparecen en ``rasgos`` (tipos.json). Queda en caché.
    """
    ruta = Path(ruta_datos)
    tags = tuple(r[4:] for r in rasgos if r.startswith("tag:"))
    mtime = (ruta / "movies.csv").stat().st_mtime
    return _features_cache(str(ruta), tags, mtime)


def vector_crudo(ratings, features: dict, rasgos: list[str]) -> np.ndarray:
    """Vector de features crudas (orden de tipos.json['rasgos']) de un usuario.

    ratings:  [(movieId, rating)] o {movieId: rating}; si una película se repite, vale
              la última calificación.
    features: salida de ``features_tipo``.
    """
    pares = dict(ratings.items() if isinstance(ratings, dict) else ratings)
    if not pares:
        raise ValueError("Se necesita al menos una calificación")
    pelis = [int(m) for m in pares]
    valores = np.array([float(pares[m]) for m in pares])
    pesos = valores - valores.mean()
    suma_abs = float(np.abs(pesos).sum())
    vacio = (frozenset(), None)
    info = [features.get(m, vacio) for m in pelis]
    suma_abs_genome = float(sum(abs(p) for p, (_, rel) in zip(pesos, info) if rel is not None))
    salida = []
    for rasgo in rasgos:
        if rasgo == RASGO_ACTIVIDAD:
            salida.append(float(np.log1p(len(pelis))))
        elif rasgo.startswith("genero:"):
            g = rasgo[7:]
            num = sum(p for p, (gen, _) in zip(pesos, info) if g in gen)
            salida.append(num / suma_abs if suma_abs > 0 else 0.0)
        elif rasgo.startswith("tag:"):
            t = rasgo[4:]
            num = sum(p * rel.get(t, 0.0) for p, (_, rel) in zip(pesos, info)
                      if rel is not None)
            salida.append(num / suma_abs_genome if suma_abs_genome > 0 else 0.0)
        else:
            raise ValueError(f"rasgo desconocido {rasgo!r}")
    return np.array(salida, dtype=float)


def tipo_usuario(userId: int, artefactos, ratings=None, estado_db=None, datos=None,
                 features: dict | None = None) -> dict | None:
    """Tipo de espectador de un usuario según sus calificaciones ACTUALES.

    Si no se pasan ``ratings``, usa los eventos del usuario en estado.db (onboarding y
    todo lo que calificó después), así que el tipo se recalcula cada vez que califica
    más. Devuelve {'cluster', 'tipo', 'n_ratings'} o None si aún no calificó nada.
    """
    from . import config, estado

    artefactos = Path(artefactos)
    if ratings is None:
        con = estado.conectar(estado_db or config.ESTADO_DB)
        try:
            ratings = estado.eventos_usuario(con, int(userId))
        finally:
            con.close()
    pares = dict(ratings.items() if isinstance(ratings, dict) else ratings)
    if not pares:
        return None
    ruta_tipos = artefactos / "tipos.json"
    rasgos = _leer_tipos(str(ruta_tipos), ruta_tipos.stat().st_mtime)["rasgos"]
    if features is None:
        features = features_tipo(datos or config.DATOS, rasgos)
    asignado = asignar_tipo(list(vector_crudo(pares, features, rasgos)), artefactos)
    return {**asignado, "n_ratings": len(pares)}


__all__ = ["GENEROS", "GENEROS_ES", "TAGS_ES", "ejecutar", "perfil_usuario",
           "actualizar_perfil_generos", "generos_por_pelicula", "nombrar_clusters",
           "tabla_tipos", "asignar_tipo", "perfil_vacio", "features_tipo", "vector_crudo",
           "tipo_usuario"]
