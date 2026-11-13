"""Streaming: Kafka (o archivos) -> Spark Structured Streaming -> SQLite (estado en vivo).

Flujo
-----
1. ``iniciar_streaming`` lee el topic de Kafka (o una carpeta de archivos JSON/CSV),
   interpreta cada mensaje ``{"userId", "movieId", "rating", "timestamp"}`` y en cada
   micro-lote llama a ``procesar_lote(df, batch_id, ctx)`` vía ``foreachBatch``.
2. ``procesar_lote`` guarda los eventos sin duplicar, actualiza el perfil de géneros de
   cada usuario activo y recalcula su top-10 por fold-in (numpy, sin reentrenar ALS),
   excluyendo las películas que ya vio. Todo queda en ``estado.db``.
3. ``Reentrenador`` (hilo aparte) cada N minutos reentrena ALS con la base + los eventos
   nuevos, escribe los factores de ítems en una carpeta versionada nueva
   (``<artefactos>/modelos/vNNNN``) y cambia el puntero en ``meta`` de forma atómica.
   El siguiente micro-lote ya usa los factores nuevos.

El checkpoint vive en ``<artefactos>/checkpoints/<topic>``: al reiniciar, Spark retoma
desde los offsets confirmados y la tabla ``lotes`` evita aplicar dos veces un lote.

Toda la lógica en Python corre en el driver (``collect`` de lotes pequeños); no se usan
workers de Python, que fallan de forma intermitente en Windows.
"""

from __future__ import annotations

import json
import shutil
import threading
import time
from collections import defaultdict
from pathlib import Path

import numpy as np

from . import batch_als, config, estado, foldin, perfiles

N_RECS = 10
ESQUEMA_EVENTO = "userId INT, movieId INT, rating DOUBLE, timestamp LONG"


# --------------------------------------------------------------------------- #
# Contexto compartido por los micro-lotes y el hilo de reentrenamiento
# --------------------------------------------------------------------------- #

class Contexto:
    """Todo lo que necesita ``procesar_lote``.

    spark:       SparkSession.
    artefactos:  carpeta con item_factors.parquet, perfiles.parquet y tipos.json
                 (artefactos/25m en modo completo, artefactos/demo en modo demo).
    estado_db:   ruta de estado.db.
    modo:        'completo' o 'demo'.
    consulta:    nombre de la consulta (el topic); identifica los lotes ya aplicados.
    historia:    DataFrame de Spark (userId, movieId, rating) con los ratings previos de
                 los usuarios: se suman a los eventos para el fold-in y para excluir lo
                 ya visto. Puede ser None.
    base_reentreno: DataFrame de Spark con la base del reentrenamiento (en modo demo,
                 base_demo.parquet; en modo completo, la historia).
    generos:     mapa movieId -> lista de géneros (para el perfil incremental).
    """

    def __init__(self, spark, artefactos, estado_db, modo: str = "completo",
                 consulta: str = "ratings", historia=None, base_reentreno=None,
                 generos: dict | None = None, n_recs: int = N_RECS,
                 params_als: dict | None = None, log=print):
        if modo not in config.MODOS_VALIDOS:
            raise ValueError(f"modo {modo!r} no válido")
        self.spark = spark
        self.artefactos = Path(artefactos)
        self.estado_db = Path(estado_db)
        self.modo = modo
        self.consulta = consulta
        self.historia = historia
        self.base_reentreno = base_reentreno if base_reentreno is not None else historia
        self.generos = generos or {}
        self.n_recs = n_recs
        self.log = log
        self.params_als = params_als or _params_base(self.artefactos)
        self.con = estado.conectar(self.estado_db)
        self.lock = threading.Lock()          # serializa escrituras de lotes y swaps
        self._factores = None                 # (ruta, ids, matriz) en memoria
        self._preparar_meta()

    # Meta inicial: versión 0 = factores precalculados en <artefactos>/item_factors.parquet.
    def _preparar_meta(self) -> None:
        puntero = estado.get_meta(self.con, "item_factors")
        if puntero is None or not Path(puntero).exists():
            estado.set_meta(self.con, item_factors=str(self.artefactos / "item_factors.parquet"),
                            version_modelo=0, modo=self.modo)
        else:
            estado.set_meta(self.con, modo=self.modo)

    def factores(self) -> tuple[np.ndarray, np.ndarray]:
        """(ids, matriz) de los factores vigentes según el puntero en meta."""
        ruta = estado.get_meta(self.con, "item_factors")
        if self._factores is None or self._factores[0] != ruta:
            ids, matriz = foldin.cargar_item_factors(ruta)
            self._factores = (ruta, ids, matriz)
        return self._factores[1], self._factores[2]

    def cerrar(self) -> None:
        self.con.close()


def _params_base(artefactos: Path) -> dict:
    """Parámetros de ALS del entrenamiento base (metricas.json) o los por defecto."""
    ruta = Path(artefactos) / "metricas.json"
    if ruta.exists():
        try:
            return {**batch_als.PARAMS_DEFECTO,
                    **json.loads(ruta.read_text(encoding="utf-8")).get("params", {})}
        except (ValueError, OSError):
            pass
    return dict(batch_als.PARAMS_DEFECTO)


# --------------------------------------------------------------------------- #
# Procesamiento de un micro-lote
# --------------------------------------------------------------------------- #

def _historia_usuarios(ctx: Contexto, usuarios: list[int]) -> dict[int, dict[int, float]]:
    """{userId: {movieId: rating}} con los ratings previos de los usuarios indicados."""
    salida: dict[int, dict[int, float]] = defaultdict(dict)
    if ctx.historia is None or not usuarios:
        return salida
    from pyspark.sql import functions as F

    filas = (ctx.historia.filter(F.col("userId").isin(usuarios))
             .select("userId", "movieId", "rating").collect())
    for f in filas:
        salida[int(f["userId"])][int(f["movieId"])] = float(f["rating"])
    return salida


def _perfil_inicial(ctx: Contexto, userId: int) -> dict | None:
    """Perfil vigente: el de estado.db o, si no hay, el precalculado en los artefactos."""
    perfil = estado.leer_perfil(ctx.con, userId)
    if perfil is not None:
        return perfil
    try:
        return perfiles.perfil_usuario(userId, ctx.artefactos)
    except FileNotFoundError:
        return None


def actualizar_usuarios(ctx: Contexto, usuarios, historia=None) -> None:
    """Recalcula y guarda (sin commit) el top-N por fold-in de los usuarios indicados."""
    ids, matriz = ctx.factores()
    historia = historia if historia is not None else _historia_usuarios(ctx, list(usuarios))
    for u in usuarios:
        ratings = dict(historia.get(u, {}))
        ratings.update(dict(estado.eventos_usuario(ctx.con, u)))  # lo nuevo manda
        recs = foldin.recomendar_foldin((ids, matriz), list(ratings.items()), n=ctx.n_recs,
                                        reg=float(ctx.params_als.get("regParam", 0.1)))
        estado.guardar_recomendaciones(ctx.con, u, recs, commit=False)


def procesar_lote(df, batch_id: int, ctx: Contexto) -> int:
    """Aplica un micro-lote al estado en vivo. Devuelve cuántos eventos nuevos guardó.

    1. Si el lote ya se aplicó (reintento de Spark tras un reinicio), no hace nada.
    2. Guarda los eventos (clave userId, movieId, timestamp: los repetidos se ignoran).
    3. Actualiza el perfil de géneros de cada usuario con sus eventos nuevos.
    4. Recalcula su top-N por fold-in, sin películas ya vistas.
    Todo en una transacción de SQLite junto con la marca del lote.
    """
    filas = (df.select("userId", "movieId", "rating", "timestamp")
             .dropna(subset=["userId", "movieId", "rating", "timestamp"]).collect())
    eventos = [{"userId": int(f[0]), "movieId": int(f[1]), "rating": float(f[2]),
                "timestamp": int(f[3])} for f in filas]
    with ctx.lock:
        if estado.lote_procesado(ctx.con, ctx.consulta, batch_id):
            ctx.log(f"[lote {batch_id}] ya aplicado; se omite")
            return 0
        try:
            nuevos = estado.guardar_eventos(ctx.con, eventos, commit=False)
            por_usuario: dict[int, list[dict]] = defaultdict(list)
            for e in nuevos:
                por_usuario[e["userId"]].append(e)
            usuarios = sorted(por_usuario)
            for u in usuarios:
                perfil = perfiles.actualizar_perfil_generos(
                    _perfil_inicial(ctx, u), por_usuario[u], ctx.generos, userId=u)
                estado.guardar_perfil(ctx.con, perfil, commit=False)
            actualizar_usuarios(ctx, usuarios)
            ctx.con.execute("INSERT INTO lotes(consulta, batch_id, procesado_en) "
                            "VALUES (?, ?, ?)", (ctx.consulta, int(batch_id), time.time()))
            ctx.con.commit()
        except BaseException:
            ctx.con.rollback()
            raise
    ctx.log(f"[lote {batch_id}] {len(eventos)} eventos ({len(nuevos)} nuevos), "
            f"{len(usuarios)} usuarios actualizados")
    return len(nuevos)


# --------------------------------------------------------------------------- #
# Reentrenamiento periódico
# --------------------------------------------------------------------------- #

def _escribir_eventos_parquet(pdf, destino: Path) -> None:
    pdf = pdf.astype({"userId": "int32", "movieId": "int32", "rating": "float64",
                      "timestamp": "int64"})
    pdf.to_parquet(destino, index=False)


def reentrenar(ctx: Contexto) -> dict:
    """Base + eventos nuevos -> ALS -> item_factors en carpeta versionada -> swap atómico.

    Usa su propia conexión a SQLite (corre en otro hilo). Devuelve un resumen con la
    versión nueva, la carpeta y el número de ratings usados.
    """
    from pyspark.sql import functions as F

    inicio = time.perf_counter()
    con = estado.conectar(ctx.estado_db)
    try:
        version = int(estado.get_meta(con, "version_modelo", 0) or 0) + 1
        modelos = ctx.artefactos / "modelos"
        carpeta = modelos / f"v{version:04d}"
        tmp = modelos / f".tmp_v{version:04d}_{int(time.time() * 1000)}"
        tmp.mkdir(parents=True, exist_ok=True)

        eventos = estado.todos_los_eventos(con)
        partes = []
        if ctx.base_reentreno is not None:
            partes.append(ctx.base_reentreno.select(
                F.col("userId").cast("int"), F.col("movieId").cast("int"),
                F.col("rating").cast("double")))
        if len(eventos):
            _escribir_eventos_parquet(eventos, tmp / "eventos.parquet")
            partes.append(ctx.spark.read.parquet(str(tmp / "eventos.parquet"))
                          .select("userId", "movieId", "rating"))
        if not partes:
            raise ValueError("No hay base ni eventos para reentrenar")
        datos = partes[0]
        for p in partes[1:]:
            datos = datos.unionByName(p)
        datos = datos.cache()
        n = datos.count()
        modelo = batch_als.entrenar(datos, ctx.params_als)
        items, _ = batch_als.factores(modelo)
        ctx.spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
        items.toPandas().to_parquet(tmp / "item_factors.parquet", index=False)
        datos.unpersist()
        (tmp / "eventos.parquet").unlink(missing_ok=True)
        info = {"version": version, "n_ratings": int(n), "n_eventos": int(len(eventos)),
                "modo": ctx.modo, "params": ctx.params_als, "creado_en": time.time()}
        (tmp / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
        if carpeta.exists():
            shutil.rmtree(carpeta)
        tmp.rename(carpeta)  # la carpeta aparece completa o no aparece

        with ctx.lock:
            # Swap atómico: puntero, versión y fecha cambian en una sola transacción.
            estado.set_meta(con, item_factors=str(carpeta / "item_factors.parquet"),
                            version_modelo=version, ultimo_reentrenamiento=time.time())
            # Las recomendaciones de los usuarios con eventos pasan a usar el modelo nuevo.
            usuarios = sorted({int(u) for u in eventos["userId"]}) if len(eventos) else []
            if usuarios:
                actualizar_usuarios(ctx, usuarios)
                ctx.con.commit()
        info["segundos"] = round(time.perf_counter() - inicio, 2)
        info["carpeta"] = str(carpeta)
        ctx.log(f"[reentrenamiento] versión {version} con {n:,} ratings "
                f"({len(eventos):,} eventos) en {info['segundos']} s")
        return info
    finally:
        con.close()
        if "tmp" in locals() and tmp.exists():
            shutil.rmtree(tmp, ignore_errors=True)


class Reentrenador(threading.Thread):
    """Hilo que reentrena ALS cada ``intervalo_s`` segundos si llegaron eventos nuevos."""

    def __init__(self, ctx: Contexto, intervalo_s: float):
        super().__init__(name="reentrenador-als", daemon=True)
        self.ctx = ctx
        self.intervalo_s = float(intervalo_s)
        self._detener = threading.Event()
        self._eventos_ultimo = -1
        self.errores: list[BaseException] = []
        self.versiones: list[int] = []

    def run(self) -> None:
        while not self._detener.wait(self.intervalo_s):
            try:
                con = estado.conectar(self.ctx.estado_db)
                n = estado.contar(con, "eventos")
                con.close()
                if n == 0 or n == self._eventos_ultimo:
                    continue  # nada nuevo desde el último reentrenamiento
                info = reentrenar(self.ctx)
                self._eventos_ultimo = n
                self.versiones.append(info["version"])
            except Exception as exc:  # el streaming sigue aunque falle un reentrenamiento
                self.errores.append(exc)
                self.ctx.log(f"[reentrenamiento] error: {exc!r}")

    def detener(self, esperar: float = 0) -> None:
        self._detener.set()
        if esperar:
            self.join(esperar)


# --------------------------------------------------------------------------- #
# Lectura del stream
# --------------------------------------------------------------------------- #

def ruta_checkpoint(artefactos, topic: str) -> Path:
    return Path(artefactos) / "checkpoints" / topic


def leer_stream(spark, fuente: str, topic: str | None = None, bootstrap: str | None = None,
                ruta=None, starting_offsets: str = "latest", formato: str = "json"):
    """DataFrame de streaming con columnas (userId, movieId, rating, timestamp)."""
    from pyspark.sql import functions as F

    if fuente == "kafka":
        crudo = (spark.readStream.format("kafka")
                 .option("kafka.bootstrap.servers", bootstrap or config.KAFKA_BOOTSTRAP)
                 .option("subscribe", topic or config.TOPIC)
                 .option("startingOffsets", starting_offsets)
                 .option("failOnDataLoss", "false")
                 .load())
        return (crudo.select(F.from_json(F.col("value").cast("string"), ESQUEMA_EVENTO)
                             .alias("e")).select("e.*"))
    if fuente == "archivo":
        if ruta is None:
            raise ValueError("La fuente archivo necesita una carpeta (--ruta)")
        lector = spark.readStream.schema(ESQUEMA_EVENTO)
        if formato == "csv":
            return lector.option("header", True).csv(str(ruta))
        return lector.json(str(ruta))
    raise ValueError(f"fuente {fuente!r} no válida (kafka o archivo)")


def iniciar_streaming(ctx: Contexto, fuente: str = "kafka", topic: str | None = None,
                      bootstrap: str | None = None, ruta=None, trigger_s: float = 5,
                      available_now: bool = False, starting_offsets: str = "latest",
                      checkpoint=None, formato: str = "json"):
    """Arranca la consulta de streaming y devuelve el ``StreamingQuery``.

    Con ``available_now=True`` procesa lo disponible (en Kafka, desde ``starting_offsets``)
    y termina solo: así funcionan la prueba de integración y la opción --availableNow.
    """
    topic = topic or config.TOPIC
    # Con fuente archivo el checkpoint va en checkpoints/archivo (no depende del topic).
    nombre = topic if fuente == "kafka" else "archivo"
    checkpoint = Path(checkpoint) if checkpoint else ruta_checkpoint(ctx.artefactos, nombre)
    checkpoint.parent.mkdir(parents=True, exist_ok=True)
    df = leer_stream(ctx.spark, fuente, topic, bootstrap, ruta, starting_offsets, formato)
    escritor = (df.writeStream
                .foreachBatch(lambda lote, batch_id: procesar_lote(lote, batch_id, ctx))
                .option("checkpointLocation", checkpoint.as_posix())
                .queryName(f"recomendador_{nombre}".replace("-", "_")))
    if available_now:
        escritor = escritor.trigger(availableNow=True)
    else:
        escritor = escritor.trigger(processingTime=f"{float(trigger_s):g} seconds")
    return escritor.start()


# --------------------------------------------------------------------------- #
# Construcción del contexto según el modo
# --------------------------------------------------------------------------- #

def crear_contexto(spark, artefactos, estado_db, modo: str = "completo", consulta="ratings",
                   datos=None, corte: str | None = None, log=print) -> Contexto:
    """Arma el Contexto leyendo historia, base de reentrenamiento y géneros.

    - historia: ratings anteriores al corte en ``datos`` (si existe la carpeta); en modo
      demo sin datos, se usa base_demo.parquet.
    - base de reentrenamiento: en modo demo, ``<artefactos>/base_demo.parquet``; en modo
      completo, la historia completa.
    """
    from .datos import antes_de, cargar_movielens

    artefactos = Path(artefactos)
    corte = corte or config.FECHA_CORTE
    historia, generos = None, {}
    if datos is not None and Path(datos).is_dir():
        tablas = cargar_movielens(datos, spark, ["ratings"])
        historia = antes_de(tablas["ratings"], corte).select("userId", "movieId", "rating")
        if (Path(datos) / "movies.csv").exists():
            generos = perfiles.generos_por_pelicula(datos)
    base = None
    if modo == "demo":
        ruta_base = artefactos / "base_demo.parquet"
        if not ruta_base.exists():
            raise FileNotFoundError(f"No existe {ruta_base}: corre scripts/preparar_demo.py")
        base = spark.read.parquet(str(ruta_base)).select("userId", "movieId", "rating")
        if historia is None:
            historia = base
    if historia is not None:
        historia = historia.cache()
    return Contexto(spark, artefactos, estado_db, modo=modo, consulta=consulta,
                    historia=historia, base_reentreno=base, generos=generos, log=log)
