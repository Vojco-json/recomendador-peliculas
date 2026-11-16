"""Pedido 4: el LLM solo redacta; pedido.py es la única fuente de los filtros."""

from __future__ import annotations

import json
import time

import pytest

from recomendador import config, llm
from recomendador.enriquecimiento import ClienteTMDb
from recomendador.herramientas import Herramientas

PEDIDO = "drama policial con un twist final"


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover
        raise AssertionError("no debe haber llamadas de red")


@pytest.fixture(scope="module")
def herr(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("redactor")
    tmdb = ClienteTMDb(api_key="", cache=tmp / "cache.json", sesion=SesionProhibida())
    return Herramientas(config.MUESTRA_MINI, tmp / "art", tmp / "estado.db", tmdb=tmdb)


class LLMTramposo:
    """Intenta omitir Crime y agregar duración (por herramientas y en el texto)."""

    def __init__(self):
        self.datos = None
        self.tools = "sin llamar"

    def chat(self, model, messages, tools=None, think=None, **kw):
        self.tools = tools
        self.datos = json.loads(messages[-1]["content"].split("DATOS: ", 1)[1])
        final = {"intro": "Solo dramas de 90 min.", "filtros": {"generos": ["Drama"],
                                                              "max_duracion_min": 90},
                 "recomendaciones": [{"movieId": 999999, "titulo": "Inventada"}]}
        return {"message": {"role": "assistant", "content": json.dumps(final), "tool_calls": [
            {"function": {"name": "recomendar", "arguments": {
                "generos": ["Drama"], "max_duracion_min": 90}}}]}}


def test_filtros_finales_no_los_cambia_el_llm(herr, monkeypatch):
    llamadas = []
    original = herr.recomendar

    def espia(*a, **k):
        llamadas.append(k)
        return original(*a, **k)

    monkeypatch.setattr(herr, "recomendar", espia)
    cliente = LLMTramposo()
    r = llm.responder_ollama(PEDIDO, 1, herr, cliente=cliente)
    assert cliente.tools is None                       # el modelo no recibe herramientas
    assert len(llamadas) == 1                          # solo el código llama a recomendar
    for f in (llamadas[0], r.filtros, cliente.datos["filtros"]):
        assert set(f["generos"]) == {"Crime", "Drama"}
        assert "twist ending" in f["tags"]
        assert not f["max_duracion_min"]
    assert r.recomendaciones and r.descartadas == ["Inventada"]
    assert "Inventada" not in r.texto + json.dumps(r.recomendaciones, default=str)


def test_llm_recibe_condiciones_parecida_y_afinidad(herr):
    cliente = LLMTramposo()
    llm.responder_ollama(PEDIDO, 1, herr, cliente=cliente)
    item = cliente.datos["recomendaciones"][0]
    assert {"cumple", "no_cumple", "sin_datos", "pelicula_parecida_calificada",
            "afinidad"} <= set(item)
    assert cliente.datos["entendi"].startswith("Entendí:")


@pytest.mark.parametrize("proveedor", ["reglas", "ollama"])
def test_linea_entendi_en_cada_respuesta(herr, proveedor):
    for mensaje in (PEDIDO, "algo corto y liviano", "hola"):
        r = llm.responder(mensaje, 1, herr, proveedor=proveedor, cliente=LLMTramposo())
        assert "Entendí: " in r.texto, mensaje
    r = llm.responder(PEDIDO, 1, herr, proveedor=proveedor, cliente=LLMTramposo())
    linea = r.texto.split("Entendí: ", 1)[1]
    assert "crimen" in linea.lower() and "drama" in linea.lower()
    assert "twist ending" in linea and "min" not in linea


def test_ollama_lento_usa_plantilla_de_reglas(herr, monkeypatch):
    monkeypatch.setattr(llm, "LIMITE_RESPUESTA_S", 0.3)

    class Lento:
        def chat(self, **kw):
            time.sleep(2)
            return {"message": {"content": "{}"}}

    inicio = time.monotonic()
    r = llm.responder(PEDIDO, 1, herr, proveedor="ollama", cliente=Lento())
    assert time.monotonic() - inicio < 1.9
    assert r.proveedor == "reglas" and r.aviso == llm.AVISO_REGLAS and r.recomendaciones
    assert llm.LIMITE_RESPUESTA_S == 0.3 and llm.TIMEOUT_OLLAMA_S == 20


def test_ollama_y_reglas_dan_la_misma_lista(herr):
    a = llm.responder(PEDIDO, 1, herr, proveedor="reglas")
    b = llm.responder(PEDIDO, 1, herr, proveedor="ollama", cliente=LLMTramposo())
    assert [x["movieId"] for x in a.recomendaciones] == [x["movieId"] for x in b.recomendaciones]
