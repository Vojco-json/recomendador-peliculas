"""Fixtures compartidas de la suite de pruebas (todas usan la muestra ml-mini).

- spark:     una sola SparkSession por proceso (scope session, local[2], con conector Kafka).
             Si el conector no se puede resolver (sin internet y sin caché .ivy2), se
             crea sin conector y se avisa; las pruebas que lo necesiten deben saltarse.
- ruta_mini: carpeta muestras/ml-mini.
- kafka_ok:  True si el broker responde (kafka_disponible).
- requiere_kafka: salta la prueba con 'Kafka no disponible' si el broker no responde.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config, kafka_utils, spark_utils  # noqa: E402

import faulthandler  # noqa: E402
import os  # noqa: E402

# Límite por prueba (incluye crear sus fixtures, p. ej. la SparkSession). Si una
# prueba se cuelga (red, JVM, broker), se vuelcan las pilas de todos los hilos y el
# proceso termina con error, en vez de quedar bloqueado indefinidamente.
LIMITE_POR_PRUEBA_S = float(os.environ.get("PRUEBAS_LIMITE_S", "100"))


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Arma un temporizador de seguridad alrededor de cada prueba."""
    if LIMITE_POR_PRUEBA_S > 0:
        faulthandler.dump_traceback_later(LIMITE_POR_PRUEBA_S, exit=True)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


@pytest.fixture(scope="session")
def spark():
    """SparkSession compartida: local[2], 6g, con conector Kafka cacheado."""
    try:
        sesion = spark_utils.crear_spark(nucleos=2, kafka=True, app="pruebas")
        sesion.conector_kafka_ok = True
    except Exception as exc:  # p. ej. sin internet la primera vez
        warnings.warn(f"No se pudo cargar el conector Kafka ({exc}); Spark sin conector")
        spark_utils.detener_spark()
        sesion = spark_utils.crear_spark(nucleos=2, kafka=False, app="pruebas")
        sesion.conector_kafka_ok = False
    yield sesion
    sesion.stop()


@pytest.fixture(scope="session")
def ruta_mini() -> Path:
    return config.MUESTRA_MINI


@pytest.fixture(scope="session")
def kafka_ok() -> bool:
    return kafka_utils.kafka_disponible(timeout=3)


@pytest.fixture
def requiere_kafka(kafka_ok):
    """Salta la prueba solo si el broker no responde."""
    if not kafka_ok:
        pytest.skip("Kafka no disponible")
