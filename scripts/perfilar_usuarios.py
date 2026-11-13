"""Perfiles de gusto y tipos de espectador (KMeans de MLlib).

    .venv/Scripts/python scripts/perfilar_usuarios.py                      # ml-25m, k=8
    .venv/Scripts/python scripts/perfilar_usuarios.py --datos muestras/ml-mini \
        --salida artefactos/mini --k 3

Calcula, por usuario, los pesos por género y por unos 40 tags del genome
(peso = rating - media del usuario), agrupa con KMeans sobre las features
estandarizadas más log(1 + n_ratings) y nombra cada tipo en español. Guarda en
--salida: perfiles.parquet y tipos.json, e imprime la tabla de tipos.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def _reejecutar_en_venv() -> None:
    """Si este intérprete no tiene pyspark, reintenta con el Python del .venv del proyecto."""
    try:
        import pyspark  # noqa: F401
        return
    except ImportError:
        pass
    venv = RAIZ / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        sys.exit(subprocess.call([str(venv), str(Path(__file__).resolve()), *sys.argv[1:]]))


def _fecha(texto: str) -> str:
    try:
        return date.fromisoformat(texto).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"fecha no válida {texto!r} (usa AAAA-MM-DD)") from exc


def _k(texto: str) -> int:
    valor = int(texto)
    if valor < 2:
        raise argparse.ArgumentTypeError("--k debe ser 2 o más")
    return valor


def crear_parser() -> argparse.ArgumentParser:
    from recomendador import config
    from recomendador.perfiles import K_DEFECTO, SEMILLA

    p = argparse.ArgumentParser(description="Perfiles de gusto y tipos de espectador.")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta MovieLens (%(default)s)")
    p.add_argument("--salida", default=str(config.ARTEFACTOS_BASE),
                   help="carpeta de artefactos (%(default)s)")
    p.add_argument("--k", type=_k, default=K_DEFECTO,
                   help="número de tipos de espectador (%(default)s; 3 en ml-mini)")
    p.add_argument("--seed", type=int, default=SEMILLA, help="semilla de KMeans")
    p.add_argument("--nucleos", type=int, default=None, help="núcleos de Spark local[N]")
    p.add_argument("--hasta", type=_fecha, default=None,
                   help="usa solo ratings anteriores a esta fecha (por defecto, todos)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    datos = Path(args.datos)
    for archivo in ("ratings.csv", "movies.csv"):
        if not (datos / archivo).exists():
            print(f"ERROR: no existe {datos / archivo}. "
                  "Descarga los datos con scripts/descargar_movielens.py", file=sys.stderr)
            return 1

    from recomendador import perfiles
    from recomendador.datos import cargar_movielens
    from recomendador.spark_utils import crear_spark_vigilado

    # Arranque vigilado: si la JVM queda colgada al lanzarse (falla intermitente de
    # pyspark en Windows), se aborta y se reintenta en vez de bloquear el script.
    spark = crear_spark_vigilado(plazo_s=60, nucleos=args.nucleos, app="perfilar-usuarios")
    try:
        nombres = ["ratings", "movies"]
        if (datos / "genome-tags.csv").exists() and (datos / "genome-scores.csv").exists():
            nombres += ["genome_tags", "genome_scores"]
        tablas = cargar_movielens(datos, spark, nombres)
        res = perfiles.ejecutar(spark, tablas, args.salida, k=args.k, seed=args.seed,
                                hasta=args.hasta)
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        spark.stop()

    print()
    print(f"Tipos de espectador (k={args.k}, seed={args.seed}, "
          f"{res['n_usuarios']:,} usuarios):")
    print(perfiles.tabla_tipos(res))
    print()
    print(f"Listo en {res['segundos']:.1f} s. Artefactos en {Path(args.salida).resolve()} "
          "(perfiles.parquet, tipos.json)")
    return 0


if __name__ == "__main__":
    _reejecutar_en_venv()
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
