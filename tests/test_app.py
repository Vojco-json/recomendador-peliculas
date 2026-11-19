"""App Streamlit (AppTest) y chat LLM: proveedor 'reglas' y cliente Ollama simulado.

Los artefactos mini se construyen en un directorio temporal (tests/base_mini.py);
no se usa nada de 25M ni se necesita Ollama corriendo.
"""

from __future__ import annotations

import importlib
import json

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import html_pagina, ir_a

from base_mini import crear_artefactos_moviepredict
from recomendador import config, llm
from recomendador.herramientas import Herramientas

VISTAS_U1 = {1, 6, 32, 47, 50, 110, 260, 296, 318, 356, 364, 480, 589, 593, 1036, 1721,
             2571, 4306, 6377, 58559}
APP = config.RAIZ / "app" / "app.py"
PEDIDO = "algo corto y liviano para hoy"


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("art_app"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    """Apunta la configuración a la muestra mini y a un estado.db temporal."""
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.setattr(config, "LLM_THINK", False)
    # Paquete 1 (corte en todos los modos): estas pruebas esperan el historial completo
    # de la muestra mini, así que llevan el corte de la mini a una fecha posterior a
    # todos sus datos (lo usan esta Herramientas y la de la app). El corte real
    # (FECHA_CORTE_MINI = 2016-01-01) se prueba en tests/test_corte.py.
    monkeypatch.setattr(config, "FECHA_CORTE_MINI", "2100-01-01")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)   # carátulas SVG, sin red
    return Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")


def _app_usuario_1() -> AppTest:
    """App MoviePredict con la sesión del usuario 1 de MovieLens ya iniciada (sin login)."""
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.session_state["usuario"] = {"userId": 1, "nombre": "usuario1"}
    return at


def _textos(at) -> str:
    # Markdown + HTML de los componentes de carátulas clicables + avisos.
    return html_pagina(at) + "\n".join(str(w.value) for w in at.warning)


# --------------------------------------------------------------------------- #
# App con el proveedor 'reglas'
# --------------------------------------------------------------------------- #

def test_app_reglas_usuario_1(entorno):
    tipo = entorno.perfil_gustos(1)["tipo"]
    at = _app_usuario_1()
    at.run()
    assert not at.exception, at.exception
    texto = _textos(at)
    assert tipo and tipo in texto                         # badge del tipo de espectador
    assert "LLM_PROVIDER:** `reglas`" in texto and "qwen3.5:9b" in texto
    assert "think:** `false`" in texto
    assert "Sin actualizaciones en vivo" in texto
    assert 'class="cr-caratula"' in texto                 # filas de carátulas de Inicio
    assert list(at.radio(key="seccion").options) == ["Inicio", "Buscar", "Explorar", "Chat"]

    ir_a(at, "Chat").chat_input[0].set_value(PEDIDO).run()
    assert not at.exception, at.exception
    respuesta = at.session_state["chat_1"][-1]
    ids = [r["movieId"] for r in respuesta["recomendaciones"]]
    # Paquete 3 (niveles): Harry y Sally (comedia de 95 min) cumple todo y va primero;
    # las demás siguen por nivel, ya no se descartan.
    assert ids[0] == 1307
    assert not set(ids) & VISTAS_U1
    assert all(r["explicacion"] for r in respuesta["recomendaciones"])
    assert "95 min" in respuesta["recomendaciones"][0]["explicacion"]
    assert "When Harry Met Sally" in _textos(at)


def test_app_aviso_si_ollama_no_responde(entorno, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://127.0.0.1:9")   # puerto cerrado
    at = _app_usuario_1()
    at.run()
    assert not at.exception, at.exception
    # El aviso aparece en la barra lateral al cargar, antes de escribir en el chat.
    assert llm.AVISO_REGLAS in [w.value for w in at.sidebar.warning]
    ir_a(at, "Chat").chat_input[0].set_value(PEDIDO).run()
    assert not at.exception, at.exception
    assert llm.AVISO_REGLAS in [w.value for w in at.main.warning]
    assert at.session_state["chat_1"][-1]["recomendaciones"]


def test_pestana_chat_drama_policial_entendi(entorno):
    """C6: la pestaña Chat responde el pedido con la línea «Entendí:» y tarjetas."""
    at = _app_usuario_1()
    at.run()
    ir_a(at, "Chat").chat_input[0].set_value("drama policial con un twist final").run()
    assert not at.exception, at.exception
    respuesta = at.session_state["chat_1"][-1]
    assert respuesta["role"] == "assistant"
    assert "Entendí:" in respuesta["content"]
    assert "Entendí:" in _textos(at)
    assert respuesta["recomendaciones"] and 'class="tarjeta"' in _textos(at)


def test_estado_ollama_con_clientes_simulados(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODEL", "qwen3.5:9b")

    class Lista:
        def __init__(self, nombres):
            self.nombres = nombres

        def list(self):
            return {"models": [{"model": n} for n in self.nombres]}

    class Caido:
        def list(self):
            raise ConnectionError("Failed to connect to Ollama")

    assert llm.estado_ollama(Lista(["qwen3.5:9b"])) == {"ok": True,
                                                        "mensaje": "Ollama listo (qwen3.5:9b)"}
    sin_modelo = llm.estado_ollama(Lista(["llama3:8b"]))
    assert not sin_modelo["ok"] and "ollama pull qwen3.5:9b" in sin_modelo["mensaje"]
    assert llm.estado_ollama(Caido()) == {"ok": False, "mensaje": llm.AVISO_REGLAS}


def test_leyenda_indica_completado_con_reglas(entorno):
    """Si el LLM no entrega recomendaciones válidas, el proveedor lo dice."""
    class SoloInventa:
        def chat(self, **kw):
            dato = {"intro": "x", "recomendaciones": [{"movieId": 999999, "titulo": "Falsa"}]}
            return {"message": {"role": "assistant", "content": json.dumps(dato)}}

    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=SoloInventa())
    # El LLM solo redacta: las tarjetas son las de pedido.py + recomendar.
    assert r.proveedor == "ollama" and r.descartadas == ["Falsa"]
    assert r.recomendaciones[0]["movieId"] == 1307          # nivel máximo primero
    assert "Falsa" not in r.texto + json.dumps(r.recomendaciones, default=str)


def test_reglas_determinista(entorno):
    a = llm.responder(PEDIDO, 1, entorno, proveedor="reglas")
    b = llm.responder(PEDIDO, 1, entorno, proveedor="reglas")
    assert a == b and a.aviso is None
    f = llm.interpretar("Algo CORTO y liviano")
    assert f["max_duracion_min"] == 100


# --------------------------------------------------------------------------- #
# Proveedor 'ollama' con un cliente simulado
# --------------------------------------------------------------------------- #

class ClienteFalso:
    """Imita ollama.Client.chat: primero pide 'recomendar' y luego responde."""

    def __init__(self, siempre_herramientas: bool = False):
        self.llamadas: list[dict] = []
        self.siempre = siempre_herramientas

    def chat(self, model, messages, tools=None, think=None, **kw):
        self.llamadas.append({"model": model, "tools": tools, "think": think,
                              "messages": list(messages)})
        if self.siempre:   # intenta usar herramientas: se ignoran (solo redacta)
            return {"message": {"role": "assistant", "content": "", "thinking": "SECRETO-1",
                                "tool_calls": [{"function": {"name": "recomendar", "arguments": {
                                    "userId": 1, "n": 5, "max_duracion_min": 100,
                                    "generos": ["Comedy", "Animation", "Children"]}}}]}}
        final = {"intro": "Para hoy te propongo esto.", "recomendaciones": [
            {"movieId": 1307, "explicacion": "Comedia breve que equilibra tu gusto por el suspenso."},
            {"movieId": 999999, "titulo": "Película Inventada", "explicacion": "no existe"},
            {"movieId": 2571, "explicacion": "ya la viste"}]}
        return {"message": {"role": "assistant", "thinking": "SECRETO-2",
                            "content": "<think>SECRETO-3</think>" + json.dumps(final)}}


def test_ollama_simulado_think_false_y_descarta_inventadas(entorno):
    cliente = ClienteFalso()
    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=cliente)
    assert all(c["think"] is False for c in cliente.llamadas)      # think=False por defecto
    assert cliente.llamadas[0]["model"] == "qwen3.5:9b"
    reglas = llm.responder(PEDIDO, 1, entorno, proveedor="reglas")
    assert ([x["movieId"] for x in r.recomendaciones]
            == [x["movieId"] for x in reglas.recomendaciones])     # misma lista final
    assert len(r.descartadas) == 2                                  # inventada y vista fuera
    assert r.proveedor == "ollama" and r.aviso is None and "recomendar" in r.herramientas_usadas
    assert r.recomendaciones[0]["explicacion"].startswith("Comedia breve")
    todo = r.texto + json.dumps(r.recomendaciones, default=str)
    assert "Inventada" not in todo and "SECRETO" not in todo        # sin thinking
    assert "95 min" in r.recomendaciones[0]["explicacion"]


def test_llm_think_true_por_entorno(entorno, monkeypatch):
    monkeypatch.setenv("LLM_THINK", "true")
    try:
        importlib.reload(config)
        assert config.LLM_THINK is True
        cliente = ClienteFalso()
        r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=cliente)
        assert cliente.llamadas[0]["think"] is True
        assert "SECRETO" not in r.texto + json.dumps(r.recomendaciones, default=str)
    finally:
        monkeypatch.delenv("LLM_THINK")
        importlib.reload(config)
    assert config.LLM_THINK is False


def test_una_sola_llamada_sin_herramientas(entorno):
    """El modelo solo redacta: una llamada, sin herramientas; sus tool_calls se ignoran."""
    cliente = ClienteFalso(siempre_herramientas=True)
    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=cliente)
    assert len(cliente.llamadas) == 1 and cliente.llamadas[0]["tools"] is None
    assert r.proveedor == llm.PROVEEDOR_COMPLETADO and r.recomendaciones
    assert not set(x["movieId"] for x in r.recomendaciones) & VISTAS_U1


def test_ollama_caido_usa_reglas_con_aviso(entorno):
    class Caido:
        def chat(self, **kw):
            raise ConnectionError("Failed to connect to Ollama")

    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=Caido())
    assert r.aviso == llm.AVISO_REGLAS and r.proveedor == "reglas"
    assert r.recomendaciones[0]["movieId"] == 1307           # nivel máximo primero
