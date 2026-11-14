"""Pruebas rápidas (sin Spark) del silenciado de taskkill en spark_utils."""
import os
import subprocess
import sys

import pytest

from recomendador import spark_utils


def test_detecta_taskkill():
    assert spark_utils._es_taskkill(["cmd", "/c", "taskkill", "/f", "/t", "/pid", "1"])
    assert spark_utils._es_taskkill(["taskkill", "/pid", "1"])
    assert not spark_utils._es_taskkill(["spark-submit.cmd", "pyspark-shell"])
    assert not spark_utils._es_taskkill([sys.executable, "-c", "print(1)"])


@pytest.mark.skipif(os.name != "nt", reason="taskkill solo existe en Windows")
def test_taskkill_de_proceso_inexistente_no_imprime(capfd):
    """Lo que hace pyspark al salir: taskkill sobre un PID que ya no existe."""
    clase = spark_utils._clase_popen_silenciosa()
    proc = clase(["cmd", "/c", "taskkill", "/f", "/t", "/pid", "999999"])
    proc.wait(30)
    salida = capfd.readouterr()
    assert "999999" not in salida.out + salida.err


def test_otros_procesos_conservan_su_salida():
    clase = spark_utils._clase_popen_silenciosa()
    proc = clase([sys.executable, "-c", "print('hola')"], stdout=subprocess.PIPE, text=True)
    out, _ = proc.communicate(timeout=30)
    assert out.strip() == "hola"
