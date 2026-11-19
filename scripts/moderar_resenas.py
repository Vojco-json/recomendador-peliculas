"""Moderación LOCAL de reseñas: el único camino para ocultar o volver a mostrar una reseña.

La app no tiene ningún control de moderación. Este script se corre en el computador que
aloja estado.db y solo cambia el flag ``oculta`` (la reseña no se borra).

Ejemplos (desde la raíz del proyecto):

    .venv/Scripts/python scripts/moderar_resenas.py --listar
    .venv/Scripts/python scripts/moderar_resenas.py --listar --pelicula 1 --limite 20
    .venv/Scripts/python scripts/moderar_resenas.py --ocultar 12
    .venv/Scripts/python scripts/moderar_resenas.py --mostrar 12
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config, estado  # noqa: E402


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Moderación local de las reseñas de MoviePredict.")
    accion = p.add_mutually_exclusive_group(required=True)
    accion.add_argument("--listar", action="store_true", help="lista las reseñas")
    accion.add_argument("--ocultar", type=int, metavar="ID", help="oculta la reseña ID")
    accion.add_argument("--mostrar", type=int, metavar="ID",
                        help="vuelve a mostrar la reseña ID")
    p.add_argument("--estado", default=str(config.ESTADO_DB),
                   help="ruta de estado.db (%(default)s)")
    p.add_argument("--limite", type=int, default=50, help="máximo a listar (%(default)s)")
    p.add_argument("--pelicula", type=int, help="con --listar: solo esta movieId")
    p.add_argument("--solo-visibles", action="store_true",
                   help="con --listar: omite las ocultas")
    return p


def _linea(r: dict) -> str:
    fecha = datetime.fromtimestamp(int(r["timestamp"])).strftime("%Y-%m-%d %H:%M")
    marca = "OCULTA " if r["oculta"] else "visible"
    texto = " ".join(str(r["texto"]).split())
    texto = texto if len(texto) <= 90 else texto[:87] + "..."
    return (f"{r['id']:>6}  {marca}  {fecha}  usuario {r['userId']:<7} "
            f"película {r['movieId']:<7} {texto}")


def main(argv: list[str] | None = None) -> int:
    a = construir_parser().parse_args(argv)
    con = estado.conectar(a.estado)
    try:
        if a.listar:
            filas = estado.listar_resenas(con, a.limite if a.pelicula is None else 10**9,
                                          incluir_ocultas=not a.solo_visibles)
            if a.pelicula is not None:
                filas = [r for r in filas if r["movieId"] == a.pelicula][:a.limite]
            if not filas:
                print("No hay reseñas.")
            for r in filas:
                print(_linea(r))
            return 0
        id_resena, ocultar = (a.ocultar, True) if a.ocultar is not None else (a.mostrar, False)
        if not estado.marcar_oculta(con, id_resena, ocultar):
            print(f"No existe la reseña {id_resena}.", file=sys.stderr)
            return 1
        print(f"Reseña {id_resena} {'oculta' if ocultar else 'visible de nuevo'}.")
        return 0
    finally:
        con.close()


if __name__ == "__main__":
    sys.exit(main())
