"""Modo demo: artefactos precalculados sobre 25M + subconjunto pequeño para reentrenar en vivo.

``preparar_demo`` copia a la carpeta de salida el modelo ALS, los factores, los perfiles
y los tipos calculados sobre 25M (ARTEFACTOS_BASE), y escribe ``base_demo.parquet`` con
los ratings ``>= desde`` y anteriores al corte. El streaming en ``--modo demo`` reentrena
ALS solo con esa base + los eventos nuevos, así tarda minutos y no horas.

No necesita Spark: lee ratings.csv con pandas por bloques (memoria acotada en 25M).
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from . import config
from .datos import fecha_a_epoch

# Lo que se copia de la base (lo obligatorio primero).
OBLIGATORIOS = ("item_factors.parquet",)
OPCIONALES = ("modelo_als", "user_factors.parquet", "perfiles.parquet", "tipos.json",
              "metricas.json", "recs_top20.parquet")
BLOQUE = 2_000_000


def _copiar(origen: Path, destino: Path) -> None:
    """Copia archivo o carpeta reemplazando lo que hubiera en el destino."""
    if destino.is_dir():
        shutil.rmtree(destino)
    elif destino.exists():
        destino.unlink()
    if origen.is_dir():
        shutil.copytree(origen, destino)
    else:
        shutil.copy2(origen, destino)


def ratings_subconjunto(datos, desde: str, corte: str):
    """Ratings de ``datos/ratings.csv`` con desde <= timestamp < corte (pandas)."""
    import pandas as pd

    ini, fin = fecha_a_epoch(desde), fecha_a_epoch(corte)
    if ini >= fin:
        raise ValueError(f"desde {desde} debe ser anterior al corte {corte}")
    archivo = Path(datos) / "ratings.csv"
    if not archivo.exists():
        raise FileNotFoundError(f"No existe {archivo}")
    tipos = {"userId": "int32", "movieId": "int32", "rating": "float64", "timestamp": "int64"}
    partes = [b[(b["timestamp"] >= ini) & (b["timestamp"] < fin)]
              for b in pd.read_csv(archivo, dtype=tipos, chunksize=BLOQUE)]
    if not partes:
        return pd.DataFrame({c: pd.Series(dtype=t) for c, t in tipos.items()})
    return pd.concat(partes, ignore_index=True)


def preparar_demo(base=None, datos=None, desde: str | None = None, salida=None,
                  corte: str | None = None, log=print) -> dict:
    """Prepara la carpeta del modo demo y devuelve un resumen (también en demo.json).

    base:   artefactos de 25M (por defecto config.ARTEFACTOS_BASE).
    datos:  carpeta de MovieLens (por defecto config.DATOS).
    desde:  inicio del subconjunto (por defecto config.DEMO_DESDE, variable DEMO_DESDE).
    salida: carpeta demo (por defecto config.ARTEFACTOS_DEMO).
    corte:  fin exclusivo del subconjunto (por defecto config.FECHA_CORTE): lo posterior
            al corte lo reproduce el productor como eventos en vivo.
    """
    base = Path(base or config.ARTEFACTOS_BASE)
    datos = Path(datos or config.DATOS)
    salida = Path(salida or config.ARTEFACTOS_DEMO)
    desde = desde or config.DEMO_DESDE
    corte = corte or config.FECHA_CORTE
    inicio = time.perf_counter()

    faltan = [n for n in OBLIGATORIOS if not (base / n).exists()]
    if faltan:
        raise FileNotFoundError(f"Faltan en {base}: {', '.join(faltan)}. "
                                "Corre antes scripts/entrenar_batch.py y perfilar_usuarios.py")
    if salida.resolve() == base.resolve():
        raise ValueError("La salida demo no puede ser la misma carpeta que la base")
    salida.mkdir(parents=True, exist_ok=True)

    copiados, ausentes = [], []
    for nombre in OBLIGATORIOS + OPCIONALES:
        if (base / nombre).exists():
            _copiar(base / nombre, salida / nombre)
            copiados.append(nombre)
        else:
            ausentes.append(nombre)
    if ausentes:
        log(f"Aviso: no están en la base (se omiten): {', '.join(ausentes)}")

    sub = ratings_subconjunto(datos, desde, corte)
    destino = salida / "base_demo.parquet"
    if destino.is_dir():
        shutil.rmtree(destino)
    sub.to_parquet(destino, index=False)

    # Si hubo una demo anterior, sus modelos versionados y checkpoints ya no aplican.
    for viejo in ("modelos", "checkpoints"):
        if (salida / viejo).exists():
            shutil.rmtree(salida / viejo, ignore_errors=True)

    resumen = {"base": str(base), "datos": str(datos), "desde": desde, "corte": corte,
               "n_base_demo": int(len(sub)), "n_usuarios": int(sub["userId"].nunique()),
               "n_peliculas": int(sub["movieId"].nunique()), "copiados": copiados,
               "segundos": round(time.perf_counter() - inicio, 2)}
    (salida / "demo.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2),
                                      encoding="utf-8")
    log(f"Demo lista en {salida}: {resumen['n_base_demo']:,} ratings entre {desde} y {corte} "
        f"({resumen['n_usuarios']:,} usuarios); copiados: {', '.join(copiados)}")
    return resumen
