"""Imprime el top-5 de una lista de pedidos con los artefactos REALES (25M).

Uso (lo ejecuta el usuario a mano):
    python scripts/probar_pedidos.py [--usuario 1] [--datos RUTA] [--artefactos RUTA]
Con --datos muestras/ml-mini se usa la muestra mini (así lo prueba la suite).
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recomendador import llm  # noqa: E402
from recomendador.herramientas import Herramientas  # noqa: E402

PEDIDOS = [
    "drama policial con un twist épico",
    "algo como de drama policial con un twist épico",
    "una comedia romántica corta",
    "algo para llorar",
    "algo como El mecanismo",
    "thriller psicológico que me haga pensar",
]


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--usuario", type=int, default=1)
    ap.add_argument("--datos", default=None)
    ap.add_argument("--artefactos", default=None)
    args = ap.parse_args()
    herr = Herramientas(args.datos, args.artefactos)
    for pedido in PEDIDOS:
        a = llm.analizar(pedido, herr)
        res = llm.recomendar_detectado(args.usuario, herr, {**a, "n": 5})
        print(f"\n=== {pedido}")
        print(f"    géneros={a['generos']} tags={a['tags']} "
              f"duración={a['max_duracion_min']} {a['frase_titulo']}")
        for r in res["recomendaciones"]:
            print(f"  {r['rank']}. {r['titulo']} ({r.get('anio')}) | "
                  f"{'/'.join(r['generos'])} | votos={r.get('votos')} | "
                  f"nivel {r['nivel']}/{r['condiciones_pedidas']} | "
                  f"cumple: {'; '.join(r['cumple']) or '-'} | "
                  f"no: {'; '.join(r['no_cumple'] + r['no_evaluables']) or '-'}")


if __name__ == "__main__":
    main()
