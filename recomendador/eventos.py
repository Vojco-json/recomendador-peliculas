"""Publicación de las acciones de la app en Kafka, con respaldo directo a estado.db.

- ``publicar_rating``: topic 'ratings' (config.TOPIC), MISMO formato que el productor
  verificado: ``{"userId","movieId","rating","timestamp"}`` en ese orden, sin espacios.
- ``publicar_resena``: topic 'resenas', ``{"uid","userId","movieId","texto","timestamp"}``.
  Antes de publicar pasa el filtro de moderacion.revisar_resena (500 caracteres y
  palabras ofensivas); si no lo pasa, no se publica y se devuelve el motivo.
- En ambos casos la key es ``str(userId)``: los eventos de un usuario van a la misma
  partición y llegan en orden.
- Si Kafka no confirma en 3 s (o no hay cliente), el evento se escribe DIRECTO en
  estado.db y se devuelve un aviso para mostrarle al usuario. Tras una falla, durante
  30 s se va directo al respaldo para no hacer esperar 3 s en cada clic.

El productor se puede inyectar (``productor=``) para probar sin broker: basta un objeto
con ``produce(topic, value=, key=, on_delivery=)`` y ``flush(timeout) -> pendientes``.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from . import config, estado, moderacion

TOPIC_RATINGS = config.TOPIC
TOPIC_RESENAS = os.environ.get("TOPIC_RESENAS", "resenas")
CAMPOS_RATING = ("userId", "movieId", "rating", "timestamp")
CAMPOS_RESENA = ("uid", "userId", "movieId", "texto", "timestamp")
TIMEOUT_S = 3.0
PAUSA_TRAS_FALLA_S = 30.0

AVISO_RESPALDO = ("No pudimos conectar con el servidor de eventos (Kafka). Tu {que} se "
                  "guardó directamente y ya cuenta; no tienes que hacer nada más.")


@dataclass
class Resultado:
    """Qué pasó con una acción publicada.

    ok:      la acción quedó registrada (por Kafka o por el respaldo).
    via:     'kafka', 'respaldo' o 'rechazada' (reseña que no pasó el filtro).
    aviso:   texto para el usuario ('' si todo fue normal).
    evento:  el dict publicado.
    """
    ok: bool
    via: str
    aviso: str = ""
    evento: dict = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Formato de los mensajes
# --------------------------------------------------------------------------- #

def json_rating(evento: dict) -> str:
    """Igual que scripts/productor_kafka.a_json: 4 claves en orden fijo, sin espacios."""
    return json.dumps({c: evento[c] for c in CAMPOS_RATING}, separators=(",", ":"))


def json_resena(resena: dict) -> str:
    return json.dumps({c: resena[c] for c in CAMPOS_RESENA}, separators=(",", ":"),
                      ensure_ascii=False)


def evento_rating(userId, movieId, rating, timestamp=None) -> dict:
    rating = float(rating)
    if not 0.5 <= rating <= 5.0 or abs(rating * 2 - round(rating * 2)) > 1e-9:
        raise ValueError(f"rating {rating} fuera de la escala 0.5-5.0 en pasos de 0.5")
    return {"userId": int(userId), "movieId": int(movieId), "rating": rating,
            "timestamp": int(time.time() if timestamp is None else timestamp)}


# --------------------------------------------------------------------------- #
# Productor de Kafka (uno por proceso, perezoso)
# --------------------------------------------------------------------------- #

_lock = threading.Lock()
_productor = None
_caido_hasta = 0.0


def _productor_defecto(bootstrap: str | None = None):
    """Producer de confluent_kafka compartido; None si no hay cliente instalado."""
    global _productor
    with _lock:
        if _productor is None:
            try:
                from confluent_kafka import Producer
            except ImportError:
                return None
            _productor = Producer({
                "bootstrap.servers": bootstrap or config.KAFKA_BOOTSTRAP,
                "acks": "all",
                "message.timeout.ms": int(TIMEOUT_S * 1000),
                "socket.timeout.ms": int(TIMEOUT_S * 1000),
                "log_level": 0,
            })
        return _productor


def _enviar(topic: str, valor: str, clave: str, productor=None,
            timeout: float = TIMEOUT_S) -> bool:
    """True si el broker confirmó el mensaje dentro del plazo. Nunca lanza excepciones."""
    global _caido_hasta
    propio = productor is None
    if propio and time.monotonic() < _caido_hasta:
        return False                      # falló hace poco: directo al respaldo
    try:
        prod = productor if productor is not None else _productor_defecto()
        if prod is None:
            return False
        resultado = {"err": "sin confirmación"}

        def _entrega(err, _msg):
            resultado["err"] = err

        prod.produce(topic, value=valor.encode("utf-8"), key=clave.encode("utf-8"),
                     on_delivery=_entrega)
        pendientes = prod.flush(timeout)
        ok = not pendientes and resultado["err"] is None
    except Exception:
        ok = False
    if propio:
        _caido_hasta = 0.0 if ok else time.monotonic() + PAUSA_TRAS_FALLA_S
    return ok


def _con_estado(estado_db):
    return estado.conectar(Path(estado_db) if estado_db else config.ESTADO_DB)


# --------------------------------------------------------------------------- #
# API para la app
# --------------------------------------------------------------------------- #

def publicar_rating(userId, movieId, rating, timestamp=None, *, productor=None,
                    estado_db=None, timeout: float = TIMEOUT_S) -> Resultado:
    """Publica una calificación en 'ratings'; si Kafka falla, la guarda en estado.db."""
    evento = evento_rating(userId, movieId, rating, timestamp)
    if _enviar(TOPIC_RATINGS, json_rating(evento), str(evento["userId"]), productor, timeout):
        return Resultado(True, "kafka", "", evento)
    con = _con_estado(estado_db)
    try:
        estado.guardar_eventos(con, [evento])
    finally:
        con.close()
    return Resultado(True, "respaldo", AVISO_RESPALDO.format(que="calificación"), evento)


def publicar_resena(userId, movieId, texto, timestamp=None, *, productor=None,
                    estado_db=None, timeout: float = TIMEOUT_S) -> Resultado:
    """Revisa la reseña y la publica en 'resenas'; si Kafka falla, la guarda en estado.db.

    Si no pasa el filtro (más de 500 caracteres, vacía u ofensiva) NO se publica ni se
    guarda: devuelve ok=False, via='rechazada' y el motivo en ``aviso``.
    """
    revision = moderacion.revisar_resena(texto)
    resena = {"uid": uuid.uuid4().hex, "userId": int(userId), "movieId": int(movieId),
              "texto": revision.texto,
              "timestamp": int(time.time() if timestamp is None else timestamp)}
    if not revision.ok:
        return Resultado(False, "rechazada", revision.mensaje, resena)
    if _enviar(TOPIC_RESENAS, json_resena(resena), str(resena["userId"]), productor, timeout):
        return Resultado(True, "kafka", "", resena)
    con = _con_estado(estado_db)
    try:
        estado.guardar_resenas(con, [resena])
    finally:
        con.close()
    return Resultado(True, "respaldo", AVISO_RESPALDO.format(que="reseña"), resena)
