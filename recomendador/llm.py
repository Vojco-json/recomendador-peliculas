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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field

from . import config, perfiles
from .herramientas import (DURACION_CORTA_MIN, _afinidad, ESQUEMAS, GENEROS_LIVIANOS, Herramientas,
                           N_DEFECTO)
from .pedido import interpretar_pedido

log = logging.getLogger(__name__)

AVISO_REGLAS = "Ollama no responde: usando modo reglas"
MAX_RONDAS_HERRAMIENTAS = 4      # bucle de herramientas limitado
TIMEOUT_OLLAMA_S = 20         # tope de la conexión HTTP con Ollama
LIMITE_RESPUESTA_S = 20       # tope total: si Ollama tarda más, plantilla de reglas
ENTENDI_NO_PEDIDO = "Entendí: no es un pedido de películas."

SISTEMA = """Eres el redactor de un recomendador de películas para el usuario {userId}.
Tu ÚNICO trabajo es escribir el texto de la respuesta. Reglas obligatorias:
- El código ya interpretó el pedido (géneros, duración, ánimos y ocasiones, y la
  película nombrada, buscada con buscar_pelicula) y ya eligió las películas. No
  cambies filtros, no agregues ni quites películas y no inventes datos.
- Usa solo los DATOS que recibes: condiciones que cumple cada película (cumple /
  no_cumple / sin_datos), la película parecida que el usuario calificó y la afinidad
  (escríbela como texto, nunca como número ni estrellas).
- Si DATOS trae aviso_titulo (la película nombrada no está), dilo en una frase.
- Responde en español y entrega SOLO un JSON con esta forma:
  {{"intro": "frase breve", "recomendaciones": [{{"movieId": 123, "explicacion": "..."}}]}}
  con los mismos movieId de DATOS y en el mismo orden.
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
    filtros: dict = field(default_factory=dict)                # filtros finales (pedido.py)


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
        return Respuesta(texto=AYUDA + "\n\n" + ENTENDI_NO_PEDIDO, proveedor="reglas",
                         aviso=aviso)
    perfil, _, recs, avisos = _recomendaciones_del_pedido(a, userId, herramientas)
    texto = _texto_reglas(a, perfil, recs, avisos) + "\n\n" + entendi(a)
    return Respuesta(texto=texto, recomendaciones=recs, proveedor="reglas", aviso=aviso,
                     herramientas_usadas=_usadas(a), filtros=filtros_finales(a))


def _usadas(a: dict) -> list[str]:
    """Herramientas que el código usó para armar la respuesta."""
    usadas = ["perfil_gustos", "peliculas_calificadas", "recomendar"]
    if a["referencia"] is not None or a["frase_titulo"]:
        usadas.append("buscar_pelicula")
    return usadas


def entendi(a: dict) -> str:
    """Línea «Entendí: …» con las condiciones finales aplicadas (salen de pedido.py)."""
    partes = []
    generos = [perfiles.GENEROS_ES.get(g, g) for g in a["generos"] if g != "Family"]
    if generos:
        partes.append(("género " if len(generos) == 1 else "géneros ") + _lista_es(generos))
    if a["tags"]:
        partes.append("tags " + ", ".join(f"«{t}»" for t in a["tags"]))
    if a["max_duracion_min"]:
        partes.append(f"duración de {a['max_duracion_min']} min o menos")
    if a.get("referencia") is not None:
        partes.append("parecidas a la película que nombraste")
    return "Entendí: " + ("; ".join(partes) if partes
                          else "sin condiciones; ordené por tus gustos") + "."


def filtros_finales(a: dict) -> dict:
    """Filtros que recibe ``recomendar``: salen SOLO de la interpretación del pedido."""
    return {"generos": list(a["generos"]), "tags": list(a["tags"]),
            "max_duracion_min": a["max_duracion_min"], "n": a["n"]}


def _recomendaciones_del_pedido(a: dict, userId: int, herramientas: Herramientas):
    """Lista final, la misma en ambos modos: recomendar con lo que entendió pedido.py."""
    perfil = herramientas.perfil_gustos(userId)
    calificadas = herramientas.peliculas_calificadas(userId, n=200).get("peliculas", [])
    refs: set = set()   # películas de referencia ya usadas (no se repiten)
    res = recomendar_detectado(userId, herramientas, a)
    recs = [_con_condiciones({**r, "explicacion": datos_de_respaldo(r, perfil, calificadas,
                                                                     refs)})
            for r in res["recomendaciones"]]
    return perfil, calificadas, recs, res.get("avisos")


def datos_para_redactar(a: dict, recs: list[dict], calificadas: list[dict]) -> dict:
    """Lo que recibe el LLM: filtros finales y cada recomendación con sus datos."""
    items = []
    for r in recs:
        parecida = next((c for c in calificadas if c["rating"] >= 3.5
                         and set(c.get("generos", [])) & set(r.get("generos", []))), None)
        items.append({
            "movieId": r["movieId"], "titulo": r["titulo"], "anio": r.get("anio"),
            "generos": r.get("generos", []), "duracion_min": r.get("duracion_min"),
            "cumple": r.get("cumple", []), "no_cumple": r.get("no_cumple", []),
            "sin_datos": r.get("no_evaluables", []),
            "pelicula_parecida_calificada": ({"titulo": parecida["titulo"],
                                              "rating": parecida["rating"]}
                                             if parecida else None),
            "afinidad": r.get("afinidad") or _afinidad(r.get("score")),
            "evidencia": r.get("explicacion", "")})
    return {"entendi": entendi(a), "filtros": filtros_finales(a),
            "aviso_titulo": a["frase_titulo"], "recomendaciones": items}


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
    """El LLM SOLO redacta. Lanza excepción si Ollama falla o tarda más de 20 s.

    1. pedido.py interpreta el mensaje: es la única fuente de los filtros.
    2. ``recomendar`` arma la lista final (la misma del modo reglas).
    3. El modelo recibe esa lista con sus datos (condiciones que cumple, película
       parecida que el usuario calificó, afinidad) y escribe solo el texto. No recibe
       herramientas: no puede elegir filtros ni películas. Lo que nombre fuera de la
       lista se descarta; si no entrega una redacción útil, se usa la plantilla de reglas.
    """
    cliente = cliente or crear_cliente()
    a = analizar(mensaje, herramientas)
    if not a["pide"]:   # saludo o pregunta general: ayuda, sin tarjetas
        return Respuesta(texto=AYUDA + "\n\n" + ENTENDI_NO_PEDIDO, proveedor="ollama")
    perfil, calificadas, recs, avisos = _recomendaciones_del_pedido(a, userId, herramientas)
    datos = datos_para_redactar(a, recs, calificadas)
    mensajes = [{"role": "system", "content": SISTEMA.format(userId=userId)}]
    mensajes += [{"role": h["role"], "content": h["content"]} for h in (historial or [])
                 if h.get("role") in ("user", "assistant")][-6:]
    mensajes.append({"role": "user", "content": f"Pedido del usuario: {mensaje}\nDATOS: "
                     + json.dumps(datos, ensure_ascii=False, default=str)})
    resp = _llamar_con_limite(cliente, LIMITE_RESPUESTA_S, model=config.LLM_MODEL,
                              messages=mensajes, tools=None, think=config.LLM_THINK)
    # 'thinking' se ignora siempre; tool_calls también (el modelo no tiene herramientas).
    contenido = _limpiar_thinking(_campo(_campo(resp, "message", {}), "content", "") or "")
    intro, textos, descartadas = _leer_redaccion(contenido, {r["movieId"] for r in recs})
    finales = [{**r, "explicacion": textos[r["movieId"]] + " — " + r["explicacion"]}
               if textos.get(r["movieId"]) else r for r in recs]
    proveedor = "ollama"
    if not intro and not textos:      # sin redacción útil: plantilla del modo reglas
        intro = _texto_reglas(a, perfil, finales, avisos)
        proveedor = PROVEEDOR_COMPLETADO
    else:
        intro = intro or "Esto encontré con tus datos:"
        if a["frase_titulo"] and a["frase_titulo"] not in intro:
            intro = a["frase_titulo"] + " " + intro
    if descartadas:
        intro += f" (Descarté {len(descartadas)} película(s) que no estaban en la lista.)"
    return Respuesta(texto=intro + "\n\n" + datos["entendi"], recomendaciones=finales,
                     proveedor=proveedor, herramientas_usadas=_usadas(a),
                     descartadas=descartadas, filtros=datos["filtros"])


def _llamar_con_limite(cliente, limite_s: float, **kw):
    """cliente.chat con tope de tiempo total; si se pasa, lanza TimeoutError."""
    ejecutor = ThreadPoolExecutor(max_workers=1)
    futuro = ejecutor.submit(cliente.chat, **kw)
    try:
        return futuro.result(timeout=limite_s)
    except FuturesTimeout as exc:
        raise TimeoutError(f"Ollama tardó más de {limite_s:g} s") from exc
    finally:
        ejecutor.shutdown(wait=False)


def _leer_redaccion(contenido: str, ids_validos: set) -> tuple[str, dict, list]:
    """Del JSON del modelo toma la intro y el texto de cada película de la lista final.

    Sin JSON no se usa el texto libre (podría nombrar películas inventadas).
    """
    dato = _extraer_json(contenido)
    if dato is None:
        return "", {}, []
    intro = str(dato.get("intro") or "").strip()
    textos, descartadas = {}, []
    for item in dato.get("recomendaciones") or []:
        try:
            mid = int(item.get("movieId"))
        except (TypeError, ValueError, AttributeError):
            descartadas.append(item)
            continue
        if mid in ids_validos:
            textos[mid] = str(item.get("explicacion") or "").strip()
        else:
            descartadas.append(item.get("titulo") or mid)
    return intro, textos, descartadas


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
