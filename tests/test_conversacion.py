"""Chat conversacional (req. 74-83, 85): 10+ conversaciones de varios turnos, sin red.

Cada conversación se corre en modo reglas y con un LLM simulado (requests.post falso
que devuelve el JSON de intención y una redacción). Se comprueba el estado esperado
por turno y que ningún título nombrado quede fuera del contexto entregado.
"""

from __future__ import annotations

import json
import re

import pandas as pd
import pytest

from recomendador import config, conversacion, proveedores
from recomendador.herramientas import Herramientas


@pytest.fixture(scope="module")
def herr(ruta_mini, tmp_path_factory):
    tmp = tmp_path_factory.mktemp("conv")
    return Herramientas(ruta_mini, tmp, tmp / "estado.db")


@pytest.fixture(scope="module")
def uid(ruta_mini):
    r = pd.read_csv(ruta_mini / "ratings.csv", nrows=2000)
    return int(r["userId"].value_counts().index[0])


@pytest.fixture
def reglas(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")


def _titulos_ok(t, estado, herr, uid):
    """Todo «título» citado está en el contexto (mostradas, calificadas o recs)."""
    ctx = conversacion.contexto(estado, uid, herr)
    assert conversacion.texto_valido(t.texto.split("\n\n")[0], ctx, t.recomendaciones) or \
        not re.findall(r"«([^»]+)»", t.texto.split("\n\n")[0]), t.texto


def _ids(estado):
    return [m["movieId"] for m in estado["mostradas"]]


# --------------------------------------------------------------------------- #
# Modo reglas: conversaciones de varios turnos
# --------------------------------------------------------------------------- #

def test_c1_pedido_ajuste_mas_descartar(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    t = conversacion.turno("drama policial con un giro final", e, uid, herr)
    assert t.intencion == "nuevo_pedido" and {"Crime", "Drama"} <= set(e["generos"])
    assert t.recomendaciones and "Entendí:" in t.texto
    _titulos_ok(t, e, herr, uid)
    t = conversacion.turno("pero que sea corta", e, uid, herr)
    assert t.intencion == "ajuste" and e["max_duracion_min"]
    assert {"Crime", "Drama"} <= set(e["generos"])                    # mantiene el resto
    antes = _ids(e)
    t = conversacion.turno("dame otras", e, uid, herr)
    assert t.intencion == "mas"
    assert not set(r["movieId"] for r in t.recomendaciones) & set(antes)   # no repite
    primera = e["mostradas"][0]["movieId"]
    t = conversacion.turno("la primera ya la vi", e, uid, herr)
    assert t.intencion == "descartar" and primera in e["descartadas"]
    assert primera not in _ids(e)
    t = conversacion.turno("otra", e, uid, herr)
    assert primera not in [r["movieId"] for r in t.recomendaciones]


def test_c2_pregunta_por_pelicula_mostrada(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    conversacion.turno("una comedia", e, uid, herr)
    segunda = e["mostradas"][1]
    t = conversacion.turno("¿de qué trata la segunda?", e, uid, herr)
    assert t.intencion == "pregunta_pelicula" and not t.recomendaciones
    assert segunda["titulo"] in t.texto
    _titulos_ok(t, e, herr, uid)


def test_c3_pregunta_perfil_no_cambia_condiciones(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    conversacion.turno("algo de terror", e, uid, herr)
    antes = dict(generos=list(e["generos"]), tags=list(e["tags"]))
    t = conversacion.turno("¿qué tipo de espectador soy según mis gustos?", e, uid, herr)
    assert t.intencion == "pregunta_perfil" and e["generos"] == antes["generos"]
    _titulos_ok(t, e, herr, uid)


def test_c4_charla_sin_tarjetas(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    t = conversacion.turno("hola, ¿cómo estás?", e, uid, herr)
    assert t.intencion == "charla" and not t.recomendaciones
    assert "Entendí:" in t.texto


def test_c5_nuevo_pedido_reemplaza(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    conversacion.turno("drama policial", e, uid, herr)
    t = conversacion.turno("mejor una de animación", e, uid, herr)
    assert t.intencion == "nuevo_pedido" and e["generos"] == ["Animation"]


def test_c6_chip_quita_y_recalcula(reglas, herr, uid):
    e = conversacion.estado_nuevo()
    conversacion.turno("comedia romántica corta", e, uid, herr)
    claves = [c for c, _ in conversacion.etiquetas(e)]
    assert "duracion" in claves and "genero:Comedy" in claves
    conversacion.quitar_condicion(e, "duracion")
    t = conversacion.recalcular(e, uid, herr)
    assert e["max_duracion_min"] is None and t.recomendaciones
    assert "duración" not in t.entendi and "comedia" in t.entendi.lower()


def test_c7_estado_nuevo_vacio():
    e = conversacion.estado_nuevo()
    assert not e["mostradas"] and not e["generos"] and not e["mensajes"]


# --------------------------------------------------------------------------- #
# LLM simulado
# --------------------------------------------------------------------------- #

class LLMFalso:
    """requests.post simulado: interpreta con las intenciones dadas y redacta."""

    def __init__(self, intenciones: list[dict], redaccion: str | None = None):
        self.intenciones = list(intenciones)
        self.redaccion = redaccion
        self.mensajes_recibidos: list = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.mensajes_recibidos.append(json["messages"])
        es_interpretar = "Interpretas" in json["messages"][0]["content"]
        if es_interpretar:
            contenido = __import__("json").dumps(self.intenciones.pop(0))
        else:
            datos = __import__("json").loads(json["messages"][1]["content"])
            contenido = self.redaccion or "Te dejo opciones según tus gustos."
            if self.redaccion == "{primera}" and datos["nuevas"]:
                contenido = f"Parte por «{datos['nuevas'][0]['titulo']}», calza contigo."

        class R:
            status_code = 200

            def json(self_):
                return {"choices": [{"message": {"content": contenido}}]}
        return R()


@pytest.fixture
def api(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "api")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(proveedores, "PRESUPUESTO", proveedores.Presupuesto(1000, 10000))


def test_c8_llm_flujo_completo(api, herr, uid):
    post = LLMFalso([
        {"intencion": "nuevo_pedido", "generos": ["Crime", "Drama", "Inventado"],
         "tags": ["twist ending", "tag que no existe"], "personas": []},
        {"intencion": "ajuste", "max_duracion_min": 110},
        {"intencion": "mas"},
        {"intencion": "descartar", "pelicula": 1},
    ], redaccion="{primera}")
    e = conversacion.estado_nuevo()
    t = conversacion.turno("drama policial con twist", e, uid, herr, post=post)
    assert t.proveedor == "groq" and set(e["generos"]) == {"Crime", "Drama"}   # lo inválido se ignora
    assert "tag que no existe" not in e["tags"]
    assert t.recomendaciones[0]["titulo"] in t.texto
    t = conversacion.turno("pero cortita", e, uid, herr, post=post)
    assert e["max_duracion_min"] == 110 and {"Crime", "Drama"} <= set(e["generos"])
    vistos = _ids(e)
    t = conversacion.turno("más", e, uid, herr, post=post)
    assert not set(r["movieId"] for r in t.recomendaciones) & set(vistos)
    primera = e["mostradas"][0]["movieId"]
    conversacion.turno("esa ya la vi", e, uid, herr, post=post)
    assert primera in e["descartadas"]
    # El modelo recibió como máximo los últimos 8 mensajes.
    ultimo = json.loads(post.mensajes_recibidos[-2][1]["content"])
    assert len(ultimo["ultimos_mensajes"]) <= 8


def test_c9_llm_inventa_titulo_usa_plantilla(api, herr, uid):
    post = LLMFalso([{"intencion": "nuevo_pedido", "generos": ["Comedy"]}],
                    redaccion="Te recomiendo «Película Inventada 3000», es genial.")
    e = conversacion.estado_nuevo()
    t = conversacion.turno("una comedia", e, uid, herr, post=post)
    assert "Película Inventada" not in t.texto and t.texto.startswith("Busqué")  # plantilla
    assert t.recomendaciones


def test_c10_llm_pelicula_no_mostrada_se_ignora(api, herr, uid):
    post = LLMFalso([{"intencion": "nuevo_pedido", "generos": ["Comedy"]},
                     {"intencion": "descartar", "pelicula": 99}])
    e = conversacion.estado_nuevo()
    conversacion.turno("una comedia", e, uid, herr, post=post)
    n = len(e["mostradas"])
    conversacion.turno("saca la 99", e, uid, herr, post=post)
    assert len(e["mostradas"]) == n and not e["descartadas"]


def test_c11_api_caida_usa_reglas(monkeypatch, herr, uid):
    monkeypatch.setattr(config, "LLM_PROVIDER", "api")
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    def caida(*a, **k):
        class R:
            status_code = 429
        return R()
    e = conversacion.estado_nuevo()
    t = conversacion.turno("algo de terror", e, uid, herr, post=caida)
    assert t.proveedor == "reglas" and t.intencion == "nuevo_pedido" and t.recomendaciones
    assert "Horror" in e["generos"]
