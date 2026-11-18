"""Chat conversacional con contexto, apoyado siempre en los datos del sistema.

Flujo de cada turno
-------------------
1. Interpretar: el modelo del chat recibe los últimos 8 mensajes y el contexto del
   usuario y devuelve un JSON estricto con la intención y los cambios de estado.
   Sin API o sin cupo se usan reglas (pedido.py + patrones para 'otra', 'pero',
   'la segunda', 'ya la vi').
2. Validar y aplicar (código): vocabulario cerrado. Géneros de MovieLens, tags de
   genome-tags, personas de personas.parquet; películas solo las ya mostradas (por
   número o título) o las que encuentre buscar_pelicula. Lo inválido se ignora.
3. Recomendar: SOLO Herramientas.recomendar con las condiciones del estado,
   excluyendo mostradas y descartadas. El LLM nunca elige películas.
4. Redactar: el modelo escribe en tono conversacional con ese contexto. Todo título
   citado debe estar en el contexto; si no, se usa la plantilla.

El estado vive solo en st.session_state (RAM): se pierde al recargar, al cerrar
sesión o con «Nueva conversación».
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import config, perfiles, proveedores
from .herramientas import N_DEFECTO, Herramientas, _afinidad
from .llm import (AYUDA, _con_condiciones, _extraer_json, _lista_es, _normalizar,
                  datos_de_respaldo, entendi)
from .pedido import interpretar_pedido

INTENCIONES = ("nuevo_pedido", "ajuste", "mas", "descartar", "pregunta_pelicula",
               "pregunta_perfil", "charla")
N_MENSAJES = 8
GENEROS_VALIDOS = set(perfiles.GENEROS) - set(perfiles.GENEROS_NO_NOMBRABLES)
ORDINALES = {"primera": 1, "primero": 1, "1": 1, "segunda": 2, "segundo": 2, "2": 2,
             "tercera": 3, "tercero": 3, "3": 3, "cuarta": 4, "4": 4, "quinta": 5, "5": 5,
             "ultima": -1, "ultimo": -1}

SISTEMA_INTERPRETAR = """Interpretas mensajes de un chat de recomendación de películas.
Devuelve SOLO un JSON con esta forma exacta:
{"intencion": "nuevo_pedido|ajuste|mas|descartar|pregunta_pelicula|pregunta_perfil|charla",
 "generos": [], "tags": [], "personas": [], "max_duracion_min": null,
 "quitar": [], "pelicula": null}
- nuevo_pedido: pide otra cosa (reemplaza las condiciones). ajuste: agrega o cambia una
  condición y mantiene el resto. mas: quiere más opciones con lo mismo. descartar: ya la
  vio o no le interesa una película mostrada. pregunta_pelicula: pregunta por una película.
  pregunta_perfil: pregunta por sus gustos. charla: cualquier otra cosa.
- generos: en inglés de MovieLens (Drama, Crime, Comedy, Sci-Fi...). tags: en inglés,
  de genome (twist ending, dark, feel-good...). personas: nombres de directores o actores.
- quitar: condiciones que pide sacar. pelicula: número de la película mostrada (1, 2...)
  o su título. No inventes nada fuera del mensaje."""

SISTEMA_RESPONDER = """Eres el asistente de MoviePredict y conversas en español de Chile,
en tono cercano y breve (2 a 4 frases). Usa SOLO los DATOS que recibes (perfil, sus
películas mejor calificadas, las películas mostradas con sus condiciones y explicación,
y la ficha si la hay). Si falta un dato, dilo. No inventes títulos, personas ni cifras.
Si citas una película, escribe su título exacto entre «». No uses estrellas.
Las tarjetas con las películas se muestran aparte: no las enumeres todas."""


@dataclass
class Turno:
    """Resultado de un turno: lo que pinta la pestaña Chat."""
    texto: str
    intencion: str
    recomendaciones: list[dict] = field(default_factory=list)
    proveedor: str = "reglas"
    entendi: str = ""


def estado_nuevo() -> dict:
    """Estado de la conversación (se guarda en st.session_state, nunca en disco)."""
    return {"generos": [], "tags": [], "max_duracion_min": None, "personas": [],
            "referencia": None, "mostradas": [], "descartadas": [], "mensajes": []}


# --------------------------------------------------------------------------- #
# Vocabulario cerrado
# --------------------------------------------------------------------------- #

_TAGS_CACHE: dict[str, set] = {}


def tags_validos(herr: Herramientas) -> set[str]:
    """Tags de genome-tags.csv (vacío si la carpeta no trae genome)."""
    ruta = Path(herr.datos) / "genome-tags.csv"
    if str(ruta) not in _TAGS_CACHE:
        tags: set[str] = set()
        if ruta.exists():
            import pandas as pd
            tags = set(pd.read_csv(ruta)["tag"].astype(str).str.lower())
        _TAGS_CACHE[str(ruta)] = tags
    return _TAGS_CACHE[str(ruta)]


def _persona_valida(nombre: str, herr: Herramientas) -> dict | None:
    """Resuelve un nombre contra personas.parquet con el mismo código de pedido.py."""
    ruta = herr.artefactos / "app" / "personas.parquet"
    p = interpretar_pedido(f"algo con {nombre}", datos=herr.datos, personas=ruta)
    return p["personas"][0] if p.get("personas") else None


def _pelicula_ref(valor, estado: dict, herr: Herramientas) -> int | None:
    """Película referida: número o título de una mostrada, o buscar_pelicula."""
    if valor is None or valor == "":
        return None
    mostradas = estado["mostradas"]
    texto = _normalizar(str(valor)).strip()
    if texto.lstrip("#").isdigit() or texto in ORDINALES:
        i = ORDINALES.get(texto) or int(texto.lstrip("#"))
        if mostradas and (i == -1 or 1 <= i <= len(mostradas)):
            return mostradas[i - 1 if i > 0 else -1]["movieId"]
        return None
    for m in mostradas:
        if texto and (texto in _normalizar(m["titulo"])):
            return m["movieId"]
    r = herr.buscar_pelicula(str(valor))
    return int(r["movieId"]) if r.get("encontrada") else None


def validar(propuesta: dict, estado: dict, herr: Herramientas) -> dict:
    """Deja solo lo que existe en el vocabulario cerrado; lo inválido se ignora."""
    if not isinstance(propuesta, dict):
        return {"intencion": "charla"}
    intencion = propuesta.get("intencion")
    intencion = intencion if intencion in INTENCIONES else "charla"
    tags = tags_validos(herr)

    def lista(x):
        return [str(v) for v in x] if isinstance(x, list) else []

    generos = [g for g in lista(propuesta.get("generos")) if g in GENEROS_VALIDOS]
    tags_ok = [t.lower() for t in lista(propuesta.get("tags")) if t.lower() in tags]
    personas = [p for p in (_persona_valida(n, herr) for n in lista(propuesta.get("personas")))
                if p]
    dur = propuesta.get("max_duracion_min")
    dur = int(dur) if isinstance(dur, (int, float)) and 30 <= dur <= 400 else None
    quitar = lista(propuesta.get("quitar"))
    pelicula = _pelicula_ref(propuesta.get("pelicula"), estado, herr)
    return {"intencion": intencion, "generos": generos, "tags": tags_ok,
            "personas": personas, "max_duracion_min": dur, "quitar": quitar,
            "pelicula": pelicula}


# --------------------------------------------------------------------------- #
# Interpretación con reglas (sin API o sin cupo)
# --------------------------------------------------------------------------- #

_RE_DESCARTAR = re.compile(r"\b(ya la vi|ya lo vi|ya las vi|la vi|no me gusto|no me gusta|"
                           r"descart\w*|sacala|saca(?:la)?|no me interesa)\b")
_RE_MAS = re.compile(r"\b(otra|otras|otro|otros|mas|more|another)\b")
_RE_AJUSTE = re.compile(r"(^|\s)(pero|y que|que sea|que tenga|mejor que)\b")
_RE_PERFIL = re.compile(r"\b(mi perfil|mis gustos|que tipo de espectador|que me gusta|"
                        r"que sabes de mi|mi tipo)\b")
_RE_ORDINAL = re.compile(r"\b(?:la|el|#)\s*(primera|primero|segunda|segundo|tercera|tercero|"
                         r"cuarta|quinta|ultima|ultimo|[1-5])\b")


def interpretar_reglas(mensaje: str, estado: dict, herr: Herramientas) -> dict:
    """Intención y cambios sin LLM: pedido.py + reglas de contexto básico."""
    texto = _normalizar(mensaje)
    p = interpretar_pedido(mensaje, datos=herr.datos,
                           personas=herr.artefactos / "app" / "personas.parquet")
    # Película referida: ordinal o título de una mostrada.
    ref = None
    m = _RE_ORDINAL.search(texto)
    if m and estado["mostradas"]:
        ref = _pelicula_ref(m.group(1), estado, herr)
    if ref is None:
        for peli in estado["mostradas"]:
            base = _normalizar(re.sub(r"\s*\(\d{4}\)\s*$", "", peli["titulo"]))
            base = re.sub(r",\s*(the|a|an|la|el|los|las)$", "", base).strip()
            if len(base) >= 3 and base in texto:
                ref = peli["movieId"]
                break
    condiciones = {"generos": list(p["generos"]), "tags": list(p["tags_genome"]),
                   "personas": list(p["personas"]),
                   "max_duracion_min": p["max_duracion_min"], "quitar": [], "pelicula": ref}
    hay_algo = bool(condiciones["generos"] or condiciones["tags"] or condiciones["personas"]
                    or condiciones["max_duracion_min"])
    if _RE_DESCARTAR.search(texto) and estado["mostradas"]:
        if ref is None:                       # 'ya la vi' sin decir cuál: la primera
            condiciones["pelicula"] = estado["mostradas"][0]["movieId"]
        return {"intencion": "descartar", **condiciones}
    if ref is not None:
        return {"intencion": "pregunta_pelicula", **condiciones}
    if _RE_PERFIL.search(texto):
        return {"intencion": "pregunta_perfil", **condiciones}
    hay_estado = bool(estado["mostradas"])
    # 'pero que sea corta', 'y que tenga giro', 'algo más corto': ajuste sin género nuevo.
    if hay_estado and not condiciones["generos"] and (
            _RE_AJUSTE.search(texto) or (hay_algo and _RE_MAS.search(texto))):
        return {"intencion": "ajuste", **condiciones}
    if hay_estado and _RE_MAS.search(texto) and not hay_algo:
        return {"intencion": "mas", **condiciones}
    if p["pide_recomendacion"] or hay_algo or p.get("titulo_mencionado"):
        if p.get("titulo_mencionado") and not hay_algo:
            r = herr.buscar_pelicula(p["titulo_mencionado"])
            if r.get("encontrada"):
                condiciones["referencia"] = int(r["movieId"])
                condiciones["generos"] = list(r.get("generos") or [])
        return {"intencion": "nuevo_pedido", **condiciones}
    if re.search(r"\b(sorprend\w*|algo|recomienda\w*)\b", texto):
        return {"intencion": "nuevo_pedido", **condiciones}
    return {"intencion": "charla", **condiciones}


# --------------------------------------------------------------------------- #
# Aplicar cambios al estado
# --------------------------------------------------------------------------- #

def _nombre_persona(p: dict) -> str:
    return str(p.get("nombre") or p.get("texto") or "")


def etiquetas(estado: dict) -> list[tuple[str, str]]:
    """Chips de las condiciones activas: (clave interna, texto visible)."""
    chips = [(f"genero:{g}", perfiles.GENEROS_ES.get(g, g)) for g in estado["generos"]
             if g != "Family"]
    chips += [(f"tag:{t}", t) for t in estado["tags"]]
    if estado["max_duracion_min"]:
        chips.append(("duracion", f"≤ {estado['max_duracion_min']} min"))
    chips += [(f"persona:{_nombre_persona(p)}", _nombre_persona(p)) for p in estado["personas"]]
    return chips


def quitar_condicion(estado: dict, clave: str) -> None:
    """Quita una condición (botón × de su chip)."""
    tipo, _, valor = clave.partition(":")
    if tipo == "genero":
        estado["generos"] = [g for g in estado["generos"] if g != valor]
    elif tipo == "tag":
        estado["tags"] = [t for t in estado["tags"] if t != valor]
    elif tipo == "duracion":
        estado["max_duracion_min"] = None
    elif tipo == "persona":
        estado["personas"] = [p for p in estado["personas"] if _nombre_persona(p) != valor]


def _quitar_por_texto(estado: dict, textos: list[str]) -> None:
    for t in textos:
        n = _normalizar(t)
        for clave, visible in etiquetas(estado):
            if n and (n in _normalizar(visible) or n in _normalizar(clave)):
                quitar_condicion(estado, clave)


def aplicar(c: dict, estado: dict) -> None:
    """Aplica la intención validada sobre el estado."""
    i = c["intencion"]
    if i == "nuevo_pedido":
        estado.update(generos=list(c.get("generos") or []), tags=list(c.get("tags") or []),
                      personas=list(c.get("personas") or []),
                      max_duracion_min=c.get("max_duracion_min"),
                      referencia=c.get("referencia"))
    elif i == "ajuste":
        _quitar_por_texto(estado, c.get("quitar") or [])
        for k in ("generos", "tags"):
            estado[k] += [x for x in c.get(k) or [] if x not in estado[k]]
        nombres = {_nombre_persona(p) for p in estado["personas"]}
        estado["personas"] += [p for p in c.get("personas") or []
                               if _nombre_persona(p) not in nombres]
        if c.get("max_duracion_min"):
            estado["max_duracion_min"] = c["max_duracion_min"]
    elif i == "descartar" and c.get("pelicula") is not None:
        mid = int(c["pelicula"])
        if mid not in estado["descartadas"]:
            estado["descartadas"].append(mid)
        estado["mostradas"] = [m for m in estado["mostradas"] if m["movieId"] != mid]


def _como_pedido(estado: dict) -> dict:
    """Forma que esperan llm.entendi y Herramientas.recomendar."""
    return {"generos": estado["generos"], "tags": estado["tags"],
            "max_duracion_min": estado["max_duracion_min"], "personas": estado["personas"],
            "referencia": estado.get("referencia"), "n": N_DEFECTO}


def entendi_estado(estado: dict) -> str:
    return entendi(_como_pedido(estado))


def recomendar(estado: dict, userId: int, herr: Herramientas, n: int = N_DEFECTO) -> list[dict]:
    """SOLO Herramientas.recomendar con el estado, sin mostradas ni descartadas."""
    excluir = {m["movieId"] for m in estado["mostradas"]} | set(estado["descartadas"])
    if estado.get("referencia") is not None:
        excluir.add(estado["referencia"])
    res = herr.recomendar(userId, n=n + len(excluir), generos=estado["generos"],
                          tags=estado["tags"], max_duracion_min=estado["max_duracion_min"],
                          personas=estado["personas"] or None, excluir_vistas=True)
    perfil = herr.perfil_gustos(userId)
    calificadas = herr.peliculas_calificadas(userId, n=200).get("peliculas", [])
    refs: set = set()
    recs = [r for r in res.get("recomendaciones", []) if r["movieId"] not in excluir][:n]
    inicio = len(estado["mostradas"])
    salida = []
    for k, r in enumerate(recs, start=1):
        r = _con_condiciones({**r, "explicacion": datos_de_respaldo(r, perfil, calificadas,
                                                                     refs)})
        r["rank"] = inicio + k
        salida.append(r)
    return salida


# --------------------------------------------------------------------------- #
# Contexto y redacción
# --------------------------------------------------------------------------- #

def contexto(estado: dict, userId: int, herr: Herramientas, cat=None,
             pelicula: int | None = None) -> dict:
    """Contexto compacto del usuario para el modelo del chat."""
    perfil = herr.perfil_gustos(userId)
    top = herr.peliculas_calificadas(userId, n=10).get("peliculas", [])
    mostradas = []
    for m in estado["mostradas"][-10:]:
        item = {"numero": m.get("rank"), "titulo": m["titulo"],
                "condiciones": m.get("condiciones_texto", "")}
        e = cat.explicar(userId, m["movieId"]) if cat is not None else None
        item["explicacion"] = (e or {}).get("texto") or m.get("explicacion", "")
        mostradas.append(item)
    ctx = {"tipo_espectador": perfil.get("tipo"),
           "gustos": {"generos": perfil.get("top_generos_es") or perfil.get("top_generos"),
                      "tags": perfil.get("top_tags") or []},
           "mejor_calificadas": [{"titulo": c["titulo"], "rating": c["rating"]} for c in top],
           "mostradas": mostradas, "condiciones": entendi_estado(estado)}
    if pelicula is not None:
        ficha = (cat.ficha(pelicula) if cat is not None else None) or herr.info_pelicula(pelicula)
        ctx["ficha"] = {k: v for k, v in (ficha or {}).items()
                        if k in ("titulo", "anio", "generos", "generos_es", "duracion_min",
                                 "imdb_rating", "directores", "reparto", "sinopsis",
                                 "promedio", "votos", "tags")}
        if isinstance(ctx["ficha"].get("reparto"), list):
            ctx["ficha"]["reparto"] = ctx["ficha"]["reparto"][:6]
    return ctx


def _titulos_permitidos(ctx: dict, recs: list[dict]) -> set[str]:
    nombres = {m["titulo"] for m in ctx["mostradas"]} | {c["titulo"] for c in
                                                         ctx["mejor_calificadas"]}
    nombres |= {r["titulo"] for r in recs}
    if ctx.get("ficha", {}).get("titulo"):
        nombres.add(ctx["ficha"]["titulo"])
    # Se aceptan también sin el año final: «Heat» por «Heat (1995)».
    return {_normalizar(n) for n in nombres} | {
        _normalizar(re.sub(r"\s*\(\d{4}\)\s*$", "", n)) for n in nombres}


def texto_valido(texto: str, ctx: dict, recs: list[dict]) -> bool:
    """Todo título citado («…» o "…") debe estar en el contexto entregado."""
    if not texto or len(texto) > 1200:
        return False
    permitidos = _titulos_permitidos(ctx, recs)
    citados = re.findall(r"[«\"“]([^»\"”]+)[»\"”]", texto)
    return all(_normalizar(c.strip()) in permitidos for c in citados)


def plantilla(intencion: str, estado: dict, ctx: dict, recs: list[dict],
              pelicula: int | None, herr: Herramientas) -> str:
    """Respuesta determinista del modo reglas para cada intención."""
    if intencion in ("nuevo_pedido", "ajuste", "mas"):
        if not recs:
            return "No quedan más películas que cumplan esto. Prueba quitando una condición."
        inicio = {"nuevo_pedido": "Busqué películas que no has visto",
                  "ajuste": "Ajusté la búsqueda manteniendo lo anterior",
                  "mas": "Aquí van otras opciones con las mismas condiciones"}[intencion]
        tipo = f" Eres «{ctx['tipo_espectador']}»." if ctx.get("tipo_espectador") else ""
        return f"{inicio}.{tipo} Encontré {len(recs)}:"
    if intencion == "descartar":
        info = herr.info_pelicula(pelicula) if pelicula is not None else {}
        titulo = info.get("titulo") if isinstance(info, dict) else None
        return (f"Listo, saqué «{titulo}» de esta conversación (no la califiqué). Pídeme "
                "«otra» si quieres más." if titulo else "¿Cuál quieres sacar? Dime su número.")
    if intencion == "pregunta_pelicula":
        f = ctx.get("ficha") or {}
        if not f:
            return "No tengo datos de esa película en el catálogo."
        partes = [f"«{f['titulo']}»"]
        if f.get("generos_es") or f.get("generos"):
            partes.append("es " + _lista_es(list(f.get("generos_es") or f["generos"])[:3]))
        if f.get("duracion_min"):
            partes.append(f"dura {f['duracion_min']} min")
        if f.get("directores"):
            partes.append("dirigida por " + _lista_es(list(f["directores"])[:2]))
        texto = ", ".join(partes) + "."
        if f.get("imdb_rating"):
            texto += f" Tiene {f['imdb_rating']:.1f} en IMDb."
        else:
            texto += " No tengo su nota de IMDb."
        mostrada = next((m for m in ctx["mostradas"] if m["titulo"] == f["titulo"]), None)
        if mostrada and mostrada.get("explicacion"):
            texto += " " + mostrada["explicacion"]
        return texto
    if intencion == "pregunta_perfil":
        g = ctx["gustos"]["generos"] or []
        top = [c["titulo"] for c in ctx["mejor_calificadas"][:3]]
        texto = (f"Tu tipo de espectador es «{ctx['tipo_espectador']}». "
                 if ctx.get("tipo_espectador") else "Aún no tengo tu tipo de espectador. ")
        if g:
            texto += f"Te inclinas por {_lista_es(list(g)[:3])}. "
        if top:
            texto += "Entre tus mejor calificadas están " + _lista_es(
                [f"«{t}»" for t in top]) + "."
        return texto.strip()
    return AYUDA


def _llamar(sistema: str, usuario: str, post=None, json_estricto=False) -> tuple[str | None, str]:
    if str(config.LLM_PROVIDER).lower() != "api":
        return None, "reglas"
    return proveedores.completar([{"role": "system", "content": sistema},
                                  {"role": "user", "content": usuario}],
                                 config.LLM_MODELO_CHAT, uso="chat", post=post,
                                 json_estricto=json_estricto)


def turno(mensaje: str, estado: dict, userId: int, herr: Herramientas, cat=None,
          post=None) -> Turno:
    """Procesa un mensaje: interpreta, valida, aplica, recomienda y redacta."""
    ctx_previo = contexto(estado, userId, herr)
    historia = estado["mensajes"][-N_MENSAJES:]
    estado["mensajes"].append({"role": "user", "content": mensaje})
    proveedor = "reglas"
    crudo, prov = _llamar(SISTEMA_INTERPRETAR, json.dumps(
        {"ultimos_mensajes": historia, "contexto": ctx_previo, "mensaje": mensaje},
        ensure_ascii=False, default=str), post=post, json_estricto=True)
    propuesta = None
    if crudo:
        try:
            propuesta = json.loads(crudo)
        except ValueError:
            propuesta = _extraer_json_libre(crudo)
    if isinstance(propuesta, dict) and propuesta.get("intencion") in INTENCIONES:
        cambios = validar(propuesta, estado, herr)
        proveedor = prov
        if cambios["intencion"] == "nuevo_pedido" and not (
                cambios["generos"] or cambios["tags"] or cambios["personas"]
                or cambios["max_duracion_min"]):
            # El modelo no tradujo condiciones: se completan con pedido.py.
            r = interpretar_reglas(mensaje, estado, herr)
            for k in ("generos", "tags", "personas", "max_duracion_min", "referencia"):
                cambios[k] = r.get(k) or cambios.get(k)
    else:
        cambios = interpretar_reglas(mensaje, estado, herr)
    intencion = cambios["intencion"]
    aplicar(cambios, estado)
    recs: list[dict] = []
    if intencion in ("nuevo_pedido", "ajuste", "mas"):
        if intencion != "mas":
            estado["mostradas"] = []           # nueva lista bajo nuevas condiciones
        recs = recomendar(estado, userId, herr)
        estado["mostradas"] += [{k: r.get(k) for k in ("movieId", "titulo", "rank",
                                                        "condiciones_texto", "explicacion")}
                                for r in recs]
    pelicula = cambios.get("pelicula") if intencion == "pregunta_pelicula" else None
    ctx = contexto(estado, userId, herr, cat, pelicula)
    base = plantilla(intencion, estado, ctx, recs,
                     cambios.get("pelicula"), herr)
    texto = None
    if proveedor != "reglas":
        datos = {"intencion": intencion, "mensaje": mensaje, "contexto": ctx,
                 "nuevas": [{"numero": r["rank"], "titulo": r["titulo"],
                             "condiciones": r.get("condiciones_texto"),
                             "afinidad": r.get("afinidad") or _afinidad(r.get("score"))}
                            for r in recs]}
        redactado, prov2 = _llamar(SISTEMA_RESPONDER, json.dumps(datos, ensure_ascii=False,
                                                                 default=str), post=post)
        if redactado and texto_valido(redactado.strip(), ctx, recs):
            texto, proveedor = redactado.strip(), prov2
    texto = texto or base
    linea = entendi_estado(estado) if intencion != "charla" or estado["mostradas"] else \
        "Entendí: no es un pedido de películas."
    estado["mensajes"].append({"role": "assistant", "content": texto})
    return Turno(texto=texto + "\n\n" + linea, intencion=intencion, recomendaciones=recs,
                 proveedor=proveedor, entendi=linea)


def _extraer_json_libre(texto: str) -> dict | None:
    m = re.search(r"\{.*\}", texto, flags=re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return _extraer_json(texto)


def recalcular(estado: dict, userId: int, herr: Herramientas) -> Turno:
    """Tras quitar un chip: misma conversación, lista nueva con el estado actual."""
    estado["mostradas"] = []
    recs = recomendar(estado, userId, herr)
    estado["mostradas"] = [{k: r.get(k) for k in ("movieId", "titulo", "rank",
                                                   "condiciones_texto", "explicacion")}
                           for r in recs]
    linea = entendi_estado(estado)
    texto = "Quité esa condición y recalculé." if recs else "No encontré películas."
    estado["mensajes"].append({"role": "assistant", "content": texto})
    return Turno(texto=texto + "\n\n" + linea, intencion="ajuste", recomendaciones=recs,
                 entendi=linea)
