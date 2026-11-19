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
import time  # noqa: E402

# Límite por prueba (incluye crear sus fixtures, p. ej. la SparkSession). Si una
# prueba se cuelga (red, JVM, broker), se vuelcan las pilas de todos los hilos y el
# proceso termina con error, en vez de quedar bloqueado indefinidamente.
LIMITE_POR_PRUEBA_S = float(os.environ.get("PRUEBAS_LIMITE_S", "100"))

# faulthandler termina con os._exit, que descarta lo que haya en el buffer de stdout.
# Con buffer por línea, la salida parcial de pytest sobrevive y la falla queda diagnosticable.
try:
    sys.stdout.reconfigure(line_buffering=True)
except (AttributeError, ValueError):
    pass


# Arrancar la JVM de Spark en frío (primer uso tras reiniciar, antivirus revisando los
# jars) puede tardar bastante más que una prueba normal. El arranque tiene su propio
# presupuesto y, al terminar, el temporizador de la prueba se rearma desde cero.
# Es la última red de seguridad: debe cubrir los reintentos del arranque vigilado.
LIMITE_ARRANQUE_SPARK_S = float(os.environ.get("PRUEBAS_LIMITE_SPARK_S", "200"))

# Plazo del primer intento de arranque de Spark (los siguientes tienen 2x y 3x): si la
# JVM no responde a tiempo, el intento se aborta y se reintenta (ver
# spark_utils.crear_spark_vigilado). Un arranque normal tarda ~10 s; 20 + 40 + 60 s
# caben en LIMITE_ARRANQUE_SPARK_S.
PLAZO_INTENTO_SPARK_S = float(os.environ.get("PRUEBAS_PLAZO_INTENTO_SPARK_S", "20"))

# Si el temporizador vence, las pilas se escriben en este archivo (además de stderr),
# porque el ejecutor de pruebas muestra solo la cola de stdout y el volcado se perdía.
# Se abre en modo 'a' (anexar) con un encabezado por sesión: una suite que se cuelga
# no pierde su volcado cuando después corre otra.
_LOG_COLGADAS = RAIZ / "artefactos" / "pruebas_colgadas.log"
try:
    _LOG_COLGADAS.parent.mkdir(parents=True, exist_ok=True)
    _archivo_volcado = open(_LOG_COLGADAS, "a", encoding="utf-8")  # noqa: SIM115
    _archivo_volcado.write(f"\n===== sesión pytest {time.strftime('%Y-%m-%d %H:%M:%S')} "
                           f"pid={os.getpid()} args={' '.join(sys.argv[1:])} =====\n")
    _archivo_volcado.flush()
except OSError:
    _archivo_volcado = sys.stderr


def _armar_temporizador(segundos: float) -> None:
    """(Re)arma el temporizador de seguridad; reemplaza al anterior si existía."""
    if segundos > 0:
        faulthandler.dump_traceback_later(segundos, exit=True, file=_archivo_volcado)


def pytest_configure(config):
    """Registra los markers para correr la regresión en tandas cortas.

    - spark: la prueba usa la SparkSession (se marca sola, ver abajo).
    - lento: además lanza un proceso aparte o entrena modelos (decenas de segundos).

    Tandas sugeridas (cada una bajo 3 minutos):
        python -m pytest -q -m "not spark"          # todo lo rápido
        python -m pytest -q -m "spark and not lento"
        python -m pytest -q -m lento
    """
    config.addinivalue_line("markers", "spark: usa la SparkSession compartida (JVM)")
    config.addinivalue_line("markers", "lento: prueba lenta (subproceso, JVM propia o "
                                       "entrenamiento); correrla en su propia tanda")


def pytest_collection_modifyitems(config, items):
    """Marca con 'spark' toda prueba que pide la fixture spark (directa o indirectamente)."""
    for item in items:
        if "spark" in getattr(item, "fixturenames", ()):
            item.add_marker(pytest.mark.spark)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    """Arma un temporizador de seguridad alrededor de cada prueba."""
    if _archivo_volcado is not sys.stderr:
        _archivo_volcado.write(f"\n>>> {item.nodeid}\n")
        _archivo_volcado.flush()
    _armar_temporizador(LIMITE_POR_PRUEBA_S)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()


# Ajustes de la sesión de pruebas: sin UI (arranca más rápido y no ocupa puertos).
OPCIONES_SPARK_PRUEBAS = {"spark.ui.enabled": "false"}


def _crear_sesion_pruebas():
    """Crea la SparkSession de pruebas (con conector Kafka si está disponible).

    El arranque es vigilado: si la JVM queda colgada al lanzarse (falla intermitente
    de pyspark en Windows), el intento se aborta, se anota en el log y se reintenta.
    """
    opciones = dict(nucleos=2, app="pruebas", extra=OPCIONES_SPARK_PRUEBAS,
                    plazo_s=PLAZO_INTENTO_SPARK_S, registro=_archivo_volcado)
    try:
        sesion = spark_utils.crear_spark_vigilado(kafka=True, **opciones)
        sesion.conector_kafka_ok = True
    except Exception as exc:  # p. ej. sin internet la primera vez
        warnings.warn(f"No se pudo cargar el conector Kafka ({exc}); Spark sin conector")
        spark_utils.detener_spark()
        sesion = spark_utils.crear_spark_vigilado(kafka=False, **opciones)
        sesion.conector_kafka_ok = False
    return sesion


@pytest.fixture(scope="session")
def spark():
    """SparkSession compartida: local[2], 6g, con conector Kafka cacheado, sin UI."""
    _armar_temporizador(LIMITE_ARRANQUE_SPARK_S)   # presupuesto propio para el arranque
    sesion = _crear_sesion_pruebas()
    _armar_temporizador(LIMITE_POR_PRUEBA_S)       # la prueba parte con su límite completo
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
