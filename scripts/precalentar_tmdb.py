"""Precalienta TMDb para la app: póster, título y sinopsis de las películas con 20 o más
votos antes del corte, en artefactos/25m/app/tmdb.parquet.

Lo corre el usuario (necesita TMDB_API_KEY e internet):
    .venv/Scripts/python -m scripts.precalentar_tmdb [--min-votos 20] [--hilos 8]

- 8 hilos en paralelo; ante un 429 espera (Retry-After o 2^intento s) y reintenta.
- Español (es-ES) con respaldo en inglés (en-US) si falta la sinopsis o el póster.
- Reanudable: las películas que ya están en el parquet no se vuelven a pedir.
La app (recomendador/tmdb.py) consulta este parquet antes que la red.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402
from recomendador.enriquecimiento import URL_POSTER_TMDB, URL_TMDB  # noqa: E402

COLUMNAS = ["tmdbId", "movieId", "titulo", "sinopsis", "idioma_sinopsis", "poster_url"]


def pedir(sesion, api_key: str, tmdb_id: int, idioma: str, reintentos: int = 5,
          dormir=time.sleep, timeout: float = 10.0) -> dict | None:
    """GET /3/movie/{id}: JSON, {} si es 404, None si falla tras los reintentos."""
    params, cab = {"language": idioma}, {"accept": "application/json"}
    if api_key.startswith("eyJ"):
        cab["Authorization"] = f"Bearer {api_key}"
    else:
        params["api_key"] = api_key
    for intento in range(reintentos):
        try:
            r = sesion.get(URL_TMDB.format(tmdb_id=tmdb_id), params=params, headers=cab,
                           timeout=timeout)
        except Exception:  # red: se reintenta
            dormir(2 ** intento)
            continue
        if r.status_code == 200:
            return r.json()
        if r.status_code == 404:
            return {}
        if r.status_code == 429 or r.status_code >= 500:
            espera = (getattr(r, "headers", None) or {}).get("Retry-After")
            dormir(float(espera) if espera else 2 ** intento)
            continue
        return None
    return None


def ficha(sesion, api_key: str, tmdb_id: int, movie_id: int, **kw) -> dict | None:
    """Ficha en español con respaldo en inglés; None si no se pudo obtener."""
    es = pedir(sesion, api_key, tmdb_id, "es-ES", **kw)
    if es is None:
        return None
    sinopsis, idioma = (es.get("overview") or "").strip(), "es-ES"
    poster, titulo = es.get("poster_path"), es.get("title")
    if es and (not sinopsis or not poster):
        en = pedir(sesion, api_key, tmdb_id, "en-US", **kw) or {}
        if not sinopsis and (en.get("overview") or "").strip():
            sinopsis, idioma = en["overview"].strip(), "en-US"
        poster, titulo = poster or en.get("poster_path"), titulo or en.get("title")
    return {"tmdbId": int(tmdb_id), "movieId": int(movie_id), "titulo": titulo,
            "sinopsis": sinopsis or None, "idioma_sinopsis": idioma if sinopsis else None,
            "poster_url": URL_POSTER_TMDB.format(ruta=poster) if poster else None}


def candidatas(peliculas_parquet, min_votos: int = 20) -> list[tuple[int, int]]:
    """[(movieId, tmdbId)] con >= min_votos antes del corte (peliculas.parquet del batch)."""
    import pandas as pd

    df = pd.read_parquet(peliculas_parquet, columns=["movieId", "tmdbId", "votos"])
    df = df[(df["votos"] >= min_votos) & df["tmdbId"].notna()]
    df = df.sort_values("votos", ascending=False)
    return [(int(m), int(t)) for m, t in zip(df["movieId"], df["tmdbId"])]


def precalentar(pares, api_key: str, salida, sesion=None, hilos: int = 8,
                dormir=time.sleep) -> int:
    """Descarga en paralelo lo que falte y reescribe el parquet. Devuelve cuántas agregó."""
    import pandas as pd

    salida = Path(salida)
    previas = pd.read_parquet(salida) if salida.exists() else pd.DataFrame(columns=COLUMNAS)
    ya = set(int(x) for x in previas["tmdbId"])
    pendientes = [(m, t) for m, t in pares if t not in ya]
    if sesion is None:
        import requests

        sesion = requests.Session()
    with ThreadPoolExecutor(max_workers=hilos) as ex:
        nuevas = [f for f in ex.map(lambda p: ficha(sesion, api_key, p[1], p[0], dormir=dormir),
                                    pendientes) if f]
    if nuevas:
        df = pd.concat([previas, pd.DataFrame(nuevas, columns=COLUMNAS)], ignore_index=True)
        salida.parent.mkdir(parents=True, exist_ok=True)
        tmp = salida.with_suffix(".tmp.parquet")
        df.to_parquet(tmp, index=False)
        tmp.replace(salida)                    # escritura atómica
    return len(nuevas)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--artefactos", default=str(config.ARTEFACTOS_BASE))
    ap.add_argument("--min-votos", type=int, default=20)
    ap.add_argument("--hilos", type=int, default=8)
    args = ap.parse_args(argv)
    api_key = os.environ.get("TMDB_API_KEY")
    if not api_key:
        print("Falta TMDB_API_KEY en el entorno.")
        return 1
    app = Path(args.artefactos) / "app"
    pares = candidatas(app / "peliculas.parquet", args.min_votos)
    print(f"{len(pares)} películas con >= {args.min_votos} votos antes del corte.")
    n = precalentar(pares, api_key, app / "tmdb.parquet", hilos=args.hilos)
    print(f"Listo: {n} fichas nuevas en {app / 'tmdb.parquet'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
