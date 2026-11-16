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
from .herramientas import (DURACION_CORTA_MIN, _afinidad, ESQUEMAS, GENEROS_LIVIANOS, Herramientas,
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
- Ánimos y ocasiones también son filtros: 'para llorar' o 'triste' -> generos ["Drama"]
  y tags ["sad"]; 'en familia' -> generos ["Children","Animation"]; 'que haga pensar'
  -> tags ["thought-provoking"]; 'con giro final' -> tags ["twist ending"]; 'que dé
  miedo' -> generos ["Horror"]. Pásalos siempre a recomendar.
- Si el usuario nombra una película ('algo como X'), llama primero a buscar_pelicula.
  Si no está en el catálogo, dilo en una frase y recomienda igual con lo demás pedido.
- Si el usuario pide películas, termina SIEMPRE con al menos una recomendación.
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


# Pedidos sin palabras de película que igual piden una sugerencia ('sorpréndeme').
_RE_PIDE_EXTRA = re.compile(r"\b(?:sorprend\w*|surprise|algo|something|tonight|"
                            r"esta noche)\b")

AYUDA = ("¡Hola! Te recomiendo películas según tus gustos de MovieLens. Pídeme, por "
         "ejemplo, «algo corto y liviano», «una de terror para esta noche», «algo para "
         "llorar» o «algo como Toy Story».")


def analizar(mensaje: str, herramientas: Herramientas) -> dict:
    """Pedido completo: filtros para recomendar, si es un pedido y la frase del título.

    Devuelve {'generos', 'tags', 'max_duracion_min', 'n', 'pide', 'condiciones',
    'frase_titulo', 'referencia'}. Si el pedido nombra una película ('algo como X'),
    se busca con buscar_pelicula: si está, sus géneros completan el pedido (cuando no
    se pidió ninguno) y ella misma no se recomienda; si no está, queda una frase que
    lo dice y se recomienda igual con lo demás.
    """
    p = interpretar_pedido(mensaje, datos=herramientas.datos)
    filtros = interpretar(mensaje)
    a = {"generos": filtros["generos"], "tags": list(p["tags_genome"]),
         "max_duracion_min": filtros["max_duracion_min"], "n": filtros["n"],
         "pide": bool(p["pide_recomendacion"]
                      or _RE_PIDE_EXTRA.search(_normalizar(mensaje))),
         "condiciones": list(p["condiciones"]), "frase_titulo": "", "referencia": None}
    titulo = p.get("titulo_mencionado")
    if titulo:
        r = herramientas.buscar_pelicula(titulo)
        if r.get("encontrada"):
            a["referencia"] = r["movieId"]
            if not a["generos"]:
                a["generos"] = list(r.get("generos") or [])
            a["frase_titulo"] = (f"Tomé «{r['titulo']}» como referencia y busqué películas "
                                 "parecidas.")
        else:
            a["frase_titulo"] = (f"No encontré «{titulo}» en el catálogo, así que te "
                                 "recomiendo según lo demás que pediste.")
            # Igual se aprovecha lo que describa X (géneros, tags, duración).
            px = interpretar_pedido(titulo, datos=herramientas.datos)
            a["generos"] += [g for g in px["generos"] if g not in a["generos"]]
            a["tags"] += [t for t in px["tags_genome"] if t not in a["tags"]]
            if not a["max_duracion_min"] and px["max_duracion_min"]:
                a["max_duracion_min"] = px["max_duracion_min"]
    return a


def recomendar_detectado(userId: int, herramientas: Herramientas, a: dict) -> dict:
    """Llama a ``recomendar`` con lo detectado (sin la película de referencia)."""
    res = herramientas.recomendar(userId, n=a["n"] + (1 if a["referencia"] else 0),
                                  max_duracion_min=a["max_duracion_min"],
                                  generos=a["generos"], tags=a["tags"], excluir_vistas=True)
    recs = [r for r in res.get("recomendaciones", []) if r["movieId"] != a["referencia"]]
    for i, r in enumerate(recs[:a["n"]], start=1):
        r["rank"] = i
    return {**res, "recomendaciones": recs[:a["n"]]}


def completar_args(args: dict, a: dict) -> dict:
    """Completa los filtros que el modelo omitió al llamar a ``recomendar``."""
    args = dict(args)
    if not args.get("generos") and a["generos"]:
        args["generos"] = a["generos"]
    if not args.get("tags") and a["tags"]:
        args["tags"] = a["tags"]
    if not args.get("max_duracion_min") and a["max_duracion_min"]:
        args["max_duracion_min"] = a["max_duracion_min"]
    if not args.get("n"):
        args["n"] = a["n"]
    return args


def resumen_condiciones(r: dict) -> str:
    """Qué condiciones del pedido cumple la película, cuáles no y cuáles no se evaluaron."""
    if not r.get("condiciones_pedidas"):
        return "Sin condiciones en el pedido: ordenada por tu afinidad ALS."
    partes = []
    if r.get("cumple"):
        partes.append("Cumple: " + "; ".join(r["cumple"]) + ".")
    if r.get("no_cumple"):
        partes.append("No cumple: " + "; ".join(r["no_cumple"]) + ".")
    if r.get("no_evaluables"):
        partes.append("Sin datos para evaluar: " + "; ".join(r["no_evaluables"]) + ".")
    return " ".join(partes)


def _con_condiciones(r: dict) -> dict:
    return {**r, "condiciones_texto": resumen_condiciones(r)}


# --------------------------------------------------------------------------- #
# Explicaciones con datos (las usan ambos proveedores)
# --------------------------------------------------------------------------- #

def _lista_es(items: list[str]) -> str:
    items = [x for x in items if x]
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " y " + items[-1] \
        if items else ""


def datos_de_respaldo(peli: dict, perfil: dict, calificadas: list[dict],
                      usadas: set | None = None) -> str:
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
    # Película de referencia: la mejor calificada que comparta géneros y que no se haya
    # usado ya en otra tarjeta de la misma respuesta (``usadas`` se comparte entre ellas).
    usadas = usadas if usadas is not None else set()
    parecidas = [c for c in calificadas if c["rating"] >= 3.5
                 and set(c.get("generos", [])) & set(peli.get("generos", []))]
    parecida = next((c for c in parecidas if c.get("movieId") not in usadas), None)
    if parecida:
        usadas.add(parecida.get("movieId"))
        comun = [perfiles.GENEROS_ES.get(g, g) for g in parecida["generos"]
                 if g in peli.get("generos", [])]
        partes.append(f"Le diste {parecida['rating']:g}★ a {parecida['titulo']}, "
                      f"que también es {_lista_es(comun[:2])}.")
    afinidad = peli.get("afinidad") or _afinidad(peli.get("score"))
    if afinidad:
        partes.append(f"Según tu historial, {afinidad} contigo.")
    return " ".join(partes)


# --------------------------------------------------------------------------- #
# Proveedor 'reglas' (determinista, sin red)
# --------------------------------------------------------------------------- #

def responder_reglas(mensaje: str, userId: int, herramientas: Herramientas,
                     aviso: str | None = None) -> Respuesta:
    """Mismo mensaje y mismos datos -> misma respuesta.

    Un mensaje que no es un pedido (saludo, pregunta general) recibe la ayuda y
    ninguna tarjeta; un pedido termina siempre con recomendaciones del catálogo.
    """
    a = analizar(mensaje, herramientas)
    if not a["pide"]:
        return Respuesta(texto=AYUDA, proveedor="reglas", aviso=aviso)
    perfil = herramientas.perfil_gustos(userId)
    calificadas = herramientas.peliculas_calificadas(userId, n=200).get("peliculas", [])
    refs: set = set()   # películas de referencia ya usadas (no se repiten)
    res = recomendar_detectado(userId, herramientas, a)
    usadas = ["perfil_gustos", "peliculas_calificadas", "recomendar"]
    if a["referencia"] is not None or a["frase_titulo"]:
        usadas.append("buscar_pelicula")
    recs = [_con_condiciones({**r, "explicacion": datos_de_respaldo(r, perfil, calificadas, refs)})
            for r in res["recomendaciones"]]
    texto = _texto_reglas(a, perfil, recs, res.get("avisos"))
    return Respuesta(texto=texto, recomendaciones=recs, proveedor="reglas", aviso=aviso,
                     herramientas_usadas=usadas)


def _texto_reglas(a: dict, perfil: dict, recs: list[dict], avisos) -> str:
    """Frase que acompaña las tarjetas del modo reglas (y de la red de seguridad)."""
    pedido = []
    if a["max_duracion_min"]:
        pedido.append(f"de {a['max_duracion_min']} min o menos")
    if a["generos"]:
        pedido.append("de " + _lista_es([perfiles.GENEROS_ES.get(g, g)
                                         for g in a["generos"] if g != "Family"]))
    tipo = f" Eres «{perfil['tipo']}»." if perfil.get("tipo") else ""
    texto = (a["frase_titulo"] + " ") if a["frase_titulo"] else ""
    if recs:
        texto += (f"Busqué películas que no has visto{' ' + ', '.join(pedido) if pedido else ''}"
                  f".{tipo} Encontré {len(recs)}:")
    else:  # solo ocurre si el usuario ya vio todo el catálogo
        texto += f"Ya calificaste todas las películas del catálogo.{tipo}"
    if avisos:
        texto += " (" + "; ".join(avisos) + ")"
    return texto


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
    """Bucle de tool calling limitado. Lanza excepción si Ollama no responde.

    La interpretación determinista del pedido se aplica siempre: los filtros que el
    modelo omite al llamar a ``recomendar`` se completan con lo detectado, y si se
    pidieron películas pero ninguna propuesta del modelo es válida, la red de
    seguridad llama a ``recomendar`` con lo detectado y muestra esas tarjetas.
    """
    cliente = cliente or crear_cliente()
    a = analizar(mensaje, herramientas)
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
            if nombre not in ("info_pelicula", "buscar_pelicula"):
                args["userId"] = userId          # el chat es siempre del usuario activo
            if nombre == "recomendar":
                args = completar_args(args, a)   # filtros que el modelo omitió
            resultado = herramientas.ejecutar(nombre, args)
            usadas.append(nombre)
            if nombre == "recomendar":
                for r in resultado.get("recomendaciones", []):
                    validas[int(r["movieId"])] = r
            mensajes.append({"role": "tool", "content": json.dumps(resultado,
                                                                   ensure_ascii=False,
                                                                   default=str)})
    respuesta = _validar(_limpiar_thinking(contenido), validas, usadas, userId, herramientas)
    if a["frase_titulo"] and a["frase_titulo"] not in respuesta.texto:
        respuesta.texto = a["frase_titulo"] + " " + respuesta.texto
    if not a["pide"]:
        if not respuesta.recomendaciones and not _extraer_json(_limpiar_thinking(contenido)):
            respuesta.texto = AYUDA        # saludo o pregunta general: sin tarjetas
        return respuesta
    if not respuesta.recomendaciones:
        _red_de_seguridad(respuesta, userId, herramientas, a)
    return respuesta


def _red_de_seguridad(respuesta: Respuesta, userId: int, herramientas: Herramientas,
                      a: dict) -> None:
    """Se pidieron películas y no quedó ninguna válida: recomendar con lo detectado."""
    perfil = herramientas.perfil_gustos(userId)
    calificadas = herramientas.peliculas_calificadas(userId, n=200).get("peliculas", [])
    refs: set = set()   # películas de referencia ya usadas (no se repiten)
    res = recomendar_detectado(userId, herramientas, a)
    respuesta.herramientas_usadas.append("recomendar")
    respuesta.recomendaciones = [
        _con_condiciones({**r, "explicacion": datos_de_respaldo(r, perfil, calificadas, refs)})
        for r in res["recomendaciones"]]
    if respuesta.recomendaciones:
        respuesta.texto = (respuesta.texto + " Te dejo las del recomendador directamente:"
                           ).strip()
        # Transparencia: la leyenda «Proveedor: …» indica que las tarjetas no vienen
        # del LLM sino de recomendar con el pedido interpretado.
        respuesta.proveedor = PROVEEDOR_COMPLETADO


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
    refs: set = set()   # películas de referencia ya usadas (no se repiten)
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
    recs, vistos, refs = [], set(), set()
    for mid, explicacion in propuestas:
        if mid in vistos:
            continue
        vistos.add(mid)
        ficha = validas[mid]
        evidencia = datos_de_respaldo(ficha, perfil, calificadas, refs)
        recs.append(_con_condiciones({**ficha, "explicacion": (
            explicacion + " — " if explicacion else "") + evidencia}))
    # Sin recomendaciones válidas el texto queda vacío: la red de seguridad lo completa.
    texto = intro or ("Esto encontré con tus datos:" if recs else "")
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
    # La red de seguridad (pedido sin recomendaciones válidas) vive en responder_ollama.
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


__all__ = ["responder", "responder_reglas", "responder_ollama", "interpretar", "analizar",
           "resumen_condiciones", "AYUDA",
           "Respuesta", "AVISO_REGLAS", "MAX_RONDAS_HERRAMIENTAS", "descripcion_config",
           "estado_ollama", "PROVEEDOR_COMPLETADO"]
