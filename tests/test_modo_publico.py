"""Paquete 11 (req. 44, 50, 51): modo público, LLM de a uno, límite 20/min, QR y moderación.

- C2: con MODO_PUBLICO=1 y sin sesión solo se ve el login (sin pestañas de la app ni
  controles de administración); con sesión tampoco hay configuración ni recarga.
- C3: con el semáforo del LLM tomado, otra solicitud recibe la plantilla al instante.
- C4: figuras_poster.py genera qr_publico.png con URL_PUBLICA y avisa sin ella.
- C6: el README documenta scripts/moderar_resenas.py como único camino de moderación.
"""

from __future__ import annotations

import sys
import time

import pytest
from streamlit.testing.v1 import AppTest

from recomendador import config, cuentas, eventos, llm
from recomendador.herramientas import Herramientas

APP = config.RAIZ / "app" / "app.py"
SCRIPTS = config.RAIZ / "scripts"
# Textos de controles de administración que nunca deben verse en modo público.
ADMIN = ["LLM_PROVIDER", "LLM_MODEL", "OLLAMA_HOST", "Actualizar", "Ocultar", "Moderar",
         "moderar_resenas", "artefactos", "estado.db", "Selecciona un usuario"]
PESTANAS_APP = {"Inicio", "Buscar", "Explorar", "Chat"}


@pytest.fixture
def publico(monkeypatch, ruta_mini, tmp_path):
    monkeypatch.setattr(config, "MODO_PUBLICO", True)
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", config.RAIZ / "artefactos" / "mini")
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    monkeypatch.setattr(cuentas, "ruta_defecto", lambda: tmp_path / "cuentas.db")


def _visible(at) -> str:
    partes = [str(m.value) for m in at.markdown]
    for grupo in (at.caption, at.info, at.warning, at.error, at.success):
        partes += [str(x.value) for x in grupo]
    partes += [str(b.label) for b in at.button]
    return "\n".join(partes)


def _sin_admin(at) -> None:
    texto = _visible(at)
    for palabra in ADMIN:
        assert palabra not in texto, f"control de administración visible: {palabra}"
    # Los selectores de nota (onboarding, ficha) sí están; uno de usuario, nunca.
    for caja in list(at.selectbox) + list(at.number_input) + list(at.radio):
        assert "usuario" not in str(caja.label).lower(), f"selector de usuario: {caja.label}"


# --------------------------------------------------------------------------- #
# C2: login obligatorio y sin administración
# --------------------------------------------------------------------------- #

def test_sin_sesion_solo_login(publico):
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    assert not at.exception, at.exception
    etiquetas = {t.label for t in at.tabs}
    assert not etiquetas & PESTANAS_APP, f"pestañas visibles sin sesión: {etiquetas}"
    assert {"Entrar", "Crear cuenta"} <= etiquetas
    assert "Bienvenido a MoviePredict" in _visible(at)
    _sin_admin(at)


def test_con_sesion_sin_controles_de_administracion(publico):
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input("visita_expo")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    assert not at.exception, at.exception
    assert at.session_state["usuario"]["nombre"] == "visita_expo"
    _sin_admin(at)
    assert "acciones en este minuto" in _visible(at)     # el límite sí se informa


def test_sin_modo_publico_si_hay_configuracion(publico, monkeypatch):
    """Control: fuera del modo público la barra lateral muestra la configuración."""
    monkeypatch.setattr(config, "MODO_PUBLICO", False)
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input("local_1")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    assert "LLM_PROVIDER" in _visible(at)


# --------------------------------------------------------------------------- #
# Límite de 20 acciones por minuto
# --------------------------------------------------------------------------- #

def test_limite_20_por_minuto():
    lim = cuentas.Limitador()
    assert lim.maximo == 20
    assert all(lim.permitir("chat") for _ in range(20))
    assert not lim.permitir("chat")          # la acción 21 se rechaza
    assert lim.restantes() == 0


# --------------------------------------------------------------------------- #
# C3: LLM de a una solicitud
# --------------------------------------------------------------------------- #

class ClienteLento:
    """Simula un Ollama que tarda: si se llamara, la prueba superaría el tope."""
    llamadas = 0

    def chat(self, **kw):
        ClienteLento.llamadas += 1
        time.sleep(5)
        return {"message": {"content": "{}"}}


class ClienteRapido:
    def chat(self, **kw):
        return {"message": {"content": '{"intro": "Mira estas", "recomendaciones": []}'}}


def test_semaforo_tomado_responde_plantilla_sin_esperar(publico, ruta_mini, tmp_path):
    h = Herramientas(ruta_mini, tmp_path, tmp_path / "estado.db")
    ClienteLento.llamadas = 0
    assert llm.SEMAFORO_LLM.acquire(blocking=False)      # otra sesión ocupa el LLM
    try:
        t0 = time.perf_counter()
        r = llm.responder("algo corto y liviano", 999_999, h, proveedor="ollama",
                          cliente=ClienteLento())
        dt = time.perf_counter() - t0
    finally:
        llm.SEMAFORO_LLM.release()
    assert dt < 2, f"esperó {dt:.1f} s"
    assert ClienteLento.llamadas == 0
    assert r.proveedor == "reglas" and r.aviso == llm.AVISO_OCUPADO
    assert r.recomendaciones and "Entendí:" in r.texto
    # Libre otra vez: la siguiente solicitud sí usa el LLM y libera el semáforo.
    r2 = llm.responder("algo corto y liviano", 999_999, h, proveedor="ollama",
                       cliente=ClienteRapido())
    assert r2.proveedor.startswith("ollama")
    assert llm.SEMAFORO_LLM.acquire(blocking=False)
    llm.SEMAFORO_LLM.release()


# --------------------------------------------------------------------------- #
# C4: QR hacia URL_PUBLICA
# --------------------------------------------------------------------------- #

def _figuras():
    if str(SCRIPTS.parent) not in sys.path:
        sys.path.insert(0, str(SCRIPTS.parent))
    from scripts import figuras_poster
    return figuras_poster


def test_qr_con_url_publica(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "URL_PUBLICA", "https://moviepredict.example.com")
    fp = _figuras()
    assert fp.main(["--artefactos", str(tmp_path / "nada"), "--figuras", str(tmp_path)]) == 0
    png = tmp_path / "qr_publico.png"
    assert png.exists() and png.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert "https://moviepredict.example.com" in capsys.readouterr().out


def test_qr_sin_url_avisa(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(config, "URL_PUBLICA", "")
    fp = _figuras()
    assert fp.main(["--artefactos", str(tmp_path / "nada"), "--figuras", str(tmp_path)]) == 0
    assert not (tmp_path / "qr_publico.png").exists()
    assert "URL_PUBLICA no está definida" in capsys.readouterr().out


# --------------------------------------------------------------------------- #
# C6: moderación solo por script (documentada en el README)
# --------------------------------------------------------------------------- #

def test_readme_documenta_moderacion_y_modo_publico():
    texto = (config.RAIZ / "README.md").read_text(encoding="utf-8")
    assert "scripts/moderar_resenas.py" in texto
    for opcion in ("--listar", "--ocultar", "--mostrar"):
        assert opcion in texto
    assert "único camino de moderación" in texto
    for palabra in ("MODO_PUBLICO", "URL_PUBLICA", "cloudflared", "precalcular_app.py",
                    "MoviePredict"):
        assert palabra in texto, palabra


def test_app_no_tiene_controles_de_moderacion():
    """Ningún archivo de la app llama a la moderación (solo el script local)."""
    for ruta in (config.RAIZ / "app").rglob("*.py"):
        codigo = ruta.read_text(encoding="utf-8")
        assert "marcar_oculta" not in codigo, ruta
