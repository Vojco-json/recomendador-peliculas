"""Paquete 4: red de seguridad del chat, regla de sistema y título inexistente."""

from __future__ import annotations

import json

import pytest

from recomendador import config, llm
from recomendador.enriquecimiento import ClienteTMDb
from recomendador.herramientas import Herramientas


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover
        raise AssertionError("no debe haber llamadas de red")


@pytest.fixture(scope="module")
def herr(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("red")
    tmdb = ClienteTMDb(api_key="", cache=tmp / "cache.json", sesion=SesionProhibida())
    return Herramientas(config.MUESTRA_MINI, tmp / "art", tmp / "estado.db", tmdb=tmdb)


class SinPeliculas:
    def chat(self, **kw):
        return {"message": {"role": "assistant", "content": "No sé qué recomendarte."}}


def test_sistema_tiene_regla_de_animos_y_buscar():
    s = llm.SISTEMA.lower()
    assert "ánimos" in s and "ocasiones" in s and "buscar_pelicula" in s


def test_red_llama_a_recomendar_con_lo_detectado(herr, monkeypatch):
    llamadas = []
    original = herr.recomendar

    def espia(*a, **k):
        llamadas.append(k)
        return original(*a, **k)

    monkeypatch.setattr(herr, "recomendar", espia)
    r = llm.responder_ollama("una comedia corta", 1, herr, cliente=SinPeliculas())
    assert llamadas and llamadas[-1]["max_duracion_min"] == 100
    assert "Comedy" in llamadas[-1]["generos"]
    assert r.recomendaciones and r.proveedor == llm.PROVEEDOR_COMPLETADO
    assert "No pude obtener" not in r.texto


def test_condiciones_en_cada_recomendacion(herr):
    r = llm.responder("una comedia corta", 1, herr, proveedor="reglas")
    for rec in r.recomendaciones:
        assert {"cumple", "no_cumple", "no_evaluables"} <= set(rec)
        assert rec["condiciones_texto"].startswith(("Cumple", "No cumple", "Sin datos"))
    assert "Cumple: género: comedia" in r.recomendaciones[0]["condiciones_texto"]


@pytest.mark.parametrize("proveedor", ["reglas", "ollama"])
def test_titulo_inexistente_una_frase_y_recomendaciones(herr, proveedor):
    r = llm.responder("algo como El mecanismo", 1, herr, proveedor=proveedor,
                      cliente=SinPeliculas())
    assert "No encontré «El mecanismo» en el catálogo" in r.texto
    assert r.recomendaciones


def test_titulo_existente_es_referencia_y_no_se_recomienda(herr):
    r = llm.responder("algo como Toy Story", 1, herr, proveedor="reglas")
    assert "Toy Story" in r.texto and r.recomendaciones
    assert 1 not in [x["movieId"] for x in r.recomendaciones]


def test_saludo_no_recomienda(herr):
    r = llm.responder("hola", 1, herr, proveedor="reglas")
    assert r.texto.startswith(llm.AYUDA) and r.recomendaciones == []
    assert "Entendí: no es un pedido" in r.texto


def test_tarjeta_de_la_app_muestra_condiciones():
    """La app pinta el resumen de condiciones dentro de la tarjeta.

    Desde MoviePredict (paquete 9) la tarjeta del chat vive en app/vistas/chat.py.
    """
    fuente = "".join((config.RAIZ / "app" / r).read_text(encoding="utf-8")
                     for r in ("app.py", "vistas/chat.py"))
    assert "condiciones_texto" in fuente and 'class="cond"' in fuente
    assert "No pude obtener" not in (config.RAIZ / "recomendador" / "llm.py").read_text(
        encoding="utf-8")
    json.dumps(llm.resumen_condiciones({"condiciones_pedidas": 1, "cumple": ["x"],
                                        "no_cumple": [], "no_evaluables": []}))
