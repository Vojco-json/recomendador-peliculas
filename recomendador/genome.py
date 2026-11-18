"""Genome de MovieLens como matriz compacta en memoria (genome.npy + índices).

genome-scores.csv de 25M pesa ~416 MB (~15 millones de filas). Antes, cada combinación
nueva de tags del chat lo volvía a leer por partes. Ahora se convierte UNA sola vez con
pyarrow en una matriz float32 [películas x tags] (~60 MB) que se guarda en:

- 25M (``config.DATOS``):  ``artefactos/25m/app/genome.npy`` y ``genome_indices.json``.
- Otras carpetas (muestra mini, pruebas): ``artefactos/genome/<huella de la ruta>/``.

Si el archivo no existe (o el CSV cambió) se construye la primera vez que se necesita.
La matriz se abre con memoria mapeada y queda en caché por proceso.
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from functools import lru_cache
from pathlib import Path

import numpy as np

from . import config

log = logging.getLogger(__name__)

NOMBRE_MATRIZ = "genome.npy"
NOMBRE_INDICES = "genome_indices.json"
DECIMALES = 5            # genome-scores.csv trae 5 decimales: se recuperan exactos
_CERROJO = threading.Lock()


def _mtime(ruta: Path) -> float:
    try:
        return ruta.stat().st_mtime
    except OSError:
        return 0.0


def carpeta_destino(ruta_datos) -> Path:
    """Dónde va la matriz de una carpeta de MovieLens."""
    ruta = Path(ruta_datos).resolve()
    try:
        if ruta == Path(config.DATOS).resolve():
            return Path(config.ARTEFACTOS_BASE) / "app"
    except OSError:
        pass
    huella = hashlib.md5(str(ruta).lower().encode("utf-8")).hexdigest()[:10]
    return Path(config.ARTEFACTOS) / "genome" / huella


def construir(ruta_datos, destino=None) -> Path | None:
    """Convierte genome-scores.csv (pyarrow) en genome.npy + genome_indices.json.

    Devuelve la carpeta destino, o None si la carpeta no trae genome.
    """
    import pyarrow.csv as pacsv

    ruta = Path(ruta_datos)
    rs, rt = ruta / "genome-scores.csv", ruta / "genome-tags.csv"
    if not rs.exists() or not rt.exists():
        return None
    destino = Path(destino) if destino else carpeta_destino(ruta)
    tabla = pacsv.read_csv(str(rs), convert_options=pacsv.ConvertOptions(
        column_types={"movieId": "int32", "tagId": "int32", "relevance": "float32"}))
    pelis = tabla["movieId"].to_numpy()
    tags = tabla["tagId"].to_numpy()
    rel = tabla["relevance"].to_numpy()
    ids_peli, fila = np.unique(pelis, return_inverse=True)
    ids_tag, col = np.unique(tags, return_inverse=True)
    matriz = np.full((len(ids_peli), len(ids_tag)), np.nan, dtype=np.float32)
    matriz[fila, col] = rel

    gt = pacsv.read_csv(str(rt)).to_pydict()
    nombre_tag = {int(i): str(t).lower() for i, t in zip(gt["tagId"], gt["tag"])}
    indices = {"movieIds": [int(x) for x in ids_peli], "tagIds": [int(x) for x in ids_tag],
               "tags": [nombre_tag.get(int(x), str(x)) for x in ids_tag],
               "origen": str(rs.resolve()), "origen_mtime": _mtime(rs)}

    destino.mkdir(parents=True, exist_ok=True)
    # Escritura atómica: primero a temporales, luego se renombran.
    tmp_npy = destino / (NOMBRE_MATRIZ + ".tmp.npy")
    np.save(tmp_npy, matriz)
    tmp_json = destino / (NOMBRE_INDICES + ".tmp")
    tmp_json.write_text(json.dumps(indices, ensure_ascii=False), encoding="utf-8")
    tmp_npy.replace(destino / NOMBRE_MATRIZ)
    tmp_json.replace(destino / NOMBRE_INDICES)
    log.info("genome.npy construido en %s (%d películas x %d tags)", destino,
             len(ids_peli), len(ids_tag))
    return destino


class Genome:
    """Matriz genome en memoria con acceso por movieId y por nombre de tag."""

    def __init__(self, matriz: np.ndarray, indices: dict):
        self.matriz = matriz
        self.movie_ids = np.asarray(indices["movieIds"], dtype=np.int64)
        self.tags = list(indices["tags"])
        self.col_tag = {t: j for j, t in enumerate(self.tags)}
        self.fila_peli = {int(m): i for i, m in enumerate(self.movie_ids)}

    def relevancias(self, tags) -> dict[str, dict[int, float]]:
        """{tag: {movieId: relevancia}} solo para los tags pedidos que existen."""
        salida: dict[str, dict[int, float]] = {}
        for t in tags:
            j = self.col_tag.get(str(t).lower())
            if j is None:
                continue
            col = np.asarray(self.matriz[:, j], dtype=np.float64)
            ok = ~np.isnan(col)
            valores = np.round(col[ok], DECIMALES)
            salida[str(t).lower()] = dict(zip(self.movie_ids[ok].tolist(), valores.tolist()))
        return salida

    def por_pelicula(self, tags) -> dict[int, dict[str, float]]:
        """{movieId: {tag: relevancia}} para los tags pedidos (formato de perfiles)."""
        salida: dict[int, dict[str, float]] = {}
        for tag, valores in self.relevancias(tags).items():
            for m, r in valores.items():
                salida.setdefault(m, {})[tag] = r
        return salida


@lru_cache(maxsize=4)
def _abrir(carpeta: str, _m_npy: float) -> Genome:
    c = Path(carpeta)
    indices = json.loads((c / NOMBRE_INDICES).read_text(encoding="utf-8"))
    return Genome(np.load(c / NOMBRE_MATRIZ, mmap_mode="r"), indices)


def cargar(ruta_datos) -> Genome | None:
    """Genome de la carpeta (lo construye si falta o si el CSV cambió); None sin genome."""
    ruta = Path(ruta_datos)
    rs = ruta / "genome-scores.csv"
    if not rs.exists() or not (ruta / "genome-tags.csv").exists():
        return None
    destino = carpeta_destino(ruta)
    with _CERROJO:                       # dos hilos no lo construyen a la vez
        vigente = False
        try:
            indices = json.loads((destino / NOMBRE_INDICES).read_text(encoding="utf-8"))
            vigente = ((destino / NOMBRE_MATRIZ).exists()
                       and abs(float(indices.get("origen_mtime", -1)) - _mtime(rs)) < 1e-3)
        except (OSError, ValueError):
            pass
        if not vigente:
            construir(ruta, destino)
    return _abrir(str(destino), _mtime(destino / NOMBRE_MATRIZ))


__all__ = ["Genome", "cargar", "construir", "carpeta_destino"]
