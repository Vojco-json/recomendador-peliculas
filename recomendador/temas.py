"""Temas y época del pedido (PEDIDO_FINAL.md §2-§4).

Un **tema** es UNA condición del orden por niveles con dos tipos de evidencia:
- fuerte: algún tag del grupo tiene relevancia genome >= UMBRAL_TEMA -> vale 1;
- débil: sin evidencia fuerte, pero la película tiene un género señal -> vale 0,5;
- ninguna -> vale 0. Sin datos genome solo cuenta la señal débil.

Los ánimos de antes (llorar, familia, liviano, pensar, giro, asustar) también son temas.
La **época** es otra condición (1 o 0; sin año, neutral como la duración).
"""
from __future__ import annotations

import re

UMBRAL_TEMA = 0.5
VALOR_DEBIL = 0.5

# nombre (español, visible) -> claves ES/EN (normalizadas), tags del genome y señal débil.
TEMAS: dict[str, dict] = {
    "policial": {"claves": ["policial", "policiales", "policia", "policias", "policiaca",
                            "policiaco", "detective", "detectives", "cop", "cops", "police"],
                 "tags": ["police", "police investigation", "police corruption",
                          "undercover cop", "detective", "private detective", "fbi"],
                 "senal": ["Crime"]},
    "mafia": {"claves": ["mafia", "mafias", "ganster", "gansters", "gangster", "gangsters",
                         "crimen organizado", "organized crime", "mob"],
              "tags": ["mafia", "gangster", "gangsters", "organized crime"],
              "senal": ["Crime"]},
    "atracos": {"claves": ["atraco", "atracos", "robo", "robos", "golpe", "heist",
                           "asalto a banco", "robbery"],
                "tags": ["heist", "bank robbery", "robbery"], "senal": ["Crime"]},
    "espías": {"claves": ["espia", "espias", "espionaje", "agente secreto", "spy", "spies",
                          "espionage"],
               "tags": ["spy", "espionage", "cia"], "senal": ["Thriller"]},
    "juicios": {"claves": ["juicio", "juicios", "abogado", "abogados", "tribunal",
                           "courtroom", "lawyer", "lawyers"],
                "tags": ["courtroom", "courtroom drama", "lawyer", "lawyers"],
                "senal": ["Drama"]},
    "asesino en serie": {"claves": ["asesino en serie", "asesinos en serie", "serial killer",
                                    "serial killers"],
                         "tags": ["serial killer"], "senal": ["Thriller"]},
    "superhéroes": {"claves": ["superheroe", "superheroes", "superhero", "superheroes"],
                    "tags": ["superhero", "superheroes", "comic book"], "senal": ["Action"]},
    "zombis": {"claves": ["zombi", "zombis", "zombie", "zombies"],
               "tags": ["zombie", "zombies"], "senal": ["Horror"]},
    "vampiros": {"claves": ["vampiro", "vampiros", "vampire", "vampires"],
                 "tags": ["vampire", "vampires"], "senal": ["Horror"]},
    "viajes en el tiempo": {"claves": ["viajes en el tiempo", "viaje en el tiempo",
                                       "time travel"],
                            "tags": ["time travel"], "senal": ["Sci-Fi"]},
    "extraterrestres": {"claves": ["extraterrestre", "extraterrestres", "alien", "aliens",
                                   "alienigena", "alienigenas"],
                        "tags": ["alien", "aliens", "alien invasion", "first contact"],
                        "senal": ["Sci-Fi"]},
    "espacio": {"claves": ["espacio", "espacial", "espaciales", "astronauta", "astronautas",
                           "space", "outer space"],
                "tags": ["space", "space travel", "space opera"], "senal": ["Sci-Fi"]},
    "distopía": {"claves": ["distopia", "distopica", "distopias", "postapocaliptica",
                            "post apocaliptica", "fin del mundo", "dystopia", "dystopian",
                            "post apocalyptic"],
                 "tags": ["dystopia", "post-apocalyptic", "apocalypse"], "senal": ["Sci-Fi"]},
    "cárcel": {"claves": ["carcel", "prision", "prisiones", "prison", "presos"],
               "tags": ["prison", "prison escape"], "senal": ["Drama"]},
    "deportes": {"claves": ["deporte", "deportes", "deportiva", "deportivas", "boxeo",
                            "futbol", "sports", "sport", "boxing"],
                 "tags": ["sports", "boxing", "football", "basketball"], "senal": ["Drama"]},
    "hechos reales": {"claves": ["hechos reales", "historia real", "historias reales",
                                 "caso real", "true story", "based on a true story",
                                 "biografica", "biopic"],
                      "tags": ["based on a true story", "true story", "biographical"],
                      "senal": ["Drama"]},
    "artes marciales": {"claves": ["artes marciales", "kung fu", "samurai", "samurais",
                                   "martial arts"],
                        "tags": ["martial arts", "kung fu", "samurai"], "senal": ["Action"]},
    "segunda guerra": {"claves": ["segunda guerra", "segunda guerra mundial", "nazi", "nazis",
                                  "ww2", "wwii", "world war ii", "world war 2"],
                       "tags": ["world war ii", "nazis"], "senal": ["War"]},
    # Ánimos y ocasiones de antes (pedido.ANIMOS): ahora son temas.
    "para llorar": {"claves": ["llorar", "para llorar", "lagrimas", "triste", "tristes",
                               "conmovedora", "emotiva", "sad", "cry", "make me cry",
                               "tearjerker", "tear jerker"],
                    "tags": ["sad", "tearjerker", "emotional"], "senal": ["Drama"],
                    "animo": "llorar"},
    "para ver en familia": {"claves": ["familia", "en familia", "familiar", "todo publico",
                                       "family", "family movie", "family friendly"],
                            "tags": ["family"], "senal": ["Children", "Animation"],
                            "animo": "familia"},
    "algo liviano": {"claves": ["liviano", "liviana", "livianas", "ligera", "ligero",
                                "relajada", "divertida", "divertido", "entretenida",
                                "buena onda", "alegre", "light", "lighthearted", "feel good",
                                "feelgood", "fun"],
                     "tags": ["feel-good", "funny"],
                     "senal": ["Comedy", "Animation", "Children"], "animo": "liviano"},
    "que haga pensar": {"claves": ["haga pensar", "hacer pensar", "hagan pensar",
                                   "reflexionar", "reflexiva", "filosofica", "profunda",
                                   "make me think", "thought provoking", "thoughtful",
                                   "mind bending", "cerebral"],
                        "tags": ["thought-provoking", "philosophical", "cerebral"],
                        "senal": [], "animo": "pensar"},
    "giro final": {"claves": ["giro final", "giro inesperado", "final inesperado",
                              "final sorprendente", "con giro", "plot twist", "twist ending",
                              "twist"],
                   "tags": ["twist ending", "plot twist", "surprise ending"], "senal": [],
                   "animo": "giro"},
    "que dé miedo": {"claves": ["asustar", "que de miedo", "scary", "creepy", "spooky"],
                     "tags": ["scary", "creepy"], "senal": ["Horror"], "animo": "asustar"},
}

# Temas de la tabla de PEDIDO_FINAL.md §3 (sin los ánimos).
TEMAS_CATALOGO = [n for n, t in TEMAS.items() if "animo" not in t]


def tags_del_tema(nombre: str, tags_validos=None) -> list[str]:
    """Tags del grupo que existen en genome-tags (todos si no se pasa el conjunto)."""
    tags = TEMAS[nombre]["tags"]
    if tags_validos is None:
        return list(tags)
    validos = {str(x).strip().lower() for x in tags_validos}
    return [t for t in tags if t in validos]


def nombre_tema(valor) -> str | None:
    """Nombre canónico de un tema a partir de su nombre o de una clave (ES/EN)."""
    from .pedido import normalizar

    n = normalizar(valor or "")
    for nombre, t in TEMAS.items():
        if n == normalizar(nombre) or n in t["claves"] or n == t.get("animo"):
            return nombre
    return None


def tema_de_tag(tag) -> str | None:
    """Tema al que pertenece un tag suelto del genome (para convertirlo en el tema)."""
    t = str(tag or "").strip().lower()
    for nombre, d in TEMAS.items():
        if t in d["tags"]:
            return nombre
    return None


def texto_cumple(nombre: str, fuerte: bool) -> str:
    """Cómo se informa el tema en cumple: directo o «(solo por género X)»."""
    if fuerte:
        return nombre
    from . import perfiles
    generos = "/".join(perfiles.GENEROS_ES.get(g, g).capitalize()
                       for g in TEMAS[nombre]["senal"])
    return f"{nombre} (solo por género {generos})"


def evaluar_tema(nombre: str, ficha: dict, genome: dict[str, dict[int, float]],
                 tags_validos=None) -> tuple[float, str]:
    """(valor 1 / 0,5 / 0, texto) de un tema para una película."""
    mid = ficha["movieId"]
    for t in tags_del_tema(nombre, tags_validos):
        if (genome.get(t, {}).get(mid) or 0.0) >= UMBRAL_TEMA:
            return 1.0, texto_cumple(nombre, True)
    if any(g in ficha.get("generos", []) for g in TEMAS[nombre]["senal"]):
        return VALOR_DEBIL, texto_cumple(nombre, False)
    return 0.0, nombre


# --------------------------------------------------------------------------- #
# Época
# --------------------------------------------------------------------------- #

_DECADAS_PALABRA = {"setentera": 1970, "setenteras": 1970, "ochentera": 1980,
                    "ochenteras": 1980, "noventera": 1990, "noventeras": 1990,
                    "sesentera": 1960}
_RE_DECADA = re.compile(r"\b(?:de los|los|anos|decada de los|decada del|en los)\s+"
                        r"(19[0-9]0|20[0-2]0|[2-9]0)s?\b(?!\s*(?:min|minutos|minutes)\b)")
_RE_DECADA_EN = re.compile(r"\b(?:19)?([2-9]0)s\b|\b(2000|2010|2020)s\b")
_RE_ANIO = re.compile(r"\b(?:de|del|en el|en|from|año|ano)\s+((?:19|20)\d\d)\b")
_RE_CLASICA = re.compile(r"\b(?:clasica|clasicas|clasico|clasicos|antigua|antiguas|vieja|"
                         r"viejas|old|classic|classics)\b")
_RE_RECIENTE = re.compile(r"\b(?:reciente|recientes|nueva|nuevas|moderna|modernas|recent|"
                          r"new)\b")


def _decada(valor: str) -> int:
    v = int(valor)
    return v if v >= 1900 else 1900 + v


def epoca_decada(inicio: int) -> dict:
    texto = f"años {str(inicio)[2:]}" if inicio < 2000 else f"años {inicio}"
    return {"desde": inicio, "hasta": inicio + 9, "texto": texto}


def detectar_epoca(t: str) -> dict | None:
    """Época en un texto ya normalizado: {'desde', 'hasta', 'texto'} o None."""
    m = _RE_DECADA.search(t)
    if m:
        return epoca_decada(_decada(m.group(1)))
    for palabra, inicio in _DECADAS_PALABRA.items():
        if re.search(rf"\b{palabra}\b", t):
            return epoca_decada(inicio)
    m = _RE_DECADA_EN.search(t)
    if m:
        return epoca_decada(_decada(m.group(1) or m.group(2)))
    m = _RE_ANIO.search(t)
    if m:
        a = int(m.group(1))
        return {"desde": a, "hasta": a, "texto": str(a)}
    if _RE_CLASICA.search(t):
        return {"desde": None, "hasta": 1979, "texto": "clásica (antes de 1980)"}
    if _RE_RECIENTE.search(t):
        return {"desde": 2010, "hasta": None, "texto": "reciente (desde 2010)"}
    return None


def validar_epoca(e) -> dict | None:
    """Época propuesta por el LLM -> forma válida o None (lo inválido se ignora)."""
    if not isinstance(e, dict):
        return None
    def anio(x):
        return int(x) if isinstance(x, (int, float)) and 1880 <= x <= 2100 else None
    desde, hasta = anio(e.get("desde")), anio(e.get("hasta"))
    if desde is None and hasta is None:
        return None
    if desde is not None and hasta is not None and desde > hasta:
        return None
    texto = str(e.get("texto") or "").strip()[:40]
    if not texto:
        if desde and hasta and hasta - desde == 9 and desde % 10 == 0:
            texto = epoca_decada(desde)["texto"]
        elif desde == hasta:
            texto = str(desde)
        elif desde is None:
            texto = f"antes de {hasta + 1}"
        else:
            texto = f"desde {desde}"
    return {"desde": desde, "hasta": hasta, "texto": texto}


def cumple_epoca(epoca: dict, anio) -> bool | None:
    """True/False según el año; None si la película no tiene año (neutral)."""
    if anio is None:
        return None
    try:
        a = int(anio)
    except (TypeError, ValueError):
        return None
    return ((epoca.get("desde") is None or a >= epoca["desde"])
            and (epoca.get("hasta") is None or a <= epoca["hasta"]))


__all__ = ["TEMAS", "TEMAS_CATALOGO", "UMBRAL_TEMA", "VALOR_DEBIL", "nombre_tema",
           "tema_de_tag", "evaluar_tema", "tags_del_tema", "detectar_epoca",
           "validar_epoca", "cumple_epoca", "epoca_decada"]
