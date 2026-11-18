"""Pedido 13 (req. 98-107): logo, colores del logo, ficha global y carátulas clicables.

Las funciones (abrir_ficha, cerrar_ficha, procesar_clic) se prueban sin Streamlit;
la navegación completa (abrir desde Inicio, Buscar y Chat, «← Volver») con AppTest y
la muestra mini. AppTest no puede pulsar dentro del iframe del componente, así que
el clic se simula escribiendo lo mismo que abrir_ficha.
"""

from __future__ import annotations

import re
import sys
import tomllib

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import clic_caratula, crear_artefactos_moviepredict, html_pagina, ir_a
from recomendador import config, cuentas, eventos

RAIZ = config.RAIZ
APP = RAIZ / "app" / "app.py"
sys.path.insert(0, str(RAIZ / "app"))

import estilo  # noqa: E402
from vistas import comun  # noqa: E402


# --------------------------------------------------------------------------- #
# Logo y colores
# --------------------------------------------------------------------------- #

def test_cabecera_con_logo_png_y_alt():
    html = estilo.cabecera_html()
    assert estilo.RUTA_LOGO.name == "logo_moviepredict.png" and estilo.RUTA_LOGO.exists()
    img = re.search(r'<img src="data:image/png;base64,[A-Za-z0-9+/=]+" alt="([^"]+)">', html)
    assert img and img.group(1) == "MoviePredict"


def test_colores_del_logo():
    assert estilo.PALETA["cian"] == "#07ECF8" and estilo.PALETA["azul"] == "#0D73C5"
    assert estilo.PALETA["ambar"] == "#f0b23a"                       # estrellas en ámbar
    css = estilo.CSS
    assert "background:var(--cr-cian)" in css                        # botón primario
    assert ".cr-dato b {{ color:var(--cr-ambar)".replace("{{", "{") in css
    tema = tomllib.loads((RAIZ / ".streamlit" / "config.toml").read_text(encoding="utf-8"))
    assert tema["theme"]["primaryColor"] == "#07ECF8"
    assert "#0D73C5" in (RAIZ / ".streamlit" / "config.toml").read_text(encoding="utf-8")
    assert "#E50914" not in (RAIZ / ".streamlit" / "config.toml").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Ficha global (sin Streamlit)
# --------------------------------------------------------------------------- #

def test_abrir_y_cerrar_ficha_solo_toca_su_clave():
    sesion = {"usuario": {"userId": 162600}, "seccion": "Buscar", "chat_162600": ["x"]}
    comun.abrir_ficha(318, "Buscar", sesion)
    assert sesion["ficha_abierta"] == {"movieId": 318, "origen": "Buscar"}
    assert comun.ficha_abierta(sesion)["movieId"] == 318
    assert sesion["usuario"] == {"userId": 162600} and sesion["seccion"] == "Buscar"
    comun.cerrar_ficha(sesion)
    assert comun.ficha_abierta(sesion) is None and "ficha_abierta" not in sesion
    assert sesion["chat_162600"] == ["x"]


def test_procesar_clic_no_reabre_el_mismo_clic():
    sesion = {}
    assert comun.procesar_clic(None, "inicio", "Inicio", sesion) is None
    assert comun.procesar_clic({"movieId": 50, "n": 1}, "inicio", "Inicio", sesion) == 50
    assert sesion["ficha_abierta"] == {"movieId": 50, "origen": "Inicio"}
    comun.cerrar_ficha(sesion)                       # «← Volver»
    # Streamlit repite el último valor del componente: no debe reabrir la ficha.
    assert comun.procesar_clic({"movieId": 50, "n": 1}, "inicio", "Inicio", sesion) is None
    assert "ficha_abierta" not in sesion
    # Un clic nuevo (otra marca) sí la abre, aunque sea la misma película.
    assert comun.procesar_clic({"movieId": 50, "n": 2}, "inicio", "Inicio", sesion) == 50


def test_sin_enlaces_ni_query_params():
    """Ni el componente ni las carátulas usan href, enlaces ni query params."""
    componente = (comun.CARPETA_COMPONENTE / "index.html").read_text(encoding="utf-8")
    codigo = re.sub(r"<!--.*?-->", "", componente, flags=re.S)
    for prohibido in ("href", "<a ", "location", "query_params", "?movie"):
        assert prohibido not in codigo, prohibido
    for archivo in ("comun.py", "buscar.py", "inicio.py", "chat.py"):
        texto = (RAIZ / "app" / "vistas" / archivo).read_text(encoding="utf-8")
        assert "href" not in texto and "query_params" not in texto, archivo
    assert "buscar_ficha" not in (RAIZ / "app" / "vistas" / "buscar.py").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Navegación con AppTest (muestra mini)
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("ficha_global"))


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


def _sesion() -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input("ficha_global")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
    assert not at.exception, at.exception
    return at


def _ids(at, clase: str) -> list[int]:
    return [int(x) for x in re.findall(rf'class="{clase}" data-movie="(\d+)"', html_pagina(at))]


def _volver(at) -> None:
    next(b for b in at.button if b.label == "← Volver").click().run()
    assert not at.exception, at.exception
    assert "ficha_abierta" not in at.session_state


def test_abrir_ficha_desde_inicio_buscar_y_chat(entorno):
    at = _sesion()
    usuario = at.session_state["usuario"]["userId"]
    assert usuario > 162541
    assert 'alt="MoviePredict"' in html_pagina(at)

    # Inicio: las carátulas van en el componente clicable.
    ids = _ids(at, "cr-caratula")
    assert ids and at.get("component_instance")
    clic_caratula(at, ids[0], "Inicio")
    assert not at.exception, at.exception
    assert any(b.label == "← Volver" for b in at.button)
    assert "Histograma de notas" in html_pagina(at)               # se ve la ficha
    assert at.session_state["usuario"]["userId"] == usuario
    assert at.radio(key="seccion").value == "Inicio"
    _volver(at)
    assert 'data-fila="para_ti"' in html_pagina(at)               # de vuelta en Inicio

    # Buscar: la consulta sobrevive a la ficha abierta.
    ir_a(at, "Buscar").text_input(key="buscar_q").input("nolan").run()
    ids = _ids(at, "cr-caratula")
    assert ids
    clic_caratula(at, ids[0], "Buscar")
    assert at.radio(key="seccion").value == "Buscar"
    assert "Histograma de notas" in html_pagina(at)
    _volver(at)
    assert "resultados para «nolan»" in html_pagina(at)

    # Chat: las tarjetas recomendadas también son clicables.
    ir_a(at, "Chat").chat_input[0].set_value("drama policial con un twist final").run()
    assert not at.exception, at.exception
    ids = _ids(at, "tarjeta")
    assert ids, "el chat no mostró tarjetas clicables"
    historial = list(at.session_state[f"chat_{usuario}"])
    clic_caratula(at, ids[0], "Chat")
    assert at.radio(key="seccion").value == "Chat"
    assert at.session_state["usuario"]["userId"] == usuario
    _volver(at)
    assert len(at.session_state[f"chat_{usuario}"]) == len(historial)   # chat intacto

    # Cambiar de sección con una ficha abierta la cierra.
    clic_caratula(at, ids[0], "Chat")
    ir_a(at, "Inicio")
    assert "ficha_abierta" not in at.session_state
    assert not at.exception, at.exception
