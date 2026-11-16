"""Filtro de reseñas públicas: largo máximo y palabras ofensivas.

- Una reseña tiene entre 1 y 500 caracteres (sin contar espacios al inicio y al final).
- Se rechaza si contiene una palabra o expresión de ``palabras_ofensivas.txt``. La
  comparación es sin tildes ni mayúsculas y por palabra completa: «IDIÓTA» y «idiota»
  se rechazan, pero «computadora» no cae por contener «puta».

La moderación posterior (ocultar una reseña ya publicada) NO está aquí: se hace solo con
el script local scripts/moderar_resenas.py.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MAX_CARACTERES = 500
RUTA_PALABRAS = Path(__file__).with_name("palabras_ofensivas.txt")

MENSAJE_VACIA = "La reseña está vacía: escribe algo sobre la película."
MENSAJE_LARGA = ("La reseña tiene {n} caracteres y el máximo es {m}. "
                 "Acórtala un poco y vuelve a publicarla.")
MENSAJE_OFENSIVA = ("La reseña no se publicó porque contiene lenguaje ofensivo. "
                    "Reescríbela sin insultos y vuelve a intentarlo.")


def normalizar(texto: str) -> str:
    """Minúsculas, sin tildes (á→a, ñ→n, ü→u) y con espacios simples."""
    base = unicodedata.normalize("NFKD", str(texto).lower())
    sin = "".join(c for c in base if not unicodedata.combining(c))
    return " ".join(sin.split())


def _palabras(texto: str) -> list[str]:
    """Palabras normalizadas (letras y dígitos; lo demás separa)."""
    return re.findall(r"[a-z0-9]+", normalizar(texto))


@lru_cache(maxsize=4)
def cargar_lista(ruta: str = str(RUTA_PALABRAS)) -> tuple[tuple[str, ...], ...]:
    """Expresiones prohibidas como tuplas de palabras normalizadas (sin repetir)."""
    salida = set()
    for linea in Path(ruta).read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#"):
            continue
        palabras = tuple(_palabras(linea))
        if palabras:
            salida.add(palabras)
    return tuple(sorted(salida))


def palabras_ofensivas(texto: str, ruta=None) -> list[str]:
    """Expresiones de la lista que aparecen en el texto como palabras completas."""
    tokens = _palabras(texto)
    encontradas = []
    for expr in cargar_lista(str(ruta or RUTA_PALABRAS)):
        n = len(expr)
        if any(tuple(tokens[i:i + n]) == expr for i in range(len(tokens) - n + 1)):
            encontradas.append(" ".join(expr))
    return encontradas


@dataclass(frozen=True)
class Revision:
    """Resultado del filtro: ok, mensaje para el usuario y el texto limpio."""
    ok: bool
    mensaje: str
    texto: str


def revisar_resena(texto: str, ruta=None) -> Revision:
    """Valida una reseña antes de publicarla."""
    limpio = (texto or "").strip()
    if not limpio:
        return Revision(False, MENSAJE_VACIA, limpio)
    if len(limpio) > MAX_CARACTERES:
        return Revision(False, MENSAJE_LARGA.format(n=len(limpio), m=MAX_CARACTERES), limpio)
    if palabras_ofensivas(limpio, ruta):
        return Revision(False, MENSAJE_OFENSIVA, limpio)
    return Revision(True, "", limpio)
