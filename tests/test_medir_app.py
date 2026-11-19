"""Requisito 96: scripts/medir_app.py corre de punta a punta (con la muestra mini).

Con 25M lo corre el usuario; aquí se comprueba que recorre todos los pasos, que el
informe trae la tabla de tiempos y el detalle de cProfile, y que no deja la
configuración global alterada.
"""

from __future__ import annotations

import pytest

from base_mini import crear_artefactos_moviepredict
from recomendador import config, cuentas, eventos
from scripts import medir_app


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("medir"))


def test_medir_app_con_muestra_mini(monkeypatch, artefactos, ruta_mini, tmp_path):
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    # medir_app cambia estos valores: monkeypatch los restaura al terminar.
    monkeypatch.setattr(config, "ESTADO_DB", config.ESTADO_DB)
    monkeypatch.setattr(config, "LLM_PROVIDER", config.LLM_PROVIDER)
    monkeypatch.setattr(cuentas, "ruta_defecto", cuentas.ruta_defecto)
    monkeypatch.setattr(eventos, "_productor_defecto", eventos._productor_defecto)
    for var in ("MODO", "ESTADO_DB", "LLM_PROVIDER"):
        monkeypatch.setenv(var, "completo" if var == "MODO" else "x")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)

    salida = tmp_path / "medicion_app.txt"
    assert medir_app.main(["--etiqueta", "prueba", "--salida", str(salida),
                           "--top", "5"]) == 0
    texto = salida.read_text(encoding="utf-8")
    assert "Medición «prueba»" in texto
    for paso in ("1. Primera carga en frío", "3. Primera carga tras calentamiento",
                 "5. Cambiar a Buscar", "7. Abrir una ficha", "8. Calificar",
                 "12. Mensaje de chat (modo reglas)", "13. Volver a Inicio",
                 "12b. Chat «una policial de los 90»"):
        assert paso in texto, paso
    assert "Interacciones con caché caliente" in texto
    assert "Detalle por función (cProfile" in texto and "cumulative" in texto
