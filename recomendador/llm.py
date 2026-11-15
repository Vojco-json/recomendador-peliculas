"""Chat de recomendaciones: LLM con herramientas (Ollama) o proveedor determinista 'reglas'.

Reglas de oro
-------------
1. Solo se muestran películas que devolvió la herramienta ``recomendar`` en ESTA
   respuesta; cualquier otra (inventada) se elimina y se cuenta en ``descartadas``.
2. Cada recomendación lleva una explicación con datos del usuario (perfil, películas
   que calificó) y de la película (géneros, duración, nota IMDb, puntaje ALS).
3. El razonamiento (thinking) está desactivado por defecto (``think=config.LLM_THINK``)
   y su contenido nunca se muestra, ni siquiera con LLM_THINK=true.
4. Si Ollama no responde, se usa el proveedor 'reglas' con el aviso
   ``AVISO_REGLAS``.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

from . import config, perfiles
from .herramientas import (DURACION_CORTA_MIN, ESQUEMAS, GENEROS_LIVIANOS, Herramientas,
                           N_DEFECTO)
from .pedido import interpretar_pedido

log = logging.getLogger(__name__)

AVISO_REGLAS = "Ollama no responde: usando modo reglas"
MAX_RONDAS_HERRAMIENTAS = 4      # bucle de herramientas limitado
TIMEOUT_OLLAMA_S = 120

SISTEMA = """Eres un recomendador de películas para el usuario {userId} de MovieLens.
Reglas obligatorias:
- No inventes datos. Usa las herramientas para ver su perfil (perfil_gustos), sus
  películas calificadas (peliculas_calificadas), sus tags y recomendaciones (recomendar).
- SOLO puedes recomendar películas devueltas por la herramienta recomendar.
- 'corto' significa max_duracion_min=100; 'liviano' significa generos
  ["Comedy","Animation","Children"].
- Explica cada recomendación con datos concretos del usuario y de la película.
- Responde en español y al final entrega SOLO un JSON con esta forma:
  {{"intro": "frase breve", "recomendaciones": [{{"movieId": 123, "explicacion": "..."}}]}}
"""


@dataclass
class Respuesta:
    """Lo que la app muestra en el chat."""
    texto: str
    recomendaciones: list[dict] = field(default_factory=list)  # ficha + 'explicacion'
    proveedor: str = "reglas"
    aviso: str | None = None
    herramientas_usadas: list[str] = field(default_factory=list)
    descartadas: list = field(default_factory=list)            # películas inventadas


# --------------------------------------------------------------------------- #
# Interpretación del pedido (determinista)
# --------------------------------------------------------------------------- #

def _normalizar(texto: str) -> str:
    texto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in texto if not unicodedata.combining(c))


# Palabras clave en español (sin tildes) -> género de MovieLens.
PALABRAS_GENERO = {
    "accion": "Action", "aventura": "Adventure", "animacion": "Animation",
    "animada": "Animation", "comedia": "Comedy", "crimen": "Crime", "policial": "Crime",
    "documental": "Documentary", "drama": "Drama", "fantasia": "Fantasy",
    "terror": "Horror", "miedo": "Horror", "musical": "Musical", "misterio": "Mystery",
    "romance": "Romance", "romantica": "Romance", "ciencia ficcion": "Sci-Fi",
    "suspenso": "Thriller", "thriller": "Thriller", "guerra": "War", "western": "Western",
    "infantil": "Children", "ninos": "Children", "familia": "Children",
}


def interpretar(mensaje: str) -> dict:
    """Filtros a partir del texto: {'max_duracion_min', 'generos', 'n', 'pide'}.

    'corto' -> <= 100 min; 'liviano/ligero' -> Comedy/Animation/Family(Children);
    'menos de 90 min' -> 90; 'dame 3' -> n=3.
    """
    # Delega en pedido.py (ES/EN, errores de tipeo, ánimos) y conserva la forma de salida.
    p = interpretar_pedido(mensaje)
    generos = list(p["generos"])
    if "liviano" in p["animos"]:      # 'liviano' en el chat sigue siendo GENEROS_LIVIANOS
        generos += [g for g in GENEROS_LIVIANOS if g not in generos]
    return {"max_duracion_min": p["max_duracion_min"], "generos": generos,
            "n": p["n"] if p["n"] else N_DEFECTO}


# --------------------------------------------------------------------------- #
# Explicaciones con datos (las usan ambos proveedores)
# --------------------------------------------------------------------------- #

def _lista_es(items: list[str]) -> str:
    items = [x for x in items if x]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1] \
        if items else ""


def datos_de_respaldo(peli: dict, perfil: dict, calificadas: list[dict]) -> str:
    """Línea con la evidencia (datos de herramientas) de por qué encaja la película."""
    partes = []
    gen_es = [perfiles.GENEROS_ES.get(g, g) for g in peli.get("generos", [])]
    ficha = [f"{peli['titulo']}" + (f" ({peli['anio']})" if peli.get("anio") else "")]
    if peli.get("duracion_min"):
        ficha.append(f"{peli['duracion_min']} min")
    if gen_es:
        ficha.append(_lista_es(gen_es))
    if peli.get("imdb_rating"):
        ficha.append(f"IMDb {peli['imdb_rating']:.1f}")
    partes.append(" · ".join(ficha) + ".")
    comunes = [perfiles.GENEROS_ES.get(g, g) for g in perfil.get("top_generos", [])
               if g in peli.get("generos", [])]
    if comunes:
        partes.append(f"Comparte {_lista_es(comunes)} con los géneros que más te gustan.")
    elif perfil.get("top_generos_es"):
        partes.append(f"Tu perfil ({perfil.get('tipo') or 'sin tipo'}) se inclina por "
                      f"{_lista_es(perfil['top_generos_es'][:2])}; esta te da un cambio "
                      "de tono dentro de lo que pediste.")
    parecida = next((c for c in calificadas if c["rating"] >= 3.5
                     and set(c.get("generos", [])) & set(peli.get("generos", []))), None)
    if parecida:
        comun = [perfiles.GENEROS_ES.get(g, g) for g in parecida["generos"]
                 if g in peli.get("generos", [])]
        partes.append(f"Le diste {parecida['rating']:g}★ a {parecida['titulo']}, "
                      f"que también es {_lista_es(comun[:2])}.")
    if peli.get("score") is not None:
        partes.append(f"Puntaje ALS para ti: {peli['score']:.2f}.")
    return " ".join(partes)


# --------------------------------------------------------------------------- #
# Proveedor 'reglas' (determinista, sin red)
# --------------------------------------------------------------------------- #

def responder_reglas(mensaje: str, userId: int, herramientas: Herramientas,
                     aviso: str | None = None) -> Respuesta:
    """Mismo mensaje y mismos datos -> misma respuesta."""
    filtros = interpretar(mensaje)
    perfil = herramientas.perfil_gustos(userId)
    calificadas = herramientas.peliculas_calificadas(userId, n=200).get("peliculas", [])
    res = herramientas.recomendar(userId, n=filtros["n"],
                                  max_duracion_min=filtros["max_duracion_min"],
                                  generos=filtros["generos"], excluir_vistas=True)
    usadas = ["perfil_gustos", "peliculas_calificadas", "recomendar"]
    recs = [{**r, "explicacion": datos_de_respaldo(r, perfil, calificadas)}
            for r in res["recomendaciones"]]

    pedido = []
    if filtros["max_duracion_min"]:
        pedido.append(f"de {filtros['max_duracion_min']} min o menos")
    if filtros["generos"]:
        pedido.append("de " + _lista_es([perfiles.GENEROS_ES.get(g, g)
                                         for g in filtros["generos"] if g != "Family"]))
    tipo = f" Eres «{perfil['tipo']}»." if perfil.get("tipo") else ""
    if recs:
        texto = (f"Busqué películas que no has visto{' ' + ', '.join(pedido) if pedido else ''}"
                 f".{tipo} Encontré {len(recs)}:")
    else:
        texto = (f"No encontré películas sin ver{' ' + ', '.join(pedido) if pedido else ''}."
                 f"{tipo} Prueba con menos filtros.")
    if res.get("avisos"):
        texto += " (" + "; ".join(res["avisos"]) + ")"
    return Respuesta(texto=texto, recomendaciones=recs, proveedor="reglas", aviso=aviso,
                     herramientas_usadas=usadas)


# --------------------------------------------------------------------------- #
# Proveedor 'ollama' (tool calling)
# --------------------------------------------------------------------------- #

def _campo(obj, nombre, defecto=None):
    """Lee un campo de un objeto pydantic de ollama o de un dict (cliente simulado)."""
    if obj is None:
        return defecto
    if isinstance(obj, dict):
        return obj.get(nombre, defecto)
    return getattr(obj, nombre, defecto)


def _limpiar_thinking(texto: str) -> str:
    """Quita bloques <think>…</think> que algunos modelos dejan en el contenido."""
    texto = re.sub(r"<think>.*?</think>", "", texto or "", flags=re.S | re.I)
    return re.sub(r"<think>.*", "", texto, flags=re.S | re.I).strip()


def _extraer_json(texto: str) -> dict | None:
    """Último objeto JSON con 'recomendaciones' dentro del texto (tolera ```json)."""
    for m in reversed(list(re.finditer(r"\{", texto))):
        try:
            dato, _ = json.JSONDecoder().raw_decode(texto[m.start():])
        except ValueError:
            continue
        if isinstance(dato, dict) and "recomendaciones" in dato:
            return dato
    return None


def crear_cliente():
    import ollama

    return ollama.Client(host=config.OLLAMA_HOST, timeout=TIMEOUT_OLLAMA_S)


def responder_ollama(mensaje: str, userId: int, herramientas: Herramientas,
                     historial: list[dict] | None = None, cliente=None) -> Respuesta:
    """Bucle de tool calling limitado. Lanza excepción si Ollama no responde."""
    cliente = cliente or crear_cliente()
    mensajes = [{"role": "system", "content": SISTEMA.format(userId=userId)}]
    mensajes += [{"role": h["role"], "content": h["content"]} for h in (historial or [])
                 if h.get("role") in ("user", "assistant")][-6:]
    mensajes.append({"role": "user", "content": f"(userId={userId}) {mensaje}"})

    validas: dict[int, dict] = {}      # movieId -> ficha devuelta por 'recomendar'
    usadas: list[str] = []
    contenido = ""
    for ronda in range(MAX_RONDAS_HERRAMIENTAS + 1):
        ultima = ronda == MAX_RONDAS_HERRAMIENTAS
        resp = cliente.chat(model=config.LLM_MODEL, messages=mensajes,
                            tools=None if ultima else ESQUEMAS, think=config.LLM_THINK)
        msg = _campo(resp, "message", {})
        contenido = _campo(msg, "content", "") or ""     # 'thinking' se ignora siempre
        llamadas = _campo(msg, "tool_calls") or []
        if not llamadas or ultima:
            break
        mensajes.append({"role": "assistant", "content": contenido,
                         "tool_calls": [_como_dict(c) for c in llamadas]})
        for llamada in llamadas:
            funcion = _campo(llamada, "function", {})
            nombre = _campo(funcion, "name", "")
            args = _campo(funcion, "arguments", {}) or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            args = dict(args)
            if nombre != "info_pelicula":
                args["userId"] = userId          # el chat es siempre del usuario activo
            resultado = herramientas.ejecutar(nombre, args)
            usadas.append(nombre)
            if nombre == "recomendar":
                for r in resultado.get("recomendaciones", []):
                    validas[int(r["movieId"])] = r
            mensajes.append({"role": "tool", "content": json.dumps(resultado,
                                                                   ensure_ascii=False,
                                                                   default=str)})
    return _validar(_limpiar_thinking(contenido), validas, usadas, userId, herramientas)


def _como_dict(llamada) -> dict:
    if isinstance(llamada, dict):
        return llamada
    try:
        return llamada.model_dump()
    except AttributeError:
        return {"function": {"name": _campo(_campo(llamada, "function"), "name"),
                             "arguments": _campo(_campo(llamada, "function"), "arguments")}}


def _validar(contenido: str, validas: dict[int, dict], usadas: list[str], userId: int,
             herramientas: Herramientas) -> Respuesta:
    """Deja solo películas de las herramientas y agrega la evidencia de cada una."""
    perfil = herramientas.perfil_gustos(userId)
    calificadas = herramientas.peliculas_calificadas(userId, n=200).get("peliculas", [])
    vistas = {c["movieId"] for c in calificadas}
    dato = _extraer_json(contenido)
    propuestas, descartadas = [], []
    if dato is not None:
        intro = str(dato.get("intro") or "").strip()
        for item in dato.get("recomendaciones") or []:
            try:
                mid = int(item.get("movieId"))
            except (TypeError, ValueError, AttributeError):
                descartadas.append(item)
                continue
            if mid in validas and mid not in vistas:
                propuestas.append((mid, str(item.get("explicacion") or "").strip()))
            else:
                descartadas.append(item.get("titulo") or mid)
    else:
        # Sin JSON: se rescatan solo títulos de las herramientas citados en el texto y no
        # se muestra el texto libre (podría nombrar películas inventadas).
        intro = ""
        t = _normalizar(contenido)
        propuestas = [(mid, "") for mid, r in validas.items()
                      if _normalizar(r["titulo"]) in t and mid not in vistas]
    recs, vistos = [], set()
    for mid, explicacion in propuestas:
        if mid in vistos:
            continue
        vistos.add(mid)
        ficha = validas[mid]
        evidencia = datos_de_respaldo(ficha, perfil, calificadas)
        recs.append({**ficha, "explicacion": (explicacion + " — " if explicacion else "")
                     + evidencia})
    texto = intro or ("Esto encontré con tus datos:" if recs else
                      "No pude obtener recomendaciones verificadas con las herramientas.")
    if descartadas:
        texto += f" (Descarté {len(descartadas)} película(s) que no venían de las herramientas.)"
    return Respuesta(texto=texto, recomendaciones=recs, proveedor="ollama",
                     herramientas_usadas=usadas, descartadas=descartadas)


# --------------------------------------------------------------------------- #
# Punto de entrada
# --------------------------------------------------------------------------- #

def responder(mensaje: str, userId: int, herramientas: Herramientas,
              historial: list[dict] | None = None, proveedor: str | None = None,
              cliente=None) -> Respuesta:
    """Responde con el proveedor configurado; si Ollama falla, cae a 'reglas' con aviso."""
    proveedor = (proveedor or config.LLM_PROVIDER).lower()
    if proveedor == "reglas":
        return responder_reglas(mensaje, userId, herramientas)
    try:
        respuesta = responder_ollama(mensaje, userId, herramientas, historial, cliente)
    except Exception as exc:  # conexión rechazada, modelo no descargado, timeout...
        log.warning("Ollama falló (%s: %s): modo reglas", type(exc).__name__, exc)
        return responder_reglas(mensaje, userId, herramientas, aviso=AVISO_REGLAS)
    if not respuesta.recomendaciones:
        # El LLM no entregó nada verificable: se completa con las reglas (sin inventar).
        respaldo = responder_reglas(mensaje, userId, herramientas)
        respuesta.recomendaciones = respaldo.recomendaciones
        if respaldo.recomendaciones:
            respuesta.texto += " Te dejo las del recomendador directamente:"
            # Transparencia: la leyenda «Proveedor: …» indica que las tarjetas vienen
            # del modo reglas y no del LLM.
            respuesta.proveedor = PROVEEDOR_COMPLETADO
    return respuesta


PROVEEDOR_COMPLETADO = "ollama (sin recomendaciones válidas; completado con reglas)"


def estado_ollama(cliente=None, timeout_s: float = 2.0) -> dict:
    """Chequeo rápido de Ollama para la barra lateral (no lanza excepciones).

    Devuelve ``{"ok": bool, "mensaje": str}``. ``ok`` es True solo si el servidor
    responde y el modelo configurado (LLM_MODEL) está descargado.
    """
    try:
        if cliente is None:
            import ollama

            cliente = ollama.Client(host=config.OLLAMA_HOST, timeout=timeout_s)
        lista = cliente.list()
    except Exception as exc:  # servidor apagado, host mal escrito, timeout...
        log.info("Ollama no disponible (%s: %s)", type(exc).__name__, exc)
        return {"ok": False, "mensaje": AVISO_REGLAS}
    modelos = _campo(lista, "models", []) or []
    nombres = {str(_campo(m, "model") or _campo(m, "name") or "") for m in modelos}
    modelo = config.LLM_MODEL
    if modelo in nombres or f"{modelo}:latest" in nombres:
        return {"ok": True, "mensaje": f"Ollama listo ({modelo})"}
    return {"ok": False, "mensaje": f"Ollama responde, pero falta el modelo {modelo} "
                                    f"(ollama pull {modelo}): usando modo reglas"}


def descripcion_config() -> dict:
    """Configuración visible en la barra lateral."""
    return {"LLM_PROVIDER": config.LLM_PROVIDER, "LLM_MODEL": config.LLM_MODEL,
            "OLLAMA_HOST": config.OLLAMA_HOST, "think": config.LLM_THINK}


__all__ = ["responder", "responder_reglas", "responder_ollama", "interpretar",
           "Respuesta", "AVISO_REGLAS", "MAX_RONDAS_HERRAMIENTAS", "descripcion_config",
           "estado_ollama", "PROVEEDOR_COMPLETADO"]
