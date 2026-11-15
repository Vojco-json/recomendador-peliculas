"""Búsqueda aproximada de títulos: MovieLens como base y TMDb (español) opcional.

- Normaliza con ``enriquecimiento.variantes_titulo``: sin tildes ni mayúsculas, sin el
  año '(1999)', con el artículo final ', The' antepuesto y con los títulos alternativos
  entre paréntesis ('a.k.a. Se7en').
- Coincidencia exacta primero; si no hay, la más parecida con ``difflib`` y un ratio
  de al menos ``UMBRAL`` (0.8) para evitar falsos positivos.
- Con ``TMDB_API_KEY`` (cliente disponible) también compara contra los títulos en
  español de ``artefactos/tmdb_cache.json`` y contra ``ClienteTMDb.buscar``
  (/search/movie, language=es-ES, con caché). El tmdbId pasa a movieId con links.csv.
- Sin clave no hay llamadas de red: solo MovieLens.
"""

from __future__ import annotations

import csv
import difflib
from pathlib import Path

from .enriquecimiento import (ClienteTMDb, normalizar_titulo, separar_titulo_anio,
                              titulo_legible, variantes_titulo)

UMBRAL = 0.8


def _leer_csv(ruta: Path) -> list[dict]:
    try:
        with open(ruta, encoding="utf-8", newline="") as f:
            return list(csv.DictReader(f))
    except OSError:
        return []


class BuscadorTitulos:
    """Índice de títulos de una carpeta de MovieLens (movies.csv y links.csv)."""

    def __init__(self, datos, cliente_tmdb: ClienteTMDb | None = None):
        self.datos = Path(datos)
        self.cliente = cliente_tmdb if cliente_tmdb is not None else ClienteTMDb()
        self.peliculas: dict[int, dict] = {}            # movieId -> {titulo, anio}
        self.indice: dict[str, set[int]] = {}           # clave normalizada -> movieIds
        for fila in _leer_csv(self.datos / "movies.csv"):
            mid = int(fila["movieId"])
            claves, anio = variantes_titulo(fila["title"])
            self.peliculas[mid] = {"titulo": fila["title"], "anio": anio}
            for c in claves:
                self.indice.setdefault(c, set()).add(mid)
        self.tmdb_a_movie: dict[int, int] = {}
        for fila in _leer_csv(self.datos / "links.csv"):
            if (fila.get("tmdbId") or "").strip():
                self.tmdb_a_movie[int(float(fila["tmdbId"]))] = int(fila["movieId"])
        self._indice_es: dict[str, set[int]] | None = None

    @property
    def usa_tmdb(self) -> bool:
        return bool(self.cliente and self.cliente.disponible)

    def _indice_tmdb(self) -> dict[str, set[int]]:
        """Títulos en español de la caché de TMDb (solo si hay clave)."""
        if self._indice_es is None:
            self._indice_es = {}
            for tmdb_id, titulo in self.cliente.titulos_es().items():
                mid = self.tmdb_a_movie.get(tmdb_id)
                if mid in self.peliculas:
                    self._indice_es.setdefault(normalizar_titulo(titulo), set()).add(mid)
        return self._indice_es

    @staticmethod
    def _mejor(clave: str, indice: dict[str, set[int]]) -> tuple[float, set[int]]:
        if clave in indice:
            return 1.0, indice[clave]
        parecidos = difflib.get_close_matches(clave, list(indice), n=1, cutoff=UMBRAL)
        if not parecidos:
            return 0.0, set()
        return difflib.SequenceMatcher(None, clave, parecidos[0]).ratio(), indice[parecidos[0]]

    def _elegir(self, ids: set[int], anio: int | None) -> int:
        """Entre varias películas con el mismo título, la del año pedido (o la más antigua id)."""
        if anio:
            for mid in sorted(ids):
                if self.peliculas[mid]["anio"] == anio:
                    return mid
        return min(ids)

    def buscar(self, titulo: str) -> dict:
        """{'encontrada': True, movieId, titulo, titulo_legible, anio, coincidencia, fuente}
        o {'encontrada': False, 'mensaje': '...'} si no hay nada razonable."""
        consulta = str(titulo or "").strip().strip("\"'«»“”")
        sin_anio, anio = separar_titulo_anio(consulta)
        clave = normalizar_titulo(sin_anio)
        if not clave:
            return {"encontrada": False, "consulta": consulta,
                    "mensaje": "No indicaste un título para buscar."}

        candidatos = [(*self._mejor(clave, self.indice), "movielens")]
        if self.usa_tmdb:
            candidatos.append((*self._mejor(clave, self._indice_tmdb()), "tmdb_cache"))
            if candidatos[0][0] < 1.0 and candidatos[1][0] < 1.0:
                candidatos.append((*self._buscar_en_tmdb(sin_anio, clave), "tmdb_busqueda"))
        # Mayor parecido; ante empate gana el orden (MovieLens primero).
        puntaje, ids, fuente = max(candidatos, key=lambda c: c[0])
        if not ids or puntaje < UMBRAL:
            return {"encontrada": False, "consulta": consulta,
                    "mensaje": f"No encontré ninguna película parecida a «{consulta}» "
                               "en el catálogo."}
        mid = self._elegir(ids, anio)
        ficha = self.peliculas[mid]
        return {"encontrada": True, "consulta": consulta, "movieId": mid,
                "titulo": ficha["titulo"], "titulo_legible": titulo_legible(ficha["titulo"]),
                "anio": ficha["anio"], "coincidencia": round(puntaje, 3), "fuente": fuente}

    def _buscar_en_tmdb(self, consulta: str, clave: str) -> tuple[float, set[int]]:
        """Resultados de /search/movie (es-ES) que estén en el catálogo de MovieLens."""
        mejor, ids = 0.0, set()
        for r in self.cliente.buscar(consulta) or []:
            mid = self.tmdb_a_movie.get(r["tmdbId"])
            if mid not in self.peliculas:
                continue
            for t in (r.get("titulo"), r.get("titulo_original")):
                if not t:
                    continue
                p = difflib.SequenceMatcher(None, clave, normalizar_titulo(t)).ratio()
                if p > mejor:
                    mejor, ids = p, {mid}
        return mejor, ids


_BUSCADORES: dict[tuple, BuscadorTitulos] = {}


def buscar(titulo: str, datos, cliente_tmdb: ClienteTMDb | None = None) -> dict:
    """Atajo con un índice reutilizable por carpeta de datos (y cliente)."""
    clave = (str(Path(datos).resolve()), id(cliente_tmdb))
    if clave not in _BUSCADORES:
        _BUSCADORES[clave] = BuscadorTitulos(datos, cliente_tmdb)
    return _BUSCADORES[clave].buscar(titulo)


__all__ = ["BuscadorTitulos", "buscar", "UMBRAL"]
