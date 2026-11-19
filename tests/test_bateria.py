"""Paquete 4: batería de 30 pedidos variados en modo reglas y con 3 LLM simulados.

Cada pedido de recomendación termina con al menos una recomendación válida del
catálogo (y con el resumen de condiciones); cada mensaje que no es un pedido recibe
la ayuda o el saludo, sin tarjetas. No usa Spark, Ollama ni red: Herramientas se crea
una vez por módulo sobre la muestra mini (sin artefactos ALS, ordena por popularidad).
"""

from __future__ import annotations

import json

import pytest

from recomendador import config, llm
from recomendador.enriquecimiento import ClienteTMDb
from recomendador.herramientas import Herramientas

USUARIO = 1

PEDIDOS = [
    "algo corto y liviano para hoy",
    "una comedia",
    "quiero una de terror",
    "algo para llorar",
    "una película para ver en familia",
    "algo que me haga pensar",
    "una con giro final",
    "dame 3 de ciencia ficción",
    "recomiéndame un drama de menos de 90 minutos",
    "una comdia romantica",                      # error de tipeo
    "pelicula de terorr cortita",                 # error de tipeo
    "something like Toy Story",
    "algo como Matrix pero corto",
    "algo como El mecanismo",                     # título que no está en el catálogo
    "I want a sad movie",
    "recommend me a short comedy",
    "a scary movie for tonight",
    "una de acción y aventura",
    "sorpréndeme",
    "algo para esta noche",
    "películas de guerra",
    "una animada para niños",
    "un thriller de menos de 2 horas",
    "algo divertido y buena onda",
    "una peli de crimen policial",
]
NO_PEDIDOS = ["hola", "gracias!", "¿cómo funcionas?", "buenos días", "ok perfecto"]
# Casos agregados en la corrección por niveles (pedido 3 del usuario): se suman a
# los 30 originales y se prueban en los mismos 4 modos.
PEDIDOS_NIVELES = [
    "drama policial con un twist épico",
    "algo como de drama policial con un twist épico",
]
MENSAJES = ([(m, True) for m in PEDIDOS] + [(m, False) for m in NO_PEDIDOS]
            + [(m, True) for m in PEDIDOS_NIVELES])
assert len(PEDIDOS) + len(NO_PEDIDOS) == 30 and len(MENSAJES) == 32


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover - si se llama, la prueba falla
        raise AssertionError("no debe haber llamadas de red")


@pytest.fixture(scope="module")
def herr(tmp_path_factory):
    """Herramientas única para todo el módulo (la batería debe tardar < 2 min)."""
    tmp = tmp_path_factory.mktemp("bateria")
    tmdb = ClienteTMDb(api_key="", cache=tmp / "cache.json", sesion=SesionProhibida())
    return Herramientas(config.MUESTRA_MINI, tmp / "art", tmp / "estado.db", tmdb=tmdb)


# --------------------------------------------------------------------------- #
# LLM simulados (imitan ollama.Client.chat)
# --------------------------------------------------------------------------- #

def _ultimo_usuario(messages) -> str:
    texto = next(m["content"] for m in reversed(messages) if m["role"] == "user")
    return texto.split(") ", 1)[-1]


class LLMBueno:
    """Llama a recomendar SIN filtros (el código debe completarlos) y propone lo devuelto."""

    def chat(self, model, messages, tools=None, think=None, **kw):
        if _ultimo_usuario(messages) in NO_PEDIDOS:
            return {"message": {"role": "assistant", "content": "¡Hola! ¿Qué quieres ver?"}}
        if messages[-1]["role"] != "tool":
            return {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"function": {"name": "recomendar", "arguments": {"userId": USUARIO}}}]}}
        resultado = json.loads(messages[-1]["content"])
        final = {"intro": "Te propongo estas.", "recomendaciones": [
            {"movieId": r["movieId"], "explicacion": "Encaja con tu pedido."}
            for r in resultado["recomendaciones"][:3]]}
        return {"message": {"role": "assistant", "content": json.dumps(final)}}


class LLMSinPeliculas:
    """Responde solo texto, sin herramientas ni JSON."""

    def chat(self, **kw):
        return {"message": {"role": "assistant", "content": "Lo siento, no tengo ideas."}}


class LLMInventa:
    """Propone movieIds que no existen ni salieron de recomendar."""

    def chat(self, **kw):
        dato = {"intro": "Mira estas.", "recomendaciones": [
            {"movieId": 999999, "titulo": "Película Inventada", "explicacion": "x"},
            {"movieId": 888888, "titulo": "Otra Falsa", "explicacion": "y"}]}
        return {"message": {"role": "assistant", "content": json.dumps(dato)}}


MODOS = {"reglas": None, "llm_bueno": LLMBueno, "llm_sin_peliculas": LLMSinPeliculas,
         "llm_inventa": LLMInventa}


@pytest.mark.parametrize("modo", list(MODOS))
@pytest.mark.parametrize("mensaje, es_pedido", MENSAJES)
def test_bateria(herr, mensaje, es_pedido, modo):
    if modo == "reglas":
        r = llm.responder(mensaje, USUARIO, herr, proveedor="reglas")
    else:
        r = llm.responder_ollama(mensaje, USUARIO, herr, cliente=MODOS[modo]())
    todo = r.texto + json.dumps(r.recomendaciones, default=str, ensure_ascii=False)
    assert "No pude obtener" not in todo
    assert "Inventada" not in todo and "Falsa" not in todo
    assert r.texto.strip()
    if not es_pedido:
        assert r.recomendaciones == [], mensaje
        return
    assert r.recomendaciones, f"{modo}: {mensaje!r} sin recomendaciones"
    catalogo = herr.catalogo()
    vistas = {p["movieId"] for p in
              herr.peliculas_calificadas(USUARIO, n=200)["peliculas"]}
    for rec in r.recomendaciones:
        assert rec["movieId"] in catalogo and rec["movieId"] not in vistas
        assert rec["explicacion"] and rec["condiciones_texto"]


def test_llm_bueno_recibe_filtros_completados(herr):
    """El modelo omitió los filtros: el código los completa con pedido.py."""
    vistos = []

    class Espia(LLMBueno):
        def chat(self, model, messages, tools=None, think=None, **kw):
            datos = json.loads(messages[-1]["content"].split("DATOS: ", 1)[1])
            vistos.append(datos["filtros"])
            final = {"intro": "Te propongo estas.", "recomendaciones": [
                {"movieId": r["movieId"], "explicacion": "Encaja."}
                for r in datos["recomendaciones"]]}
            return {"message": {"role": "assistant", "content": json.dumps(final)}}

    r = llm.responder_ollama("algo corto y liviano para hoy", USUARIO, herr, cliente=Espia())
    assert r.proveedor == "ollama" and r.recomendaciones
    assert vistos[0]["max_duracion_min"] == 100 and "algo liviano" in vistos[0]["temas"]
