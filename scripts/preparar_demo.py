"""Prepara el modo demo: artefactos de 25M + subconjunto desde DEMO_DESDE.

Ejemplos (desde la raíz del proyecto):

    .venv/Scripts/python scripts/preparar_demo.py
    .venv/Scripts/python scripts/preparar_demo.py --base artefactos/25m --datos data/ml-25m \
        --desde 2015-01-01 --salida artefactos/demo
    # Muestra mini (90 ratings entre 2015-01-01 y el corte 2016-01-01):
    .venv/Scripts/python scripts/preparar_demo.py --base artefactos/mini \
        --datos muestras/ml-mini --desde 2015-01-01 --corte 2016-01-01 --salida artefactos/demo_mini
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402
from recomendador.demo import preparar_demo  # noqa: E402


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--base", default=str(config.ARTEFACTOS_BASE),
                   help="artefactos precalculados sobre 25M (ARTEFACTOS_BASE)")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta de MovieLens")
    p.add_argument("--desde", default=config.DEMO_DESDE,
                   help="inicio del subconjunto AAAA-MM-DD (DEMO_DESDE, por defecto 2015-01-01)")
    p.add_argument("--corte", default=config.FECHA_CORTE,
                   help="fin exclusivo del subconjunto (FECHA_CORTE)")
    p.add_argument("--salida", default=str(config.ARTEFACTOS_DEMO), help="carpeta demo")
    return p


def main(argv: list[str] | None = None) -> int:
    a = construir_parser().parse_args(argv)
    try:
        preparar_demo(a.base, a.datos, a.desde, a.salida, a.corte)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
