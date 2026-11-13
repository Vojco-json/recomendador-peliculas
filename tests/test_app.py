"""App Streamlit (AppTest) y chat LLM: proveedor 'reglas' y cliente Ollama simulado.

Los artefactos mini se construyen en un directorio temporal (tests/base_mini.py);
no se usa nada de 25M ni se necesita Ollama corriendo.
"""

from __future__ import annotations

import importlib
import json

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import crear_artefactos_app
from recomendador import config, llm
from recomendador.herramientas import Herramientas

VISTAS_U1 = {1, 6, 32, 47, 50, 110, 260, 296, 318, 356, 364, 480, 589, 593, 1036, 1721,
             2571, 4306, 6377, 58559}
APP = config.RAIZ / "app" / "app.py"
PEDIDO = "algo corto y liviano para hoy"


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_app(spark, ruta_mini, tmp_path_factory.mktemp("art_app"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    """Apunta la configuración a la muestra mini y a un estado.db temporal."""
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.setattr(config, "LLM_THINK", False)
    return Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")


def _textos(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown) + "\n".join(
        str(w.value) for w in at.warning)


# --------------------------------------------------------------------------- #
# App con el proveedor 'reglas'
# --------------------------------------------------------------------------- #

def test_app_reglas_usuario_1(entorno):
    tipo = entorno.perfil_gustos(1)["tipo"]
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    assert not at.exception, at.exception
    texto = _textos(at)
    assert tipo and tipo in texto                         # badge del tipo de espectador
    assert "LLM_PROVIDER:** `reglas`" in texto and "qwen3.5:9b" in texto
    assert "think:** `false`" in texto
    assert "Sin actualizaciones en vivo" in texto
    assert 'class="tarjeta"' in texto                     # tarjetas de recomendaciones

    at.chat_input[0].set_value(PEDIDO).run()
    assert not at.exception, at.exception
    respuesta = at.session_state["chat_1"][-1]
    ids = [r["movieId"] for r in respuesta["recomendaciones"]]
    assert ids == [1307]                                   # Harry y Sally: comedia de 95 min
    assert not set(ids) & VISTAS_U1
    assert all(r["explicacion"] and "95 min" in r["explicacion"]
               for r in respuesta["recomendaciones"])
    assert "When Harry Met Sally" in _textos(at)


def test_app_aviso_si_ollama_no_responde(entorno, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "ollama")
    monkeypatch.setattr(config, "OLLAMA_HOST", "http://127.0.0.1:9")   # puerto cerrado
    at = AppTest.from_file(str(APP), default_timeout=60)
    at.run()
    at.chat_input[0].set_value(PEDIDO).run()
    assert not at.exception, at.exception
    assert llm.AVISO_REGLAS in [w.value for w in at.warning]
    assert at.session_state["chat_1"][-1]["recomendaciones"]


def test_reglas_determinista(entorno):
    a = llm.responder(PEDIDO, 1, entorno, proveedor="reglas")
    b = llm.responder(PEDIDO, 1, entorno, proveedor="reglas")
    assert a == b and a.aviso is None
    f = llm.interpretar("Algo CORTO y liviano")
    assert f["max_duracion_min"] == 100
    assert {"Comedy", "Animation", "Family"} <= set(f["generos"])


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
        if self.siempre or len(self.llamadas) == 1:
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
    assert [x["movieId"] for x in r.recomendaciones] == [1307]     # inventada y vista fuera
    assert len(r.descartadas) == 2
    assert r.proveedor == "ollama" and r.aviso is None and "recomendar" in r.herramientas_usadas
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


def test_bucle_de_herramientas_limitado(entorno):
    cliente = ClienteFalso(siempre_herramientas=True)
    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=cliente)
    assert len(cliente.llamadas) == llm.MAX_RONDAS_HERRAMIENTAS + 1
    assert cliente.llamadas[-1]["tools"] is None
    assert not set(x["movieId"] for x in r.recomendaciones) & VISTAS_U1


def test_ollama_caido_usa_reglas_con_aviso(entorno):
    class Caido:
        def chat(self, **kw):
            raise ConnectionError("Failed to connect to Ollama")

    r = llm.responder(PEDIDO, 1, entorno, proveedor="ollama", cliente=Caido())
    assert r.aviso == llm.AVISO_REGLAS and r.proveedor == "reglas"
    assert [x["movieId"] for x in r.recomendaciones] == [1307]
