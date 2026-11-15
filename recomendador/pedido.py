"""Interpretación determinista del pedido del usuario (español e inglés).

``interpretar_pedido(texto)`` convierte un mensaje libre en condiciones para el
recomendador, sin LLM:

    {
      "generos":            ["Drama"],                 # géneros de MovieLens
      "tags_genome":        ["sad"],                   # solo tags presentes en genome-tags.csv
      "tags_descartados":   ["tearjerker"],            # candidatos que no existen (sin error)
      "max_duracion_min":   100 | None,
      "animos":             ["llorar"],                # ánimos u ocasiones detectados
      "titulo_mencionado":  "Toy Story" | None,        # 'algo como X', 'parecida a X'
      "pide_recomendacion": True,                      # False en saludos o preguntas generales
      "n":                  5,
      "condiciones":        ["géneros: drama", ...],   # textos en español para mostrar
    }

Tolera tildes, mayúsculas y errores de tipeo ('comdia', 'terorr', 'ciensia ficcion')
con ``difflib`` de la biblioteca estándar: una palabra (o grupo de palabras) del
mensaje coincide con una clave si empiezan con la misma letra y su parecido es de al
menos ``UMBRAL_TIPEO``. Las claves de menos de 5 letras solo coinciden exactas.
Lo usan el modo reglas, el modo Ollama (``llm.interpretar``) y la red de seguridad.
"""

from __future__ import annotations

import csv
import re
import unicodedata
from difflib import SequenceMatcher
from functools import lru_cache
from pathlib import Path

DURACION_CORTA_MIN = 100   # 'corto'; igual que herramientas.DURACION_CORTA_MIN
N_DEFECTO = 5              # igual que herramientas.N_DEFECTO
UMBRAL_TIPEO = 0.82        # 'short'/'sport' (0.80) no coincide; 'terorr'/'terror' (0.83) sí
LARGO_MIN_APROX = 5        # claves más cortas exigen coincidencia exacta

# --------------------------------------------------------------------------- #
# Diccionarios ES/EN (claves sin tildes y en minúsculas)
# --------------------------------------------------------------------------- #

GENEROS: dict[str, list[str]] = {
    "Action": ["accion", "action", "de pelea"],
    "Adventure": ["aventura", "aventuras", "adventure"],
    "Animation": ["animacion", "animada", "animadas", "dibujos animados", "animated",
                  "animation", "cartoon", "cartoons"],
    "Children": ["infantil", "infantiles", "ninos", "para ninos", "kids", "children"],
    "Comedy": ["comedia", "comedias", "comedy", "comedies", "chistosa", "graciosa"],
    # ('comica' se omite: con tipeo se confundiría con 'comida')
    "Crime": ["crimen", "policial", "policiales", "crime", "mafia", "gangster",
              "gangsters"],
    "Documentary": ["documental", "documentales", "documentary"],
    "Drama": ["drama", "dramas", "dramatica"],
    "Fantasy": ["fantasia", "fantasy"],
    "Horror": ["terror", "miedo", "horror", "terrorifica"],
    "Musical": ["musical", "musicales"],
    "Mystery": ["misterio", "mystery"],
    "Romance": ["romance", "romantica", "romanticas", "romantic", "love story",
                "de amor"],
    "Sci-Fi": ["ciencia ficcion", "sci fi", "scifi", "science fiction", "futurista"],
    "Thriller": ["suspenso", "suspense", "thriller"],
    "War": ["guerra", "belica", "belicas", "war movie", "war film"],
    "Western": ["western", "vaqueros", "cowboys"],
}

GENEROS_ES = {
    "Action": "acción", "Adventure": "aventura", "Animation": "animación",
    "Children": "infantil", "Comedy": "comedia", "Crime": "crimen",
    "Documentary": "documental", "Drama": "drama", "Fantasy": "fantasía",
    "Horror": "terror", "Musical": "musical", "Mystery": "misterio",
    "Romance": "romance", "Sci-Fi": "ciencia ficción", "Thriller": "suspenso",
    "War": "bélica", "Western": "western", "Film-Noir": "cine negro", "IMAX": "IMAX",
    "Family": "familiar",
}

# Ánimos y ocasiones -> géneros, tags del genome y texto de la condición.
ANIMOS: dict[str, dict] = {
    "llorar": {
        "claves": ["llorar", "para llorar", "lagrimas", "triste", "tristes",
                   "conmovedora", "emotiva", "sad", "cry", "make me cry", "tearjerker",
                   "tear jerker"],
        "generos": ["Drama"], "tags": ["sad", "tearjerker", "emotional"],
        "texto": "para emocionarse (drama triste)"},
    "familia": {
        "claves": ["familia", "en familia", "familiar", "todo publico", "family",
                   "family movie", "family friendly"],
        "generos": ["Children", "Animation"], "tags": ["family"],
        "texto": "para ver en familia"},
    "liviano": {
        "claves": ["liviano", "liviana", "livianas", "ligera", "ligero", "relajada",
                   "divertida", "divertido", "entretenida", "buena onda", "alegre",
                   "light", "lighthearted", "feel good", "feelgood", "fun"],
        "generos": ["Comedy"], "tags": ["feel-good", "funny"],
        "texto": "algo liviano"},
    "pensar": {
        "claves": ["haga pensar", "hacer pensar", "hagan pensar", "reflexionar",
                   "reflexiva", "filosofica", "profunda", "make me think",
                   "thought provoking", "thoughtful", "mind bending", "cerebral"],
        "generos": [], "tags": ["thought-provoking", "philosophical", "cerebral"],
        "texto": "que haga pensar"},
    "giro": {
        "claves": ["giro final", "giro inesperado", "final inesperado",
                   "final sorprendente", "con giro", "plot twist", "twist ending",
                   "twist"],
        "generos": [], "tags": ["twist ending", "plot twist", "surprise ending"],
        "texto": "con giro final"},
    "asustar": {
        "claves": ["asustar", "que de miedo", "scary", "creepy", "spooky"],
        "generos": ["Horror"], "tags": ["scary", "creepy"],
        "texto": "que dé miedo"},
}

_RE_CORTO = re.compile(r"\b(?:cort[oa]s?|cortit[oa]s?|breves?|rapid[oa]s?|short|quick)\b")
_RE_MAX_MIN = re.compile(
    r"(?:menos de|maximo|max|hasta|no mas de|under|less than|no more than|at most|"
    r"below)\s*(\d{2,3})\s*(?:min|mins|minutos|minutes)?\b")
_RE_MAX_HORAS = re.compile(
    r"(?:menos de|maximo|hasta|no mas de|under|less than|at most)\s*(?:(\d)|una|one|an)"
    r"\s*(?:h|hr|hrs|hora|horas|hour|hours)\b")
_RE_N = re.compile(r"\b(?:dame|recomienda(?:me)?|quiero|top|give me|recommend)\s+(\d{1,2})\b")

# 'algo como X', 'parecida a X', 'something like X'... (sobre el texto original).
_RE_TITULO = re.compile(
    r"(?:algo\s+como|una?\s+(?:pel[ií]cula|peli)\s+como|(?:pel[ií]culas?|pelis?)\s+como|"
    r"parecid[ao]s?\s+a|similar(?:es)?\s+a|del\s+estilo\s+de|al\s+estilo\s+de|"
    r"something\s+like|(?:a\s+)?(?:movies?|films?)\s+like|similar\s+to)\s+"
    r"(?P<t>[^,.;!?¿¡\n]+)", re.IGNORECASE)
# Corta el título antes de una nueva condición ('... pero corta', '... y que sea').
_RE_FIN_TITULO = re.compile(r"\s+(?:pero|but|y que|and that|que sea|que dure|para|for)\s.*$",
                            re.IGNORECASE)

# Palabras que indican que se pide una recomendación aunque no haya condiciones.
_RE_PIDE = re.compile(
    r"\b(?:recomien\w*|recomenda\w*|sugier\w*|suger\w*|dame|quiero ver|que veo|que ver|"
    r"ver algo|algo para ver|peliculas?|pelis?|recommend\w*|suggest\w*|movies?|films?|"
    r"watch)\b")


# --------------------------------------------------------------------------- #
# Utilidades
# --------------------------------------------------------------------------- #

def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes y con la puntuación cambiada por espacios."""
    t = unicodedata.normalize("NFKD", str(texto).lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^0-9a-zñ]+", " ", t.replace("ñ", "n"))
    return re.sub(r"\s+", " ", t).strip()


def _parecido(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def contiene(palabras: list[str], clave: str) -> bool:
    """¿La clave (una o varias palabras) aparece en el mensaje, exacta o con un error de tipeo?"""
    partes = clave.split()
    k = len(partes)
    for i in range(len(palabras) - k + 1):
        ventana = " ".join(palabras[i:i + k])
        if ventana == clave:
            return True
        if (len(clave) >= LARGO_MIN_APROX and ventana[:1] == clave[:1]
                and _parecido(ventana, clave) >= UMBRAL_TIPEO):
            return True
    return False


@lru_cache(maxsize=8)
def _leer_tags(ruta: str) -> frozenset[str]:
    try:
        with open(ruta, encoding="utf-8", newline="") as f:
            return frozenset(fila["tag"].strip().lower() for fila in csv.DictReader(f))
    except (OSError, KeyError):
        return frozenset()


def cargar_tags_genome(datos=None) -> frozenset[str]:
    """Tags de ``genome-tags.csv`` en la carpeta de datos (vacío si no existe)."""
    if datos is None:
        from . import config
        datos = config.DATOS
    return _leer_tags(str(Path(datos) / "genome-tags.csv"))


def _agregar(lista: list, items) -> None:
    for x in items:
        if x not in lista:
            lista.append(x)


def _titulo_mencionado(texto: str) -> tuple[str | None, str]:
    """('Toy Story', texto sin esa parte) o (None, texto)."""
    m = _RE_TITULO.search(texto)
    if not m:
        return None, texto
    titulo = _RE_FIN_TITULO.sub("", m.group("t")).strip(" \"'«»“”‘’")
    if not titulo:
        return None, texto
    return titulo, texto[:m.start()] + " " + texto[m.end():]


def _duracion(t: str) -> int | None:
    m = _RE_MAX_MIN.search(t)
    if m and 30 <= int(m.group(1)) <= 400:
        return int(m.group(1))
    m = _RE_MAX_HORAS.search(t)
    if m:
        return int(m.group(1) or 1) * 60
    if _RE_CORTO.search(t):
        return DURACION_CORTA_MIN
    return None


# --------------------------------------------------------------------------- #
# Función principal
# --------------------------------------------------------------------------- #

def interpretar_pedido(texto: str, tags_validos=None, datos=None) -> dict:
    """Convierte un mensaje libre en condiciones (ver el docstring del módulo).

    tags_validos: conjunto de tags del genome; si es None se lee ``genome-tags.csv``
    de ``datos`` (por defecto ``config.DATOS``). Los tags que no existan se descartan.
    """
    texto = str(texto or "")
    titulo, resto = _titulo_mencionado(texto)
    t = normalizar(resto)          # el título no cuenta como género ('Star Wars' != War)
    palabras = t.split()

    generos: list[str] = []
    tags: list[str] = []
    animos: list[str] = []
    for nombre, animo in ANIMOS.items():
        if any(contiene(palabras, c) for c in animo["claves"]):
            animos.append(nombre)
            _agregar(generos, animo["generos"])
            _agregar(tags, animo["tags"])
    for genero, claves in GENEROS.items():
        if any(contiene(palabras, c) for c in claves):
            _agregar(generos, [genero])

    if tags_validos is None:
        tags_validos = cargar_tags_genome(datos)
    validos = {str(x).strip().lower() for x in tags_validos}
    tags_ok = [x for x in tags if x in validos]
    descartados = [x for x in tags if x not in validos]

    max_dur = _duracion(t)
    m = _RE_N.search(t)
    n = max(1, min(int(m.group(1)), 10)) if m else N_DEFECTO

    pide = bool(generos or tags or max_dur or titulo or _RE_PIDE.search(t))

    condiciones: list[str] = []
    if generos:
        nombres = [GENEROS_ES.get(g, g) for g in generos]
        condiciones.append(("género: " if len(nombres) == 1 else "géneros: ")
                           + ", ".join(nombres))
    if max_dur:
        condiciones.append(f"de {max_dur} min o menos")
    condiciones += [ANIMOS[a]["texto"] for a in animos]
    if titulo:
        condiciones.append(f"parecida a «{titulo}»")

    return {
        "generos": generos,
        "tags_genome": tags_ok,
        "tags_descartados": descartados,
        "max_duracion_min": max_dur,
        "animos": animos,
        "titulo_mencionado": titulo,
        "pide_recomendacion": pide,
        "n": n,
        "condiciones": condiciones,
    }


__all__ = ["interpretar_pedido", "cargar_tags_genome", "normalizar", "contiene",
           "GENEROS", "GENEROS_ES", "ANIMOS", "DURACION_CORTA_MIN", "UMBRAL_TIPEO"]
