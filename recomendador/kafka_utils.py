"""Utilidades de Kafka: detección del broker y administración de topics.

No levanta ni toca contenedores: solo se conecta al broker que ya corre
(por defecto localhost:9092, ver config.KAFKA_BOOTSTRAP).
"""

from __future__ import annotations

import socket

from . import config


def _primer_host(bootstrap: str) -> tuple[str, int]:
    """'localhost:9092,otro:9093' -> ('localhost', 9092)."""
    primero = bootstrap.split(",")[0].strip()
    host, _, puerto = primero.rpartition(":")
    return (host or "localhost"), int(puerto or 9092)


def _admin(bootstrap: str):
    from confluent_kafka.admin import AdminClient

    # log_level 0 evita que librdkafka llene la consola si el broker no responde.
    return AdminClient({"bootstrap.servers": bootstrap, "log_level": 0,
                        "socket.timeout.ms": 3000})


def kafka_disponible(timeout: float = 3, bootstrap: str | None = None) -> bool:
    """True si el broker responde (TCP + metadatos). Nunca lanza excepciones."""
    bootstrap = bootstrap or config.KAFKA_BOOTSTRAP
    try:
        host, puerto = _primer_host(bootstrap)
        with socket.create_connection((host, puerto), timeout=timeout):
            pass
    except Exception:
        return False
    try:
        import confluent_kafka  # noqa: F401
    except Exception:
        # Sin cliente instalado solo podemos confirmar que el puerto responde.
        return True
    try:
        metadatos = _admin(bootstrap).list_topics(timeout=timeout)
        return metadatos is not None and len(metadatos.brokers) > 0
    except Exception:
        return False


def listar_topics(bootstrap: str | None = None, timeout: float = 5) -> set[str]:
    """Nombres de los topics existentes."""
    return set(_admin(bootstrap or config.KAFKA_BOOTSTRAP).list_topics(timeout=timeout).topics)


def crear_topic(nombre: str, particiones: int | None = None, replicacion: int = 1,
                bootstrap: str | None = None, timeout: float = 10) -> bool:
    """Crea el topic si no existe (idempotente).

    Devuelve True si lo creó y False si ya existía.
    """
    from confluent_kafka import KafkaError, KafkaException
    from confluent_kafka.admin import NewTopic

    bootstrap = bootstrap or config.KAFKA_BOOTSTRAP
    particiones = particiones or config.TOPIC_PARTICIONES
    admin = _admin(bootstrap)
    if nombre in admin.list_topics(timeout=timeout).topics:
        return False
    futuros = admin.create_topics([NewTopic(nombre, num_partitions=particiones,
                                            replication_factor=replicacion)])
    try:
        futuros[nombre].result(timeout)
        return True
    except KafkaException as exc:
        error = exc.args[0] if exc.args else None
        if isinstance(error, KafkaError) and error.code() == KafkaError.TOPIC_ALREADY_EXISTS:
            return False
        raise


def borrar_topic(nombre: str, bootstrap: str | None = None, timeout: float = 10) -> bool:
    """Borra un topic (lo usan las pruebas con topics únicos). True si lo borró."""
    admin = _admin(bootstrap or config.KAFKA_BOOTSTRAP)
    if nombre not in admin.list_topics(timeout=timeout).topics:
        return False
    admin.delete_topics([nombre], operation_timeout=timeout)[nombre].result(timeout)
    return True
