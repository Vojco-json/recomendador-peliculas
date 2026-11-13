"""Descarga los datasets no comerciales de IMDb y genera peliculas_enriquecidas.parquet.

    .venv/Scripts/python scripts/descargar_imdb.py                     # descarga + cruce 25M
    .venv/Scripts/python scripts/descargar_imdb.py --solo-descargar
    # Cruce con la muestra (sin red):
    .venv/Scripts/python scripts/descargar_imdb.py --sin-descargar --imdb muestras/imdb-mini \
        --datos muestras/ml-mini --salida artefactos/mini

Baja title.basics.tsv.gz y title.ratings.tsv.gz de https://datasets.imdbws.com/ a
data/imdb (descarga directa de los archivos oficiales, sin scraping). Luego une por
links.csv (imdbId -> 'tt' + 7 dígitos = tconst) y escribe en --salida
peliculas_enriquecidas.parquet con duración, año y nota IMDb.
Licencia: los datos de IMDb son solo para uso personal y no comercial.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config, enriquecimiento  # noqa: E402


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--imdb", default=str(enriquecimiento.DIR_IMDB),
                   help="carpeta de los TSV de IMDb (por defecto data/imdb)")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta de MovieLens")
    p.add_argument("--salida", default=str(config.ARTEFACTOS_BASE),
                   help="carpeta donde escribir peliculas_enriquecidas.parquet")
    p.add_argument("--forzar", action="store_true", help="vuelve a descargar aunque existan")
    grupo = p.add_mutually_exclusive_group()
    grupo.add_argument("--solo-descargar", action="store_true", help="no hace el cruce")
    grupo.add_argument("--sin-descargar", action="store_true",
                       help="solo hace el cruce con los TSV ya presentes")
    return p


def main(argv: list[str] | None = None) -> int:
    try:  # la consola de Windows puede no ser UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    a = construir_parser().parse_args(argv)
    print(enriquecimiento.ATRIBUCION_IMDB)
    try:
        if not a.sin_descargar:
            enriquecimiento.descargar_imdb(a.imdb, forzar=a.forzar)
        if a.solo_descargar:
            return 0
        df = enriquecimiento.enriquecer_imdb(a.datos, a.imdb, a.salida)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # p. ej. sin internet
        print(f"Error al descargar o cruzar IMDb: {exc}", file=sys.stderr)
        return 1
    r = enriquecimiento.resumen_enriquecimiento(df)
    print(f"Películas: {r['peliculas']:,} · con duración IMDb: {r['con_duracion']:,} · "
          f"con nota IMDb: {r['con_nota_imdb']:,}")
    print(f"Escrito: {Path(a.salida) / enriquecimiento.NOMBRE_ENRIQUECIDAS}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
