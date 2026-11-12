"""Pruebas del paquete de ingesta (muestra ml-mini).

- Orden temporal, formato JSON, filtros --desde/--hasta y --limite del productor.
- CLI: dry-run, errores claros sin traceback (Kafka caído, datos inexistentes).
- Descarga: parseo y verificación de md5, extracción segura y --dry-run sin red.
- ordenar_ratings con Spark (se salta si falta pyspark o hadoop.dll en Windows).
- Con broker disponible: crear_topic idempotente e ida y vuelta real de 5 mensajes
  por un topic de prueba único (se salta con 'Kafka no disponible' si no responde).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
import zipfile
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador.datos import fecha_a_epoch  # noqa: E402
from scripts import descargar_movielens as desc  # noqa: E402
from scripts import productor_kafka as prod  # noqa: E402

SCRIPTS = RAIZ / "scripts"
MINI = RAIZ / "muestras" / "ml-mini"


def _correr(script: str, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    """Ejecuta un script del proyecto con el mismo Python de las pruebas."""
    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    return subprocess.run([sys.executable, str(SCRIPTS / script), *args], cwd=RAIZ,
                          capture_output=True, text=True, encoding="utf-8",
                          timeout=timeout, env=entorno)


# --------------------------------------------------------------------------- #
# Productor: lectura, orden y formato
# --------------------------------------------------------------------------- #

def test_eventos_ordenados_por_timestamp():
    eventos = list(prod.leer_eventos(MINI))
    assert len(eventos) == 300
    tiempos = [e["timestamp"] for e in eventos]
    assert tiempos == sorted(tiempos)
    assert tiempos[0] == 1262404000 and tiempos[-1] == 1547869200
    # Orden determinista ante empates: (timestamp, userId, movieId)
    claves = [(e["timestamp"], e["userId"], e["movieId"]) for e in eventos]
    assert claves == sorted(claves)


def test_formato_json():
    evento = next(prod.leer_eventos(MINI, limite=1))
    datos = json.loads(prod.a_json(evento))
    assert list(datos) == ["userId", "movieId", "rating", "timestamp"]
    assert isinstance(datos["userId"], int) and isinstance(datos["movieId"], int)
    assert isinstance(datos["rating"], float) and 0.5 <= datos["rating"] <= 5.0
    assert isinstance(datos["timestamp"], int)


def test_filtro_desde_y_hasta():
    desde_corte = list(prod.leer_eventos(MINI, desde="2016-01-01"))
    assert len(desde_corte) == 90  # valor documentado en muestras/ml-mini/LEEME.md
    assert min(e["timestamp"] for e in desde_corte) >= fecha_a_epoch("2016-01-01")
    assert len(list(prod.leer_eventos(MINI, desde="2018-01-01"))) == 30
    assert len(list(prod.leer_eventos(MINI, desde="2015-01-01"))) == 180
    # --hasta es inclusivo (todo ese día): 2015-01-01..2015-12-31 = 90
    assert len(list(prod.leer_eventos(MINI, desde="2015-01-01", hasta="2015-12-31"))) == 90
    with pytest.raises(ValueError):
        list(prod.leer_eventos(MINI, desde="2018-01-01", hasta="2017-01-01"))


def test_limite():
    assert len(list(prod.leer_eventos(MINI, limite=5))) == 5
    assert len(list(prod.leer_eventos(MINI, desde="2018-01-01", limite=500))) == 30


def test_ritmo_velocidad_y_acelerar():
    a = {"timestamp": 1000}
    b = {"timestamp": 1360}
    assert prod.pausa(a, b, velocidad=0) == 0
    assert prod.pausa(a, b, velocidad=20) == pytest.approx(0.05)
    assert prod.pausa(a, b, velocidad=20, acelerar=3600) == pytest.approx(0.1)
    assert prod.pausa(None, b, acelerar=3600) == 0
    # 6 eventos a 50/seg => ~5 pausas de 20 ms
    inicio = time.perf_counter()
    assert len(list(prod.con_ritmo(prod.leer_eventos(MINI, limite=6), velocidad=50))) == 6
    transcurrido = time.perf_counter() - inicio
    assert 0.08 <= transcurrido < 2


# --------------------------------------------------------------------------- #
# Productor: línea de comandos
# --------------------------------------------------------------------------- #

def test_cli_dry_run_emite_5_eventos_ordenados():
    r = _correr("productor_kafka.py", "--datos", "muestras/ml-mini", "--dry-run",
                "--limite", "5")
    assert r.returncode == 0, r.stderr
    lineas = [json.loads(l) for l in r.stdout.strip().splitlines()]
    assert len(lineas) == 5
    tiempos = [e["timestamp"] for e in lineas]
    assert tiempos == sorted(tiempos)
    assert set(lineas[0]) == {"userId", "movieId", "rating", "timestamp"}


def test_cli_dry_run_con_desde():
    r = _correr("productor_kafka.py", "--datos", str(MINI), "--dry-run",
                "--desde", "2018-01-01")
    assert r.returncode == 0, r.stderr
    eventos = [json.loads(l) for l in r.stdout.strip().splitlines()]
    assert len(eventos) == 30
    assert all(e["timestamp"] >= fecha_a_epoch("2018-01-01") for e in eventos)


def test_cli_kafka_caido_mensaje_claro_sin_traceback():
    # Puerto 1 en localhost: nadie escucha, la conexión se rechaza al instante.
    r = _correr("productor_kafka.py", "--datos", str(MINI), "--bootstrap", "127.0.0.1:1",
                "--limite", "3")
    assert r.returncode != 0
    assert "Kafka no responde" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_cli_crear_topic_kafka_caido_sin_traceback():
    r = _correr("crear_topic.py", "--bootstrap", "127.0.0.1:1")
    assert r.returncode != 0
    assert "Kafka no responde" in r.stderr
    assert "Traceback" not in r.stderr + r.stdout


def test_cli_datos_inexistentes_sin_traceback(tmp_path):
    r = _correr("productor_kafka.py", "--datos", str(tmp_path / "no_existe"), "--dry-run")
    assert r.returncode != 0
    assert "ERROR" in r.stderr and "Traceback" not in r.stderr


def test_cli_fecha_invalida():
    r = _correr("productor_kafka.py", "--datos", str(MINI), "--dry-run", "--desde", "2018-13-01")
    assert r.returncode != 0 and "Traceback" not in r.stderr


# --------------------------------------------------------------------------- #
# Descarga de MovieLens (sin red)
# --------------------------------------------------------------------------- #

def test_leer_md5_formatos():
    h = "6b51fb2759a8657d3bfcbfc42b592ada"
    assert desc.leer_md5(f"MD5 (ml-25m.zip) = {h}\n") == h
    assert desc.leer_md5(f"{h.upper()}  ml-25m.zip") == h
    with pytest.raises(ValueError):
        desc.leer_md5("sin hash")


def test_md5_archivo_y_descompresion(tmp_path):
    contenido = tmp_path / "x.zip"
    with zipfile.ZipFile(contenido, "w") as z:
        for nombre in desc.ARCHIVOS_ESPERADOS:
            z.writestr(f"ml-25m/{nombre}", "a,b\n1,2\n")
    import hashlib

    assert desc.md5_archivo(contenido) == hashlib.md5(contenido.read_bytes()).hexdigest()
    desc.descomprimir(contenido, tmp_path)
    assert desc.datos_completos(tmp_path / "ml-25m")

    malicioso = tmp_path / "malo.zip"
    with zipfile.ZipFile(malicioso, "w") as z:
        z.writestr("../fuera.txt", "x")
    with pytest.raises(ValueError):
        desc.descomprimir(malicioso, tmp_path / "dest")


def test_descarga_dry_run_no_toca_red_ni_disco(tmp_path):
    destino = tmp_path / "data"
    r = _correr("descargar_movielens.py", "--destino", str(destino), "--dry-run")
    assert r.returncode == 0, r.stderr
    assert "ml-25m.zip" in r.stdout and "md5" in r.stdout
    assert not destino.exists()


def test_descarga_no_se_repite_si_ya_existe(tmp_path):
    carpeta = tmp_path / "ml-25m"
    carpeta.mkdir()
    for nombre in desc.ARCHIVOS_ESPERADOS:
        (carpeta / nombre).write_text("x")
    # URL inválida: si intentara descargar, fallaría.
    r = _correr("descargar_movielens.py", "--destino", str(tmp_path),
                "--url", "http://127.0.0.1:1/ml-25m.zip")
    assert r.returncode == 0, r.stderr
    assert "no se descarga" in r.stdout


# --------------------------------------------------------------------------- #
# ordenar_ratings con Spark
# --------------------------------------------------------------------------- #

def _hadoop_listo() -> bool:
    if os.name != "nt":
        return True
    hh = os.environ.get("HADOOP_HOME")
    return bool(hh) and (Path(hh) / "bin" / "hadoop.dll").exists() \
        and (Path(hh) / "bin" / "winutils.exe").exists()


def test_ordenar_ratings_spark(request, tmp_path):
    pytest.importorskip("pyspark")
    if not _hadoop_listo():
        pytest.skip("Faltan HADOOP_HOME/winutils.exe/hadoop.dll: Spark no puede escribir parquet")
    spark = request.getfixturevalue("spark")  # SparkSession compartida (conftest)
    from scripts import ordenar_ratings

    destino, n = ordenar_ratings.ordenar(MINI, tmp_path, spark, archivos=3)
    assert n == 300
    assert destino == tmp_path / "ratings_ordenados.parquet"
    assert list(destino.glob("part-*.parquet"))

    # El productor lee el parquet (por nombre de archivo) en el mismo orden que el CSV.
    pytest.importorskip("pyarrow")
    desde_parquet = list(prod.leer_eventos(destino, fuente="parquet"))
    desde_csv = list(prod.leer_eventos(MINI, fuente="csv"))
    assert desde_parquet == desde_csv
    assert len(list(prod.leer_eventos(destino, desde="2016-01-01"))) == 90


# --------------------------------------------------------------------------- #
# Integración real con Kafka (se salta si no hay broker)
# --------------------------------------------------------------------------- #

@pytest.fixture
def topic_unico(requiere_kafka):
    """Topic 'ratings_test_<uuid>' que se borra al terminar."""
    from recomendador import kafka_utils

    nombre = f"ratings_test_{uuid.uuid4().hex[:12]}"
    yield nombre
    try:
        kafka_utils.borrar_topic(nombre)
    except Exception:
        pass


def test_crear_topic_idempotente(topic_unico):
    from recomendador import kafka_utils

    assert kafka_utils.crear_topic(topic_unico, particiones=2) is True
    assert kafka_utils.crear_topic(topic_unico, particiones=2) is False
    r = _correr("crear_topic.py", "--topic", topic_unico)
    assert r.returncode == 0 and "ya existía" in r.stdout


def test_ida_y_vuelta_5_mensajes(topic_unico):
    from confluent_kafka import Consumer

    from recomendador import config, kafka_utils

    kafka_utils.crear_topic(topic_unico, particiones=1)
    eventos = list(prod.leer_eventos(MINI, desde="2016-01-01", limite=5))
    assert prod.publicar(eventos, topic_unico, config.KAFKA_BOOTSTRAP) == 5

    consumidor = Consumer({"bootstrap.servers": config.KAFKA_BOOTSTRAP,
                           "group.id": f"prueba-{uuid.uuid4().hex[:8]}",
                           "auto.offset.reset": "earliest", "enable.auto.commit": False})
    recibidos = []
    try:
        consumidor.subscribe([topic_unico])
        limite = time.time() + 30
        while len(recibidos) < 5 and time.time() < limite:
            msg = consumidor.poll(1.0)
            if msg is None or msg.error():
                continue
            recibidos.append((msg.key().decode(), json.loads(msg.value())))
    finally:
        consumidor.close()

    assert [e for _, e in recibidos] == eventos  # una partición: mismo orden
    assert [k for k, _ in recibidos] == [str(e["userId"]) for e in eventos]
