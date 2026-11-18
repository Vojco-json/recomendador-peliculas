"""Proveedor 'api' sin red (req. 68-73, 78, 85): respaldo, cupo, caché y claves."""

from __future__ import annotations

import json
import threading

import pytest
import requests

from recomendador import config, proveedores


class Resp:
    def __init__(self, codigo=200, texto="hola"):
        self.status_code, self._texto = codigo, texto

    def json(self):
        return {"choices": [{"message": {"content": self._texto}}]}


class Post:
    """requests.post simulado: una respuesta (o excepción) por URL."""

    def __init__(self, groq, gemini=None):
        self.respuestas = {"groq": groq, "gemini": gemini}
        self.llamadas: list[str] = []
        self.headers: list[dict] = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        assert timeout == proveedores.TIMEOUT_API_S == 15
        nombre = "groq" if "groq" in url else "gemini"
        self.llamadas.append(nombre)
        self.headers.append(headers)
        r = self.respuestas[nombre]
        if isinstance(r, Exception):
            raise r
        return r


@pytest.fixture
def claves(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "clave-groq-secreta")
    monkeypatch.setenv("GEMINI_API_KEY", "clave-gemini-secreta")


def _completar(post, presupuesto=None, uso="chat"):
    return proveedores.completar([{"role": "user", "content": "hola"}], "m", uso=uso,
                                 post=post, presupuesto=presupuesto or proveedores.Presupuesto(
                                     100, 1000), semaforo=threading.BoundedSemaphore(5))


def test_valores_por_defecto():
    assert config.LLM_PROVIDER in ("api", "reglas", "ollama")
    assert config.LLM_MODELO_CHAT == "openai/gpt-oss-120b"
    assert config.LLM_MODELO_TARJETAS == "llama-3.1-8b-instant"
    assert config.LLM_CONCURRENCIA == 5
    assert proveedores.URL_GROQ == "https://api.groq.com/openai/v1"
    assert proveedores.URL_GEMINI == "https://generativelanguage.googleapis.com/v1beta/openai/"


def test_429_pasa_a_gemini(claves):
    post = Post(Resp(429), Resp(200, "desde gemini"))
    assert _completar(post) == ("desde gemini", "gemini")
    assert post.llamadas == ["groq", "gemini"]


def test_timeout_y_red_caen_a_reglas(claves):
    post = Post(requests.Timeout("lento"), requests.ConnectionError("sin red"))
    assert _completar(post) == (None, "reglas")


def test_sin_gemini_solo_groq(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    post = Post(Resp(429))
    assert _completar(post) == (None, "reglas")
    assert post.llamadas == ["groq"]


def test_sin_claves_no_llama(monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    post = Post(Resp(200))
    assert _completar(post) == (None, "reglas") and post.llamadas == []


def test_claves_solo_en_cabecera_no_en_logs(claves, caplog):
    post = Post(requests.ConnectionError("clave-groq-secreta"), Resp(200, "ok"))
    with caplog.at_level("DEBUG"):
        _completar(post)
    assert post.headers[0]["Authorization"] == "Bearer clave-groq-secreta"
    assert "secreta" not in caplog.text
    assert "secreta" not in json.dumps(proveedores.estado())
    fuente = (config.RAIZ / "recomendador" / "proveedores.py").read_text(encoding="utf-8")
    assert "gsk_" not in fuente and "AIza" not in fuente


def test_prioridad_del_chat_y_tope_diario():
    reloj = [0.0]
    p = proveedores.Presupuesto(10, 12, reloj=lambda: reloj[0])
    assert all(p.tomar("tarjeta") for _ in range(8))      # hasta que quedan 2 (< 30 %)
    assert p.restante_minuto() == 2
    assert not p.tomar("tarjeta")                          # queda < 30 %: reservado al chat
    assert p.tomar("chat") and p.tomar("chat") and not p.tomar("chat")
    reloj[0] = 61                                          # minuto nuevo
    assert p.tomar("chat") and p.tomar("chat")             # 12 del día
    assert p.agotado_dia() and not p.tomar("chat")         # tope diario: todo a reglas


def test_semaforo_lleno_no_espera(claves):
    sem = threading.BoundedSemaphore(1)
    sem.acquire()
    post = Post(Resp(200))
    r = proveedores.completar([], "m", post=post, presupuesto=proveedores.Presupuesto(9, 9),
                              semaforo=sem)
    assert r == (None, "reglas") and post.llamadas == []


def test_cache_tarjetas_compartida_y_una_llamada_para_el_par(claves, monkeypatch):
    import sys
    sys.path.insert(0, str(config.RAIZ / "app"))
    from vistas import tarjetas

    monkeypatch.setattr(config, "LLM_PROVIDER", "api")
    monkeypatch.setattr(proveedores, "PRESUPUESTO", proveedores.Presupuesto(100, 1000))
    proveedores.CACHE_TARJETAS.clear()
    e1 = {"movieId": 1, "veredicto": "Probablemente te guste", "titulo": "Heat",
          "texto": "plantilla 1", "tags_a_favor": ["dark"]}
    e2 = {"movieId": 2, "veredicto": "Puede que no", "titulo": "Cars", "texto": "plantilla 2"}
    post = Post(Resp(200, json.dumps({"probable": "Te va a gustar por lo oscura.",
                                      "improbable": "Es demasiado liviana para ti."})))
    textos = tarjetas.redactar_par({"probable": e1, "improbable": e2}, post=post)
    assert post.llamadas == ["groq"]                       # una sola llamada para las dos
    assert textos["probable"].startswith("Te va a gustar")
    # Otra sesión (sin memoria propia) reutiliza la caché del proceso: sin llamar.
    monkeypatch.setattr(proveedores.requests, "post", lambda *a, **k: 1 / 0)
    assert tarjetas.redactar(e1) == ("Te va a gustar por lo oscura.", "llm")
    assert f"1|{e1['veredicto']}|plantilla 1" in proveedores.CACHE_TARJETAS
