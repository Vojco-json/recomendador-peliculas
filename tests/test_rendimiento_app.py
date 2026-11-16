"""Paquete 11 (req. 45): Inicio, Buscar, Ficha y Explorar cargan en menos de 2 s.

Con la muestra mini precalculada (app/*.parquet) y la caché caliente (una primera
corrida de calentamiento), cada recarga de la app debe tardar menos de 2 s. Como
st.tabs dibuja todas las pestañas en cada corrida, una recarga incluye Inicio,
Buscar, Explorar y Chat a la vez; se mide además con una búsqueda y con una ficha
abiertas. Por último se comprueba, en un proceso aparte, que la app no importa pyspark.
"""

from __future__ import annotations

import subprocess
import sys
import time

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import crear_artefactos_moviepredict
from recomendador import config, cuentas, eventos

APP = config.RAIZ / "app" / "app.py"
LIMITE_S = 2.0


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("rendimiento"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    monkeypatch.setattr(cuentas, "ruta_defecto", lambda: tmp_path / "cuentas.db")


def _boton(at, etiqueta):
    return next(b for b in at.button if b.label == etiqueta)


def _medir(at, accion=None) -> float:
    t0 = time.perf_counter()
    (accion() if accion else at).run()
    dt = time.perf_counter() - t0
    assert not at.exception, at.exception
    return dt


def test_paginas_en_menos_de_2_s(entorno):
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input("rapida")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    _boton(at, "Crear cuenta").click().run()
    _boton(at, "Probar con un CSV de ejemplo").click().run()
    assert not at.exception, at.exception
    at.run()                                         # calentamiento de cachés

    tiempos = {"Inicio + Buscar + Explorar (recarga)": _medir(at)}
    at.text_input(key="buscar_q").input("nolan")
    tiempos["Buscar 'nolan'"] = _medir(at)
    ver = [b for b in at.button if b.key and b.key.startswith("ver_")]
    assert ver, "la búsqueda no devolvió resultados"
    tiempos["Ficha (abrir)"] = _medir(at, ver[0].click)
    tiempos["Ficha (recarga)"] = _medir(at)
    print("\nTiempos por página (s):", {k: round(v, 3) for k, v in tiempos.items()})
    lentas = {k: round(v, 2) for k, v in tiempos.items() if v >= LIMITE_S}
    assert not lentas, f"páginas de {LIMITE_S} s o más: {lentas}"


def test_la_app_no_importa_pyspark():
    """Importa todo lo que usa la app en un proceso limpio y revisa sys.modules."""
    codigo = (
        "import sys; sys.path[:0] = [r'%s', r'%s'];"
        "import recomendador.catalogo_app, recomendador.herramientas, recomendador.llm,"
        " recomendador.cuentas, recomendador.tmdb, recomendador.pedido, estilo;"
        "from vistas import buscar, chat, explorar, ficha, inicio, login, onboarding, tarjetas;"
        "print('PYSPARK' if any(m.split('.')[0] == 'pyspark' for m in sys.modules) else 'OK')"
    ) % (config.RAIZ, config.RAIZ / "app")
    salida = subprocess.run([sys.executable, "-c", codigo], capture_output=True, text=True,
                            timeout=90, cwd=str(config.RAIZ))
    assert salida.returncode == 0, salida.stderr
    assert salida.stdout.strip().endswith("OK"), salida.stdout
