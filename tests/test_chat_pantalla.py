"""Reqs. 82 y 84 en la app real (AppTest): chips, «Nueva conversación», aviso y barra lateral.

Delegado no puede capturar un servidor Streamlit, así que esta prueba deja la evidencia
de forma automática: con proveedor 'api' y SIN claves (la cadena cae a reglas, sin red)
se envía un pedido en la pestaña Chat y se comprueba lo que ve el usuario.
"""

from __future__ import annotations

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import crear_artefactos_moviepredict
from recomendador import config, cuentas, eventos

APP = config.RAIZ / "app" / "app.py"


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("chatui"))


@pytest.fixture
def at(monkeypatch, artefactos, ruta_mini, tmp_path):
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "MODO_PUBLICO", False)
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "api")
    for var in ("GROQ_API_KEY", "GEMINI_API_KEY", "TMDB_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    monkeypatch.setattr(cuentas, "ruta_defecto", lambda: tmp_path / "cuentas.db")
    app = AppTest.from_file(str(APP), default_timeout=120)
    app.run()
    app.text_input(key="reg_nombre").input("chat_ui")
    app.text_input(key="reg_clave").input("secreta123")
    app.text_input(key="reg_clave2").input("secreta123")
    next(b for b in app.button if b.label == "Crear cuenta").click().run()
    next(b for b in app.button if b.label == "Probar con un CSV de ejemplo").click().run()
    assert not app.exception, app.exception
    return app


def _textos(app) -> str:
    return "\n".join([c.value for c in app.caption] + [m.value for m in app.markdown])


def test_chips_aviso_y_barra_lateral(at):
    # Aviso de servicio externo visible en la pestaña Chat (proveedor 'api').
    # (No se importa app.vistas: «app» resolvería a app/app.py y ejecutaría la página.)
    assert any("servicio externo" in c.value for c in at.caption)
    assert any(b.label == "Nueva conversación" for b in at.button)

    at.chat_input(key="chat_entrada").set_value("drama policial con un twist final").run()
    assert not at.exception, at.exception

    chips = [b.label for b in at.button if b.key and b.key.startswith("chip_")]
    assert len(chips) >= 2 and all(c.endswith("×") for c in chips), chips
    assert any("Entendí:" in c.value for c in at.caption)

    lateral = "\n".join(m.value for m in at.sidebar.markdown) + "\n".join(
        c.value for c in at.sidebar.caption)
    assert "Último proveedor:" in lateral and "reglas" in lateral.lower()
    assert "Cupo:" in lateral and "por minuto" in lateral

    # La × de un chip quita esa condición y recalcula (hay un chip menos).
    at.button(key=next(b.key for b in at.button if b.key and b.key.startswith("chip_"))).click().run()
    quedan = [b.label for b in at.button if b.key and b.key.startswith("chip_")]
    assert len(quedan) == len(chips) - 1

    # «Nueva conversación» borra chips e historial.
    at.button(key="chat_nueva").click().run()
    assert any("sin condiciones" in c.value for c in at.caption if "Entend" in c.value)
    # AppTest conserva elementos de la pasada interrumpida por st.rerun() que la nueva
    # no sobrescribe (el navegador real los borra al terminar la recarga): se refresca.
    at.run()
    assert not [b for b in at.button if b.key and b.key.startswith("chip_")]
    assert not at.chat_message
