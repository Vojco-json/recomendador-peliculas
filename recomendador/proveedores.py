"""Proveedor 'api': APIs gratuitas compatibles con OpenAI con cadena de respaldo.

Cadena: Groq -> Gemini (solo si GEMINI_API_KEY existe) -> modo reglas (``None``).
Ante un 429, un error de red, una respuesta inválida o más de 15 s se pasa al
siguiente sin mostrar errores. Las claves se leen del entorno en cada llamada y
nunca se escriben en código, artefactos, logs ni respuestas.

Además vive aquí lo compartido por todas las sesiones del proceso:
- ``SEMAFORO_LLM``: hasta LLM_CONCURRENCIA llamadas simultáneas (si está lleno,
  quien llega recibe la plantilla al instante, sin esperar).
- ``PRESUPUESTO``: tope por minuto y por día. Con menos del 30 % del cupo del
  minuto, las tarjetas usan la plantilla y lo que queda se reserva para el chat;
  al agotar el tope diario todo pasa a modo reglas.
- ``CACHE_TARJETAS``: caché por proceso de las redacciones de tarjetas.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque

import requests

from . import config

log = logging.getLogger(__name__)

TIMEOUT_API_S = 15
URL_GROQ = "https://api.groq.com/openai/v1"
URL_GEMINI = "https://generativelanguage.googleapis.com/v1beta/openai/"
RESERVA_CHAT = 0.30           # fracción del cupo por minuto reservada para el chat

SEMAFORO_LLM = threading.BoundedSemaphore(config.LLM_CONCURRENCIA)


class Presupuesto:
    """Cupo de llamadas por minuto y por día, compartido por todas las sesiones."""

    def __init__(self, por_minuto: int | None = None, por_dia: int | None = None,
                 reloj=time.time):
        self.por_minuto = por_minuto or config.LLM_MAX_POR_MINUTO
        self.por_dia = por_dia or config.LLM_MAX_POR_DIA
        self._reloj = reloj
        self._minuto: deque[float] = deque()
        self._dia: deque[float] = deque()
        self._lock = threading.Lock()

    def _limpiar(self, ahora: float) -> None:
        while self._minuto and ahora - self._minuto[0] >= 60:
            self._minuto.popleft()
        while self._dia and ahora - self._dia[0] >= 86_400:
            self._dia.popleft()

    def restante_minuto(self) -> int:
        with self._lock:
            self._limpiar(self._reloj())
            return max(0, self.por_minuto - len(self._minuto))

    def restante_dia(self) -> int:
        with self._lock:
            self._limpiar(self._reloj())
            return max(0, self.por_dia - len(self._dia))

    def tomar(self, uso: str = "chat") -> bool:
        """Reserva una llamada para 'chat' o 'tarjeta'. False = usar la plantilla."""
        with self._lock:
            ahora = self._reloj()
            self._limpiar(ahora)
            if len(self._dia) >= self.por_dia:
                return False                                  # tope diario: modo reglas
            queda = self.por_minuto - len(self._minuto)
            if queda <= 0:
                return False
            if uso != "chat" and queda < RESERVA_CHAT * self.por_minuto:
                return False                                  # lo que queda es del chat
            self._minuto.append(ahora)
            self._dia.append(ahora)
            return True

    def agotado_dia(self) -> bool:
        return self.restante_dia() <= 0


PRESUPUESTO = Presupuesto()
CACHE_TARJETAS: dict = {}          # compartida por proceso (todas las sesiones)
_LOCK_CACHE = threading.Lock()


def cache_get(clave):
    with _LOCK_CACHE:
        return CACHE_TARJETAS.get(clave)


def cache_set(clave, valor) -> None:
    with _LOCK_CACHE:
        if len(CACHE_TARJETAS) > 5000:        # tope simple de memoria
            CACHE_TARJETAS.clear()
        CACHE_TARJETAS[clave] = valor


def cadena(modelo: str) -> list[dict]:
    """Proveedores disponibles en orden (sin claves en la salida)."""
    pasos = []
    if os.environ.get("GROQ_API_KEY"):
        pasos.append({"nombre": "groq", "url": URL_GROQ, "var": "GROQ_API_KEY",
                      "modelo": modelo})
    if os.environ.get("GEMINI_API_KEY"):
        pasos.append({"nombre": "gemini", "url": URL_GEMINI, "var": "GEMINI_API_KEY",
                      "modelo": config.LLM_MODELO_GEMINI})
    return pasos


def hay_api() -> bool:
    return bool(cadena(config.LLM_MODELO_CHAT))


def completar(mensajes: list[dict], modelo: str, uso: str = "chat", post=None,
              json_estricto: bool = False, presupuesto: Presupuesto | None = None,
              semaforo=None) -> tuple[str | None, str]:
    """Texto de la primera API que responda bien y el nombre del proveedor.

    Devuelve (None, 'reglas') si no hay claves, no hay cupo, el semáforo está lleno
    o todos fallan. Nunca lanza excepciones ni registra claves.
    """
    presupuesto = presupuesto or PRESUPUESTO
    semaforo = semaforo or SEMAFORO_LLM
    pasos = cadena(modelo)
    if not pasos or not presupuesto.tomar(uso):
        return None, "reglas"
    if not semaforo.acquire(blocking=False):
        return None, "reglas"
    post = post or requests.post
    try:
        for paso in pasos:
            cuerpo = {"model": paso["modelo"], "messages": mensajes, "temperature": 0.3}
            if json_estricto:
                cuerpo["response_format"] = {"type": "json_object"}
            try:
                r = post(paso["url"].rstrip("/") + "/chat/completions", json=cuerpo,
                         headers={"Authorization": "Bearer " + os.environ.get(paso["var"], "")},
                         timeout=TIMEOUT_API_S)
                codigo = getattr(r, "status_code", 200)
                if codigo != 200:          # 429, 5xx...: siguiente sin mostrar errores
                    log.info("Proveedor %s respondió %s: paso al siguiente", paso["nombre"],
                             codigo)
                    continue
                texto = r.json()["choices"][0]["message"]["content"]
                if texto and str(texto).strip():
                    return str(texto), paso["nombre"]
            except Exception as exc:       # red, timeout, JSON raro (sin la clave en el log)
                log.info("Proveedor %s falló (%s): paso al siguiente", paso["nombre"],
                         type(exc).__name__)
        return None, "reglas"
    finally:
        semaforo.release()


def estado() -> dict:
    """Para la barra lateral: proveedores configurados y cupo restante (sin claves)."""
    return {"cadena": [p["nombre"] for p in cadena(config.LLM_MODELO_CHAT)] + ["reglas"],
            "cupo_minuto": PRESUPUESTO.restante_minuto(), "max_minuto": PRESUPUESTO.por_minuto,
            "cupo_dia": PRESUPUESTO.restante_dia(), "max_dia": PRESUPUESTO.por_dia}
