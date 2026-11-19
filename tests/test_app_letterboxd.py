"""App MoviePredict (AppTest): login → onboarding → Inicio, Letterboxd, límite y paleta.

Artefactos de la muestra mini (ALS + perfiles + app/*.parquet) en una carpeta temporal;
Kafka se simula caído (respaldo directo a estado.db) y el chat usa el proveedor 'reglas'.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import html_pagina, ir_a

from base_mini import crear_artefactos_moviepredict
from recomendador import config, cuentas, estado, eventos, perfiles

APP = config.RAIZ / "app" / "app.py"
ESTILO = config.RAIZ / "app" / "estilo.py"
CSV_MINI = config.RAIZ / "muestras" / "letterboxd_mini.csv"
# Las 10 filas de muestras/letterboxd_mini.csv, ya emparejadas con ml-mini.
LETTERBOXD_MINI = {6: 5.0, 1036: 5.0, 2571: 4.5, 589: 5.0, 47: 4.5,
                   1721: 1.5, 539: 1.0, 597: 1.5, 1307: 2.0, 356: 2.0}
PROHIBIDOS = ["#14181c", "#2c3440", "#00e054", "#ff8000", "#40bcf4"]
FILAS = {"para_ti": "Para ti",
         "populares_tipo": "Populares entre espectadores como tú",
         "porque_te_gusto": "Porque te gustó "}


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("moviepredict"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    """Configuración apuntando a la mini, estado.db propio y Kafka 'caído'."""
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    return tmp_path / "estado.db"


def _html(at) -> str:
    """Markdown + HTML de los componentes de carátulas clicables."""
    return html_pagina(at)


def _boton(at, etiqueta: str):
    return next(b for b in at.button if b.label == etiqueta)


def _registrar(at, nombre: str) -> int:
    at.run()
    at.text_input(key="reg_nombre").input(nombre)
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    _boton(at, "Crear cuenta").click().run()
    assert not at.exception, at.exception
    return int(at.session_state["usuario"]["userId"])


def _eventos(db, userId) -> dict[int, float]:
    con = estado.conectar(db)
    try:
        return dict(estado.eventos_usuario(con, userId))
    finally:
        con.close()


def _nota_onboarding(movieId: int, i: int) -> float:
    """Nota conocida si está en la muestra de Letterboxd; si no, alterna alta/baja."""
    return LETTERBOXD_MINI.get(movieId, 4.5 if i % 2 == 0 else 2.0)


def _calificar_onboarding(at) -> dict[int, float]:
    claves = [s.key for s in at.selectbox if s.key and s.key.startswith("onb_")]
    assert len(claves) == 10, claves                     # ~10 populares para calificar
    notas = {}
    for i, clave in enumerate(claves):
        m = int(clave.split("_")[1])
        notas[m] = _nota_onboarding(m, i)
        at.selectbox(key=clave).set_value(notas[m])
    _boton(at, "Guardar y ver mis recomendaciones").click().run()
    assert not at.exception, at.exception
    return notas


def _comprobar_inicio(at, artefactos, ruta_mini, notas: dict) -> None:
    html = _html(at)
    assert {"Inicio", "Buscar", "Explorar", "Chat"} <= set(at.radio(key="seccion").options)
    assert "Primer paso" not in html
    for clave, titulo in FILAS.items():
        bloque = re.search(rf'<section class="cr-fila" data-fila="{clave}"><h3>(.*?)</h3>'
                           r'(.*?)</section>', html, re.S)
        assert bloque, f"falta la fila {clave}"
        assert bloque.group(1).startswith(titulo.replace("ú", "ú"))
        assert 'class="cr-caratula"' in bloque.group(2), f"fila {clave} sin carátulas"
    semilla = re.search(r'data-fila="porque_te_gusto"><h3>Porque te gustó (.+?)</h3>', html)
    assert semilla and semilla.group(1).strip()
    # Tipo de espectador: el mismo que calcula perfiles.tipo_usuario con esas notas.
    tipo = perfiles.tipo_usuario(0, artefactos, ratings=notas, datos=ruta_mini)
    assert tipo and f'<span class="cr-tipo">{tipo["tipo"]}</span>' in html
    assert "MoviePredict" in html


# --------------------------------------------------------------------------- #
# C1-C3: login, onboarding e Inicio
# --------------------------------------------------------------------------- #

def test_sin_sesion_solo_login(entorno):
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.run()
    assert not at.exception, at.exception
    claves = {t.key for t in at.text_input}
    assert {"login_nombre", "login_clave", "reg_nombre", "reg_clave"} <= claves
    assert [t.label for t in at.tabs] == ["Entrar", "Crear cuenta"]
    assert not ir_a(at, "Chat").chat_input and not at.selectbox            # ni chat ni onboarding
    assert not at.sidebar.markdown and not at.sidebar.button   # sin barra lateral
    html = _html(at)
    assert "MoviePredict" in html and '<section class="cr-fila"' not in html
    assert "usuario" not in at.session_state


def test_registro_onboarding_e_inicio(entorno, artefactos, ruta_mini):
    at = AppTest.from_file(str(APP), default_timeout=90)
    uid = _registrar(at, "ana_onb")
    assert uid > cuentas.ULTIMO_USERID_MOVIELENS
    assert "Primer paso" in _html(at) and not ir_a(at, "Chat").chat_input     # onboarding
    notas = _calificar_onboarding(at)
    assert _eventos(entorno, uid) == notas                      # las 10 quedaron
    _comprobar_inicio(at, artefactos, ruta_mini, notas)
    # Login de nuevo (otra sesión): ya tiene notas, así que salta el onboarding.
    at2 = AppTest.from_file(str(APP), default_timeout=90)
    at2.run()
    at2.text_input(key="login_nombre").input("ana_onb")
    at2.text_input(key="login_clave").input("secreta123")
    _boton(at2, "Entrar").click().run()
    assert not at2.exception and at2.session_state["usuario"]["userId"] == uid
    assert "Primer paso" not in _html(at2) and '<section class="cr-fila"' in _html(at2)


def test_login_incorrecto(entorno):
    at = AppTest.from_file(str(APP), default_timeout=90)
    at.run()
    at.text_input(key="login_nombre").input("nadie")
    at.text_input(key="login_clave").input("malamala")
    _boton(at, "Entrar").click().run()
    assert "Nombre o contraseña incorrectos." in [e.value for e in at.error]
    assert "usuario" not in at.session_state


def test_importar_letterboxd_salta_onboarding(entorno, artefactos, ruta_mini):
    at = AppTest.from_file(str(APP), default_timeout=90)
    uid = _registrar(at, "beto_lbx")
    _boton(at, "Probar con un CSV de ejemplo").click().run()
    assert not at.exception, at.exception
    assert _eventos(entorno, uid) == LETTERBOXD_MINI
    assert "Primer paso" not in _html(at)
    assert "Importamos 10 calificaciones de Letterboxd." in [s.value for s in at.success]
    _comprobar_inicio(at, artefactos, ruta_mini, LETTERBOXD_MINI)


def test_importar_csv_invalido(entorno, tmp_path):
    sys.path.insert(0, str(config.RAIZ / "app"))
    from vistas.comun import importar_letterboxd

    malo = tmp_path / "otro.csv"
    malo.write_text("a,b\n1,2\n", encoding="utf-8")
    r = importar_letterboxd(malo, 170000, config.DATOS, entorno, cuentas.Limitador())
    assert not r["ok"] and "Letterboxd" in r["error"] and not _eventos(entorno, 170000)
    r = importar_letterboxd(CSV_MINI, 170000, config.DATOS, entorno, cuentas.Limitador())
    assert r["ok"] and sorted(r["importadas"]) == sorted(LETTERBOXD_MINI)


# --------------------------------------------------------------------------- #
# C4: límite de 20 acciones por minuto
# --------------------------------------------------------------------------- #

def test_accion_21_rechazada_en_la_app(entorno):
    """10 notas del onboarding + 10 mensajes de chat = 20; el mensaje 21 se rechaza."""
    at = AppTest.from_file(str(APP), default_timeout=90)
    uid = _registrar(at, "caro_lim")
    _calificar_onboarding(at)
    for i in range(10):
        ir_a(at, "Chat").chat_input[0].set_value(f"algo corto y liviano {i}").run()
        assert not at.exception, at.exception
    historial = at.session_state[f"chat_{uid}"]
    assert len(historial) == 20
    assert not [w for w in at.warning if "límite" in str(w.value)]
    ir_a(at, "Chat").chat_input[0].set_value("drama policial con un twist final").run()
    assert not at.exception, at.exception
    avisos = [w.value for w in at.warning if "Llegaste al límite de 20" in str(w.value)]
    assert avisos and "no se registró" in avisos[0]
    assert len(at.session_state[f"chat_{uid}"]) == 20          # no se registró


def test_calificacion_21_no_se_registra(entorno):
    sys.path.insert(0, str(config.RAIZ / "app"))
    from vistas.comun import calificar_varias

    ids = [1, 6, 32, 47, 50, 110, 260, 296, 318, 356, 364, 480, 527, 539, 589, 593, 597,
           1036, 1265, 1307, 1721]
    assert len(ids) == 21
    r = calificar_varias(170001, [(m, 4.0) for m in ids], entorno, cuentas.Limitador())
    assert r["limite"] and len(r["registradas"]) == 20
    assert "Llegaste al límite de 20" in r["avisos"][-1]
    guardadas = _eventos(entorno, 170001)
    assert len(guardadas) == 20 and 1721 not in guardadas


# --------------------------------------------------------------------------- #
# C5 y C7: paleta propia y sin pyspark
# --------------------------------------------------------------------------- #

def test_paleta_moviepredict_sin_colores_de_letterboxd():
    fuente = ESTILO.read_text(encoding="utf-8").lower()
    for color in PROHIBIDOS:
        assert color not in fuente, color
    sys.path.insert(0, str(config.RAIZ / "app"))
    import estilo

    assert estilo.NOMBRE_APP == "MoviePredict"
    assert estilo.PALETA == {"fondo": "#0e1216", "superficie": "#1f2730", "borde": "#34404c",
                             "texto": "#aab4be", "titulo": "#f3f5f7", "ambar": "#f0b23a",
                             "turquesa": "#3cb4a8", "coral": "#e5675c",
                             # pedido 13: acento cian y secundario azul del logo
                             "cian": "#07ECF8", "azul": "#0D73C5"}
    for color in estilo.PALETA.values():
        assert color in estilo.CSS
    assert "MoviePredict" in estilo.pie_html()


SCRIPT_SIN_PYSPARK = r"""
import sys
from streamlit.testing.v1 import AppTest
from recomendador import eventos
eventos._productor_defecto = lambda *a, **k: None       # Kafka caído: respaldo directo


def ir_a(at, seccion):
    # Elige una sección de la navegación (solo se ejecuta la sección activa).
    radio = at.radio(key="seccion")
    if radio.value != seccion:
        radio.set_value(seccion).run()
    return at


at = AppTest.from_file("app/app.py", default_timeout=120)
at.run()
at.text_input(key="reg_nombre").input("dani_spark")
at.text_input(key="reg_clave").input("secreta123")
at.text_input(key="reg_clave2").input("secreta123")
next(b for b in at.button if b.label == "Crear cuenta").click().run()
next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
ir_a(at, "Buscar").text_input(key="buscar_q").input("nolan").run()
ir_a(at, "Chat").chat_input[0].set_value("drama policial con un twist final").run()
assert not at.exception, at.exception
ir_a(at, "Inicio")
# Las filas van dentro del componente de carátulas clicables (args en json_args).
pagina = "".join(str(m.value) for m in at.markdown) + "".join(
    c.proto.json_args for c in at.get("component_instance"))
assert "cr-fila" in pagina
cargados = sorted(m for m in sys.modules if m.startswith("pyspark"))
print("pyspark:", cargados)
sys.exit(1 if cargados else 0)
"""


def test_navegar_no_importa_pyspark(artefactos, ruta_mini, tmp_path):
    """C7: login, importar, Inicio, Buscar y Chat en un proceso limpio sin pyspark."""
    env = dict(os.environ, MODO="completo", DATOS=str(ruta_mini),
               ARTEFACTOS_BASE=str(artefactos), ESTADO_DB=str(tmp_path / "estado.db"),
               LLM_PROVIDER="reglas", TMDB_API_KEY="")
    r = subprocess.run([sys.executable, "-c", SCRIPT_SIN_PYSPARK], cwd=config.RAIZ,
                       env=env, capture_output=True, text=True, timeout=300)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    assert "pyspark: []" in r.stdout
