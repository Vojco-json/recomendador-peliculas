"""Pruebas del paquete 5: streaming -> SQLite (fuente archivo y Kafka real).

- Fuente archivo (sin Kafka): Structured Streaming lee JSON de una carpeta y foreachBatch
  llama a procesar_lote; se verifica el estado, la deduplicación y el checkpoint.
- Integración REAL: productor_kafka.publicar -> topic único ratings_test_<uuid> ->
  Structured Streaming (startingOffsets=earliest, Trigger.availableNow) -> SQLite.
  Se salta con 'Kafka no disponible' solo si el broker no responde.
"""

from __future__ import annotations

import time
import uuid

import pandas as pd
import pytest

from base_mini import CORTE, crear_base_mini
from recomendador import estado, kafka_utils, streaming
from scripts.productor_kafka import a_json, leer_eventos, publicar

ESPERA_S = 90


@pytest.fixture(scope="module")
def base(spark, ruta_mini, tmp_path_factory):
    return crear_base_mini(spark, ruta_mini, tmp_path_factory.mktemp("base_stream"))


@pytest.fixture(scope="module")
def eventos_post_corte(ruta_mini):
    return list(leer_eventos(ruta_mini, desde=CORTE))


@pytest.fixture(scope="module")
def vistas_mini(ruta_mini):
    r = pd.read_csv(ruta_mini / "ratings.csv")
    return r.groupby("userId")["movieId"].apply(set).to_dict()


def _contexto(spark, base, ruta_mini, db, consulta):
    return streaming.crear_contexto(spark, base, db, "completo", consulta=consulta,
                                    datos=ruta_mini, corte=CORTE, log=lambda *_: None)


def _verificar_estado(con, vistas, desde_t):
    """15 usuarios con perfil y top-N por fold-in, sin vistas y actualizados."""
    assert estado.contar(con, "eventos") == 90
    assert estado.contar(con, "perfiles") == 15
    for u in range(1, 16):
        recs = estado.leer_recomendaciones(con, u)
        # Cada usuario calificó 20 de 25 películas: quedan 5 sin ver para el top-10.
        assert 1 <= len(recs) <= 10, (u, recs)
        assert [r["rank"] for r in recs] == list(range(1, len(recs) + 1))
        assert not {r["movieId"] for r in recs} & vistas[u], u
        assert estado.ultima_actualizacion(con, u) >= desde_t


def test_esquema_wal(tmp_path):
    con = estado.conectar(tmp_path / "estado.db")
    assert estado.modo_journal(con) == "wal"
    cols = [c[1] for c in con.execute("PRAGMA table_info(recomendaciones)")]
    assert cols == ["userId", "movieId", "rank", "score", "actualizado_en"]
    estado.set_meta(con, version_modelo=0, modo="demo", ultimo_reentrenamiento=None)
    assert {"version_modelo", "modo", "ultimo_reentrenamiento"} <= set(estado.meta(con))
    e = {"userId": 1, "movieId": 2, "rating": 4.0, "timestamp": 10}
    assert len(estado.guardar_eventos(con, [e, e])) == 1
    con.close()


def test_fuente_archivo_procesar_lote(spark, base, ruta_mini, tmp_path, eventos_post_corte,
                                      vistas_mini):
    entrada = tmp_path / "entrada"
    entrada.mkdir()
    (entrada / "eventos.json").write_text(
        "\n".join(a_json(e) for e in eventos_post_corte) + "\n", encoding="utf-8")
    db = tmp_path / "estado.db"
    ctx = _contexto(spark, base, ruta_mini, db, "archivo")
    t0 = time.time()
    q = streaming.iniciar_streaming(ctx, "archivo", ruta=entrada, available_now=True)
    assert q.awaitTermination(ESPERA_S), "el streaming de archivos no terminó a tiempo"
    assert q.exception() is None
    assert (base / "checkpoints" / "archivo").is_dir()
    _verificar_estado(ctx.con, vistas_mini, t0)

    # Perfil incremental: usuario 1 sigue siendo de acción/thriller.
    p1 = estado.leer_perfil(ctx.con, 1)
    assert set(p1["top_generos"][:2]) == {"Action", "Thriller"}
    assert p1["n_ratings"] == 20

    # procesar_lote directo: un lote repetido no se reaplica y los eventos no se duplican.
    lotes = estado.contar(ctx.con, "lotes")
    df = spark.read.schema(streaming.ESQUEMA_EVENTO).json(str(entrada))
    assert streaming.procesar_lote(df, 0, ctx) == 0           # batch_id ya aplicado
    assert streaming.procesar_lote(df, 999, ctx) == 0         # eventos repetidos
    assert estado.contar(ctx.con, "eventos") == 90
    assert estado.leer_perfil(ctx.con, 1)["n_ratings"] == 20

    # Reinicio con el mismo checkpoint: no hay lotes nuevos ni duplicados.
    q2 = streaming.iniciar_streaming(ctx, "archivo", ruta=entrada, available_now=True)
    assert q2.awaitTermination(ESPERA_S)
    assert estado.contar(ctx.con, "lotes") == lotes + 1      # solo el 999 manual
    assert estado.contar(ctx.con, "eventos") == 90
    ctx.cerrar()


def test_integracion_kafka_real(spark, base, ruta_mini, tmp_path, requiere_kafka,
                                eventos_post_corte, vistas_mini):
    if not getattr(spark, "conector_kafka_ok", False):
        pytest.fail("El broker responde pero Spark no tiene el conector spark-sql-kafka")
    topic = f"ratings_test_{uuid.uuid4().hex[:12]}"
    kafka_utils.crear_topic(topic, particiones=2)
    try:
        assert publicar(eventos_post_corte, topic) == 90
        db = tmp_path / "estado.db"
        ctx = _contexto(spark, base, ruta_mini, db, topic)
        t0 = time.time()
        q = streaming.iniciar_streaming(ctx, "kafka", topic=topic, available_now=True,
                                        starting_offsets="earliest")
        assert q.awaitTermination(ESPERA_S), "el streaming de Kafka no terminó a tiempo"
        assert q.exception() is None
        assert (base / "checkpoints" / topic).is_dir()
        _verificar_estado(ctx.con, vistas_mini, t0)

        # Reinicio con el mismo checkpoint: no relee ni duplica.
        lotes = estado.contar(ctx.con, "lotes")
        q2 = streaming.iniciar_streaming(ctx, "kafka", topic=topic, available_now=True,
                                         starting_offsets="earliest")
        assert q2.awaitTermination(ESPERA_S)
        assert estado.contar(ctx.con, "eventos") == 90
        assert estado.contar(ctx.con, "lotes") == lotes
        ctx.cerrar()
    finally:
        try:
            kafka_utils.borrar_topic(topic)
        except Exception:
            pass
