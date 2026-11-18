"""Req. 86: 20 sesiones de AppTest a la vez sobre la muestra mini, cada página < 2 s.

Se preparan 20 sesiones (cuenta + CSV de ejemplo + calentamiento) y luego las 20
recargan la app al mismo tiempo en hilos. Cada recarga dibuja Inicio, Buscar,
Explorar y Chat (st.tabs pinta todas las pestañas). Ninguna puede fallar ni tardar
2 s o más.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import crear_artefactos_moviepredict
from recomendador import config, cuentas, eventos

APP = config.RAIZ / "app" / "app.py"
SESIONES = 20
LIMITE_S = 2.0


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("carga"))


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


def _sesion(i: int) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input(f"carga_{i:02d}")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
    at.run()                                    # calentamiento
    assert not at.exception, at.exception
    return at


def _recargar(at: AppTest) -> float:
    t0 = time.perf_counter()
    at.run()
    dt = time.perf_counter() - t0
    assert not at.exception, at.exception
    return dt


def test_20_sesiones_a_la_vez(entorno):
    sesiones = [_sesion(i) for i in range(SESIONES)]
    with ThreadPoolExecutor(max_workers=SESIONES) as ex:
        tiempos = list(ex.map(_recargar, sesiones))
    print(f"\n{SESIONES} sesiones a la vez: máx {max(tiempos):.2f} s, "
          f"media {sum(tiempos) / len(tiempos):.2f} s")
    lentas = [round(t, 2) for t in tiempos if t >= LIMITE_S]
    assert not lentas, f"recargas de {LIMITE_S} s o más: {lentas}"
