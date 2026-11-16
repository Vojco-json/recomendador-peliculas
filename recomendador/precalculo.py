"""Capa batch (arquitectura Lambda) para la app CineRuta: todo con Spark, sin pandas.

Escribe en ``artefactos/<perfil>/app/`` cuatro parquets que la app solo lee:

1. ``peliculas.parquet``: una fila por película de movies.csv con
   movieId, titulo, anio, generos (lista), duracion_min, imdb_rating, imdb_votos, tmdbId,
   votos, promedio, histograma (10 conteos para 0,5; 1,0; …; 5,0), tags_genome,
   tags_usuarios y tags (los principales: genome si hay, si no los de usuarios).
   votos/promedio/histograma y tags_usuarios usan SOLO ratings/tags anteriores al corte.
2. ``personas.parquet``: directores y actores por película (movieId, tconst, nconst,
   nombre, nombre_norm, rol, orden). Cruza links.csv con title.crew, title.principals
   y name.basics de IMDb. Primero se filtran crew y principals por los tconst de
   links.csv (semi join con broadcast) y solo después se cruza con name.basics,
   también filtrado por los nconst que quedaron. ``nombre_norm`` va sin tildes y en
   minúsculas para buscar «nolan» o «penelope cruz».
3. ``populares_por_tipo.parquet``: para cada tipo de espectador (columna ``cluster``
   de perfiles.parquet, los mismos tipos de perfilar_usuarios) las películas más
   valoradas por sus usuarios ANTES del corte (groupBy cluster, movieId).
4. ``vecinos.parquet``: hasta 20 vecinos por película con BucketedRandomProjectionLSH
   de Spark ML sobre los itemFactors ALS normalizados (distancia euclídea entre
   vectores unitarios, equivalente a coseno). Nunca incluye a la propia película.

Los parquets se escriben con Spark (una carpeta con un archivo part-*), que
pandas/pyarrow leen directamente con ``pd.read_parquet(ruta)``.
"""

from __future__ import annotations

from pathlib import Path

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F

from .datos import cargar_movielens, fecha_a_epoch

# --------------------------------------------------------------------------- #
# Constantes
# --------------------------------------------------------------------------- #

CARPETA_APP = "app"
NOMBRES = {
    "peliculas": "peliculas.parquet",
    "personas": "personas.parquet",
    "populares_por_tipo": "populares_por_tipo.parquet",
    "vecinos": "vecinos.parquet",
}
# Archivos de IMDb que necesita personas.parquet (además de los de enriquecimiento).
ARCHIVOS_IMDB_PERSONAS = ("title.crew.tsv.gz", "title.principals.tsv.gz", "name.basics.tsv.gz")

# Casillas del histograma: 0,5; 1,0; …; 5,0 (escala de MovieLens).
CASILLAS = [i / 2 for i in range(1, 11)]
N_TAGS = 5            # tags principales por película
N_ACTORES = 8         # actores por película (orden de créditos de IMDb)
N_VECINOS = 20        # vecinos por película
TOP_POPULARES = 50    # películas por tipo de espectador
NOTA_ALTA = 4.0       # una calificación >= 4 cuenta como «le gustó»

# Artículos que MovieLens pospone al final («Matrix, The»): se anteponen para mostrar.
_ARTICULOS = "The|A|An|Les|Le|La|El|Los|Las|Il|Lo|Der|Die|Das|De|Het|Den"
# Tildes y diacríticos frecuentes -> letra base (para nombre_norm sin UDF de Python).
_CON_TILDE = "áàäâãåāéèëêēíìïîīóòöôõøōúùüûūñçýÿšžčćłńőűÁÀÄÂÃÅĀÉÈËÊĒÍÌÏÎĪÓÒÖÔÕØŌÚÙÜÛŪÑÇÝŸŠŽČĆŁŃŐŰ"
_SIN_TILDE = "aaaaaaaeeeeeiiiiiooooooouuuuuncyyszcclnouAAAAAAAEEEEEIIIIIOOOOOOOUUUUUNCYYSZCCLNOU"


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def normalizar_col(col) -> "F.Column":
    """Texto sin tildes, en minúsculas, con la puntuación como espacios (Spark puro)."""
    sin = F.translate(col, _CON_TILDE, _SIN_TILDE)
    sin = F.regexp_replace(F.lower(sin), r"[^0-9a-z]+", " ")
    return F.trim(F.regexp_replace(sin, r"\s+", " "))


def tconst_col(col) -> "F.Column":
    """imdbId de links.csv ('0114709' o '114709') -> 'tt0114709' (al menos 7 dígitos)."""
    limpio = F.regexp_replace(F.lower(F.trim(col.cast("string"))), r"^tt", "")
    limpio = F.regexp_replace(limpio, r"\.0$", "")
    return (F.when(limpio.rlike(r"^[0-9]+$"),
                   F.concat(F.lit("tt"), F.when(F.length(limpio) >= 7, limpio)
                            .otherwise(F.lpad(limpio, 7, "0")))))


def archivo_imdb(directorio, base: str) -> Path | None:
    """'title.crew.tsv.gz' (oficial) o 'title.crew.tsv' (muestra); None si no está."""
    directorio = Path(directorio)
    for nombre in (f"{base}.tsv.gz", f"{base}.tsv"):
        if (directorio / nombre).exists():
            return directorio / nombre
    return None


def leer_tsv_imdb(spark: SparkSession, ruta: Path) -> DataFrame:
    """TSV de IMDb: tabulaciones, '\\N' como nulo y sin comillas (los textos traen ")."""
    return (spark.read.option("header", True).option("sep", "\t")
            .option("quote", "").option("nullValue", "\\N").csv(str(ruta)))


def escribir(df: DataFrame, ruta: Path) -> Path:
    """Escribe un parquet con Spark (un solo archivo part-*, sobrescribiendo)."""
    df.coalesce(1).write.mode("overwrite").parquet(str(ruta))
    return ruta


def _ordenados(col_struct_lista, campo: str):
    """De una lista de structs (orden, valor) devuelve los valores ordenados por 'orden'."""
    return F.transform(F.array_sort(col_struct_lista), lambda s: s[campo])


# --------------------------------------------------------------------------- #
# 1. peliculas.parquet
# --------------------------------------------------------------------------- #

def estadisticas_ratings(ratings: DataFrame, corte: str) -> DataFrame:
    """votos, promedio e histograma de 10 casillas por película, solo antes del corte."""
    previos = ratings.filter(F.col("timestamp") < F.lit(fecha_a_epoch(corte)))
    casillas = [F.sum(F.when(F.col("rating") == v, 1).otherwise(0)).cast("long")
                .alias(f"_c{i}") for i, v in enumerate(CASILLAS)]
    agg = previos.groupBy("movieId").agg(F.count("*").alias("votos"),
                                          F.avg("rating").alias("promedio"), *casillas)
    return agg.select("movieId", F.col("votos").cast("long").alias("votos"),
                      F.round("promedio", 3).alias("promedio"),
                      F.array(*[F.col(f"_c{i}") for i in range(len(CASILLAS))])
                      .alias("histograma"))


def tags_genome(scores: DataFrame | None, tags: DataFrame | None, n: int = N_TAGS):
    """Los n tags genome de mayor relevancia de cada película (movieId, tags_genome)."""
    if scores is None or tags is None:
        return None
    v = Window.partitionBy("movieId").orderBy(F.desc("relevance"), F.asc("tagId"))
    top = (scores.withColumn("pos", F.row_number().over(v)).filter(F.col("pos") <= n)
           .join(F.broadcast(tags), "tagId"))
    return top.groupBy("movieId").agg(
        _ordenados(F.collect_list(F.struct("pos", "tag")), "tag").alias("tags_genome"))


def tags_usuarios(tags: DataFrame | None, corte: str, n: int = N_TAGS):
    """Los n tags de usuarios más repetidos por película, solo antes del corte."""
    if tags is None:
        return None
    previos = (tags.filter(F.col("timestamp") < F.lit(fecha_a_epoch(corte)))
               .withColumn("tag", F.lower(F.trim("tag"))).filter(F.length("tag") > 0))
    conteo = previos.groupBy("movieId", "tag").agg(F.count("*").alias("n"))
    v = Window.partitionBy("movieId").orderBy(F.desc("n"), F.asc("tag"))
    top = conteo.withColumn("pos", F.row_number().over(v)).filter(F.col("pos") <= n)
    return top.groupBy("movieId").agg(
        _ordenados(F.collect_list(F.struct("pos", "tag")), "tag").alias("tags_usuarios"))


def datos_imdb_titulos(spark: SparkSession, imdb_dir) -> DataFrame | None:
    """(tconst, anio_imdb, duracion_min, imdb_rating, imdb_votos) de title.basics/ratings."""
    basics = archivo_imdb(imdb_dir, "title.basics") if imdb_dir else None
    notas = archivo_imdb(imdb_dir, "title.ratings") if imdb_dir else None
    if basics is None:
        return None
    df = leer_tsv_imdb(spark, basics).select(
        "tconst", F.col("startYear").cast("int").alias("anio_imdb"),
        F.col("runtimeMinutes").cast("int").alias("duracion_min"))
    if notas is not None:
        df = df.join(leer_tsv_imdb(spark, notas).select(
            "tconst", F.col("averageRating").cast("double").alias("imdb_rating"),
            F.col("numVotes").cast("long").alias("imdb_votos")), "tconst", "left")
    else:
        df = (df.withColumn("imdb_rating", F.lit(None).cast("double"))
              .withColumn("imdb_votos", F.lit(None).cast("long")))
    return df


def titulo_legible_col(col) -> "F.Column":
    """'Usual Suspects, The (1995)' -> 'The Usual Suspects' (sin año ni paréntesis)."""
    t = F.regexp_replace(col, r"\s*\((\d{4})(?:[-–]\d{0,4})?\)\s*$", "")
    sin_par = F.trim(F.regexp_replace(F.regexp_replace(t, r"\([^()]*\)", " "), r"\s+", " "))
    t = F.when(F.length(sin_par) > 0, sin_par).otherwise(F.trim(t))
    return F.regexp_replace(t, rf"^(.*), ({_ARTICULOS})$", "$2 $1")


def construir_peliculas(spark: SparkSession, tablas: dict, corte: str,
                        imdb_dir=None) -> DataFrame:
    """Tabla de películas de la app (ver docstring del módulo)."""
    movies = tablas["movies"]
    base = movies.select(
        "movieId", titulo_legible_col(F.col("title")).alias("titulo"),
        F.regexp_extract("title", r"\((\d{4})(?:[-–]\d{0,4})?\)\s*$", 1).alias("_anio_ml"),
        F.when(F.col("genres") == "(no genres listed)", F.array().cast("array<string>"))
        .otherwise(F.split("genres", r"\|")).alias("generos"))
    links = tablas.get("links")
    if links is not None:
        base = base.join(links.select("movieId", tconst_col(F.col("imdbId")).alias("tconst"),
                                      F.col("tmdbId").cast("long").alias("tmdbId")),
                         "movieId", "left")
    else:
        base = (base.withColumn("tconst", F.lit(None).cast("string"))
                .withColumn("tmdbId", F.lit(None).cast("long")))

    imdb = datos_imdb_titulos(spark, imdb_dir)
    if imdb is not None:
        # Solo las filas de IMDb cuyo tconst está en links.csv.
        tconsts = base.select("tconst").where(F.col("tconst").isNotNull()).distinct()
        base = base.join(imdb.join(F.broadcast(tconsts), "tconst", "left_semi"),
                         "tconst", "left")
    else:
        for nombre, tipo in (("anio_imdb", "int"), ("duracion_min", "int"),
                             ("imdb_rating", "double"), ("imdb_votos", "long")):
            base = base.withColumn(nombre, F.lit(None).cast(tipo))

    stats = estadisticas_ratings(tablas["ratings"], corte)
    base = base.join(stats, "movieId", "left")
    vacio = F.array(*[F.lit(0).cast("long") for _ in CASILLAS])
    base = (base.withColumn("votos", F.coalesce("votos", F.lit(0).cast("long")))
            .withColumn("histograma", F.coalesce("histograma", vacio)))

    sin_tags = F.array().cast("array<string>")
    for parte, columna in ((tags_genome(tablas.get("genome_scores"), tablas.get("genome_tags")),
                            "tags_genome"),
                           (tags_usuarios(tablas.get("tags"), corte), "tags_usuarios")):
        if parte is not None:
            base = base.join(parte, "movieId", "left")
            base = base.withColumn(columna, F.coalesce(columna, sin_tags))
        else:
            base = base.withColumn(columna, sin_tags)
    base = base.withColumn("tags", F.when(F.size("tags_genome") > 0, F.col("tags_genome"))
                           .otherwise(F.col("tags_usuarios")))

    anio = F.coalesce(F.col("anio_imdb"), F.when(F.col("_anio_ml") != "",
                                                 F.col("_anio_ml").cast("int")))
    return base.select(
        F.col("movieId").cast("long").alias("movieId"), "titulo", anio.alias("anio"),
        "generos", "duracion_min", "imdb_rating", "imdb_votos", "tmdbId", "tconst",
        "votos", "promedio", "histograma", "tags_genome", "tags_usuarios", "tags",
    ).orderBy("movieId")


# --------------------------------------------------------------------------- #
# 2. personas.parquet
# --------------------------------------------------------------------------- #

ESQUEMA_PERSONAS = ("movieId LONG, tconst STRING, nconst STRING, nombre STRING, "
                    "nombre_norm STRING, rol STRING, orden INT")


def personas_vacia(spark: SparkSession) -> DataFrame:
    """Tabla de personas sin filas (sin crear workers de Python, que fallan en Windows)."""
    columnas = [c.strip().split(" ") for c in ESQUEMA_PERSONAS.split(",")]
    return spark.range(0).select(*[F.lit(None).cast(t).alias(n) for n, t in columnas])


def construir_personas(spark: SparkSession, links: DataFrame, imdb_dir,
                       n_actores: int = N_ACTORES) -> DataFrame:
    """Directores y actores de cada película de links.csv.

    Orden de las operaciones (importa sobre los datos completos de IMDb):
    1. tconst de links.csv (≈62 mil en 25M) en broadcast;
    2. title.crew y title.principals se filtran con un semi join por esos tconst;
    3. name.basics se filtra por los nconst que quedaron y recién ahí se cruza.
    Si faltan los archivos de IMDb devuelve una tabla vacía con el esquema final.
    """
    crew_p = archivo_imdb(imdb_dir, "title.crew") if imdb_dir else None
    princ_p = archivo_imdb(imdb_dir, "title.principals") if imdb_dir else None
    nombres_p = archivo_imdb(imdb_dir, "name.basics") if imdb_dir else None
    if nombres_p is None or (crew_p is None and princ_p is None):
        return personas_vacia(spark)

    ids = (links.select(F.col("movieId").cast("long").alias("movieId"),
                        tconst_col(F.col("imdbId")).alias("tconst"))
           .where(F.col("tconst").isNotNull()))
    tconsts = F.broadcast(ids.select("tconst").distinct())

    partes = []
    if crew_p is not None:
        crew = leer_tsv_imdb(spark, crew_p).join(tconsts, "tconst", "left_semi")
        directores = (crew.where(F.col("directors").isNotNull())
                      .select("tconst", F.posexplode(F.split("directors", ",")))
                      .select("tconst", F.trim("col").alias("nconst"),
                              (F.col("pos") + 1).alias("orden"))
                      .withColumn("rol", F.lit("director")))
        partes.append(directores)
    if princ_p is not None:
        principals = (leer_tsv_imdb(spark, princ_p).join(tconsts, "tconst", "left_semi")
                      .select("tconst", "nconst", "category",
                              F.col("ordering").cast("int").alias("ordering")))
        actores = (principals.where(F.col("category").isin("actor", "actress", "self"))
                   .select("tconst", "nconst", F.lit("actor").alias("rol"),
                           F.col("ordering").alias("_ord")))
        v = Window.partitionBy("tconst").orderBy("_ord", "nconst")
        actores = (actores.withColumn("orden", F.row_number().over(v))
                   .where(F.col("orden") <= n_actores)
                   .select("tconst", "nconst", "orden", "rol"))
        # Directores que solo aparecen en principals (si title.crew no los trae).
        dir_princ = (principals.where(F.col("category") == "director")
                     .select("tconst", "nconst", (F.col("ordering") + 100).alias("orden"),
                             F.lit("director").alias("rol")))
        partes += [actores, dir_princ]

    creditos = partes[0].select("tconst", "nconst", "orden", "rol")
    for p in partes[1:]:
        creditos = creditos.unionByName(p.select("tconst", "nconst", "orden", "rol"))
    creditos = (creditos.groupBy("tconst", "nconst", "rol").agg(F.min("orden").alias("orden")))

    nconsts = F.broadcast(creditos.select("nconst").distinct())
    nombres = (leer_tsv_imdb(spark, nombres_p).join(nconsts, "nconst", "left_semi")
               .select("nconst", F.col("primaryName").alias("nombre")))

    personas = (creditos.join(nombres, "nconst").join(ids, "tconst")
                .where(F.col("nombre").isNotNull()))
    # Renumera el orden de los directores 1, 2, … dentro de cada película.
    v = Window.partitionBy("movieId", "rol").orderBy("orden", "nconst")
    personas = personas.withColumn("orden", F.row_number().over(v).cast("int"))
    return personas.select("movieId", "tconst", "nconst", "nombre",
                           normalizar_col(F.col("nombre")).alias("nombre_norm"), "rol",
                           "orden").orderBy("movieId", F.desc("rol"), "orden")


# --------------------------------------------------------------------------- #
# 3. populares_por_tipo.parquet
# --------------------------------------------------------------------------- #

def construir_populares_por_tipo(spark: SparkSession, ratings: DataFrame, ruta_perfiles,
                                 corte: str, top: int = TOP_POPULARES,
                                 min_votos: int = 1) -> DataFrame:
    """Películas preferidas por cada tipo de espectador, solo con ratings antes del corte.

    Los tipos son la columna ``cluster`` (y su nombre ``tipo``) de perfiles.parquet,
    la salida de perfilar_usuarios. Orden dentro del tipo: más calificaciones altas
    (>= 4), luego mejor promedio y luego más votos. Columnas: cluster, tipo, movieId,
    votos, promedio, votos_altos, rank.
    """
    perf = (spark.read.parquet(str(ruta_perfiles))
            .select(F.col("userId").cast("int").alias("userId"),
                    F.col("cluster").cast("int").alias("cluster"), "tipo"))
    previos = ratings.filter(F.col("timestamp") < F.lit(fecha_a_epoch(corte)))
    agg = (previos.join(F.broadcast(perf), "userId")
           .groupBy("cluster", "tipo", "movieId")
           .agg(F.count("*").cast("long").alias("votos"),
                F.round(F.avg("rating"), 3).alias("promedio"),
                F.sum(F.when(F.col("rating") >= NOTA_ALTA, 1).otherwise(0))
                .cast("long").alias("votos_altos"))
           .where(F.col("votos") >= min_votos))
    v = Window.partitionBy("cluster").orderBy(F.desc("votos_altos"), F.desc("promedio"),
                                              F.desc("votos"), F.asc("movieId"))
    return (agg.withColumn("rank", F.row_number().over(v)).where(F.col("rank") <= top)
            .select("cluster", "tipo", F.col("movieId").cast("long").alias("movieId"),
                    "votos", "promedio", "votos_altos", "rank")
            .orderBy("cluster", "rank"))


# --------------------------------------------------------------------------- #
# 4. vecinos.parquet (LSH sobre factores ALS)
# --------------------------------------------------------------------------- #

def construir_vecinos(spark: SparkSession, ruta_items, n: int = N_VECINOS,
                      bucket_length: float | None = None, umbral: float | None = None,
                      num_tablas: int = 4, permitidos: DataFrame | None = None,
                      seed: int = 42, respaldo: bool = True) -> DataFrame:
    """Hasta ``n`` vecinos por película con BucketedRandomProjectionLSH.

    Los factores se normalizan a norma 1, así la distancia euclídea d cumple
    coseno = 1 - d²/2 (columna ``similitud``). ``permitidos`` (movieId) limita las
    películas consideradas, por ejemplo a las que tienen un mínimo de votos.
    Por defecto, con pocas películas (<= 5000) el balde es grande y el umbral 2
    (equivale a la búsqueda exacta); con muchas, baldes finos y umbral 1 (coseno >= 0,5)
    para no comparar todos los pares.

    Respaldo: el LSH es aproximado y con umbral 1 algunas películas «raras» pueden
    quedar con menos de ``n`` vecinos (o sin ninguno). Con ``respaldo=True`` esas
    películas (y solo esas) se completan con una búsqueda exacta por producto punto
    contra todas las demás, en Spark SQL (sin UDF de Python). Así cada película tiene
    min(n, total - 1) vecinos y la fila «Porque te gustó X» nunca queda vacía.
    Columnas: movieId, vecino, distancia, similitud, rank, metodo ('lsh' o 'exacto').
    """
    from pyspark.ml.feature import BucketedRandomProjectionLSH, Normalizer
    from pyspark.ml.functions import array_to_vector, vector_to_array

    items = spark.read.parquet(str(ruta_items)).select(
        F.col("movieId").cast("long").alias("movieId"),
        array_to_vector(F.col("features").cast("array<double>")).alias("crudo"))
    if permitidos is not None:
        items = items.join(F.broadcast(permitidos.select(
            F.col("movieId").cast("long").alias("movieId")).distinct()), "movieId", "left_semi")
    items = Normalizer(inputCol="crudo", outputCol="features", p=2.0).transform(items) \
        .select("movieId", "features").cache()
    total = items.count()
    pequeno = total <= 5000
    bucket_length = bucket_length or (4.0 if pequeno else 0.05)
    umbral = umbral or (2.0 if pequeno else 1.0)

    lsh = BucketedRandomProjectionLSH(inputCol="features", outputCol="hashes",
                                      bucketLength=bucket_length, numHashTables=num_tablas,
                                      seed=seed).fit(items)
    pares = (lsh.approxSimilarityJoin(items, items, umbral, distCol="distancia")
             .select(F.col("datasetA.movieId").alias("movieId"),
                     F.col("datasetB.movieId").alias("vecino"), "distancia")
             .where(F.col("movieId") != F.col("vecino"))           # nunca la propia película
             .dropDuplicates(["movieId", "vecino"])
             .withColumn("metodo", F.lit("lsh")))

    if respaldo:
        pares = pares.cache()
        objetivo = min(n, total - 1)
        cuenta = pares.groupBy("movieId").agg(F.count("*").alias("k"))
        # Películas con menos vecinos LSH que el objetivo (incluidas las que no tienen).
        faltantes = (items.select("movieId").join(cuenta, "movieId", "left")
                     .where(F.coalesce(F.col("k"), F.lit(0)) < objetivo)
                     .select("movieId").cache())
        if faltantes.count() > 0:
            arr = items.select("movieId", vector_to_array("features").alias("v"))
            izq = arr.join(faltantes, "movieId", "left_semi").select(
                F.col("movieId"), F.col("v").alias("va"))
            der = arr.select(F.col("movieId").alias("vecino"), F.col("v").alias("vb"))
            # Coseno = producto punto de vectores unitarios; d = sqrt(2 - 2·coseno).
            punto = F.aggregate(F.zip_with("va", "vb", lambda x, y: x * y),
                                F.lit(0.0), lambda acc, z: acc + z)
            exactos = (izq.crossJoin(der).where(F.col("movieId") != F.col("vecino"))
                       .select("movieId", "vecino",
                               F.sqrt(F.greatest(F.lit(0.0), 2 - 2 * punto)).alias("distancia"),
                               F.lit("exacto").alias("metodo")))
            # Para las faltantes se usa solo la búsqueda exacta (es mejor que el LSH).
            pares = (pares.join(faltantes, "movieId", "left_anti")
                     .unionByName(exactos))

    v = Window.partitionBy("movieId").orderBy(F.asc("distancia"), F.asc("vecino"))
    resultado = (pares.withColumn("rank", F.row_number().over(v)).where(F.col("rank") <= n)
                 .select("movieId", "vecino", F.round("distancia", 5).alias("distancia"),
                         F.round(1 - F.col("distancia") ** 2 / 2, 5).alias("similitud"),
                         F.col("rank").cast("int").alias("rank"), "metodo")
                 .orderBy("movieId", "rank"))
    return resultado


def cobertura_vecinos(spark: SparkSession, ruta_vecinos, ruta_items,
                      n: int = N_VECINOS) -> dict:
    """Resumen de vecinos.parquet para revisar el resultado (p. ej. tras correr 25M).

    Devuelve {'peliculas': con al menos un vecino, 'completas': con n vecinos,
    'incompletas': con 1..n-1, 'sin_vecinos': películas de item_factors sin ninguno
    (las excluidas por votos mínimos cuentan aquí), 'exactos': filas del respaldo}.
    """
    v = spark.read.parquet(str(ruta_vecinos))
    por_peli = v.groupBy("movieId").agg(F.count("*").alias("k"))
    fila = por_peli.agg(F.count("*").alias("peliculas"),
                        F.sum(F.when(F.col("k") >= n, 1).otherwise(0)).alias("completas"),
                        F.sum(F.when(F.col("k") < n, 1).otherwise(0)).alias("incompletas")
                        ).first()
    items = spark.read.parquet(str(ruta_items)).select(F.col("movieId").cast("long"))
    sin = items.join(por_peli, "movieId", "left_anti").count()
    exactos = v.where(F.col("metodo") == "exacto").count() if "metodo" in v.columns else 0
    return {"peliculas": int(fila["peliculas"] or 0), "completas": int(fila["completas"] or 0),
            "incompletas": int(fila["incompletas"] or 0), "sin_vecinos": int(sin),
            "exactos": int(exactos)}


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #

def ejecutar(spark: SparkSession, datos, artefactos, corte: str, imdb_dir=None,
             top_populares: int = TOP_POPULARES, min_votos_vecinos: int = 0,
             log=print) -> dict:
    """Genera los 4 parquets en ``<artefactos>/app`` y devuelve {nombre: ruta}.

    datos:      carpeta de MovieLens (ml-25m o muestras/ml-mini).
    artefactos: carpeta del perfil con perfiles.parquet e item_factors.parquet.
    corte:      fecha AAAA-MM-DD; los ratings y tags desde esa fecha no se usan.
    imdb_dir:   carpeta con los TSV de IMDb (opcional; sin ella faltan duración y personas).
    """
    artefactos = Path(artefactos)
    salida = artefactos / CARPETA_APP
    salida.mkdir(parents=True, exist_ok=True)
    disponibles = [t for t in ("ratings", "movies", "links", "tags", "genome_tags",
                               "genome_scores") if (Path(datos) / {
                                   "genome_tags": "genome-tags.csv",
                                   "genome_scores": "genome-scores.csv"}.get(t, f"{t}.csv")
                               ).exists()]
    tablas = cargar_movielens(datos, spark, disponibles)
    rutas = {}

    log("1/4 peliculas.parquet (estadísticas antes del corte, IMDb y tags) ...")
    rutas["peliculas"] = escribir(construir_peliculas(spark, tablas, corte, imdb_dir),
                                  salida / NOMBRES["peliculas"])

    log("2/4 personas.parquet (directores y reparto vía links.csv) ...")
    links = tablas.get("links")
    personas = (construir_personas(spark, links, imdb_dir) if links is not None
                else personas_vacia(spark))
    rutas["personas"] = escribir(personas, salida / NOMBRES["personas"])

    log("3/4 populares_por_tipo.parquet (groupBy por tipo de espectador) ...")
    rutas["populares_por_tipo"] = escribir(
        construir_populares_por_tipo(spark, tablas["ratings"], artefactos / "perfiles.parquet",
                                     corte, top_populares),
        salida / NOMBRES["populares_por_tipo"])

    log("4/4 vecinos.parquet (LSH sobre factores ALS, top-20) ...")
    permitidos = None
    if min_votos_vecinos > 0:
        permitidos = (estadisticas_ratings(tablas["ratings"], corte)
                      .where(F.col("votos") >= min_votos_vecinos).select("movieId"))
    rutas["vecinos"] = escribir(
        construir_vecinos(spark, artefactos / "item_factors.parquet", permitidos=permitidos),
        salida / NOMBRES["vecinos"])
    return rutas
