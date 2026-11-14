"""Entrena ALS con partición temporal y lo compara con la popularidad.

    .venv/Scripts/python scripts/entrenar_batch.py                       # ml-25m, corte 2018-01-01
    .venv/Scripts/python scripts/entrenar_batch.py --datos muestras/ml-mini --corte 2016-01-01 \
        --salida artefactos/mini --nucleos 2
    .venv/Scripts/python scripts/entrenar_batch.py --desde 2015-01-01 --salida artefactos/demo_base

Entrenamiento = ratings anteriores a --corte (y, con --desde, solo los de esa fecha en
adelante: lo usa el modo demo). Prueba = ratings desde el corte de usuarios y películas
vistos en el entrenamiento. Imprime la tabla RMSE / Precision@10 / NDCG@10 y guarda en
--salida: metricas.json, modelo_als/, item_factors.parquet, user_factors.parquet y
recs_top20.parquet.
"""

from __future__ import annotations

import argparse
import math
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def _reejecutar_en_venv() -> None:
    """Si este intérprete no tiene pyspark, reintenta con el Python del .venv del proyecto.

    En Windows, una ruta relativa '.venv/Scripts/python' lanzada desde otro proceso
    puede resolverse contra el directorio del proceso padre (ver pytest.py).
    """
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


def _fraccion(texto: str) -> float:
    valor = float(texto)
    if not 0 < valor <= 1:
        raise argparse.ArgumentTypeError("--fraccion debe estar en (0, 1]")
    return valor


def crear_parser() -> argparse.ArgumentParser:
    from recomendador import config
    from recomendador.batch_als import MIN_VOTOS, PARAMS_DEFECTO

    p = argparse.ArgumentParser(description="ALS batch con partición temporal y métricas.")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta MovieLens (%(default)s)")
    p.add_argument("--corte", type=_fecha, default=config.FECHA_CORTE,
                   help="fecha de corte AAAA-MM-DD (%(default)s)")
    p.add_argument("--salida", default=str(config.ARTEFACTOS_BASE),
                   help="carpeta de artefactos (%(default)s)")
    p.add_argument("--nucleos", type=int, default=None, help="núcleos de Spark local[N]")
    p.add_argument("--fraccion", type=_fraccion, default=None,
                   help="fracción de usuarios a usar (0-1], determinista")
    p.add_argument("--desde", type=_fecha, default=None,
                   help="descarta ratings de entrenamiento anteriores a esta fecha (modo demo)")
    p.add_argument("--rank", type=int, default=PARAMS_DEFECTO["rank"])
    p.add_argument("--reg", type=float, default=PARAMS_DEFECTO["regParam"], help="regParam")
    p.add_argument("--iter", type=int, default=PARAMS_DEFECTO["maxIter"], help="maxIter")
    p.add_argument("--seed", type=int, default=PARAMS_DEFECTO["seed"])
    p.add_argument("--min-votos", type=int, default=MIN_VOTOS,
                   help="mínimo de votos para la lista de popularidad (%(default)s)")
    p.add_argument("--relevante", type=float, default=config.RELEVANTE_MIN,
                   help="rating mínimo para considerar relevante una película (%(default)s)")
    lista = lambda tipo: (lambda t: [tipo(x) for x in t.split(",") if x.strip()])  # noqa: E731
    p.add_argument("--ranks-implicito", type=lista(int), default=[50, 100],
                   help="ranks del ALS implícito a validar, separados por coma (50,100)")
    p.add_argument("--alphas", type=lista(float),
                   default=[float(x) for x in os.environ.get("ALS_ALPHA", "10,40").split(",")],
                   help="valores de alpha del ALS implícito (env ALS_ALPHA; 10,40)")
    p.add_argument("--pesos-hibrido", type=lista(float), default=[0.3, 0.5, 0.7],
                   help="pesos del ALS en el híbrido ALS+popularidad (0.3,0.5,0.7)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    datos = Path(args.datos)
    if not (datos / "ratings.csv").exists():
        print(f"ERROR: no existe {datos / 'ratings.csv'}. "
              "Descarga los datos con scripts/descargar_movielens.py", file=sys.stderr)
        return 1
    if args.desde and args.desde >= args.corte:
        print(f"ERROR: --desde {args.desde} debe ser anterior a --corte {args.corte}",
              file=sys.stderr)
        return 2

    from recomendador import batch_als
    from recomendador.datos import cargar_movielens
    from recomendador.spark_utils import crear_spark

    params = {"rank": args.rank, "regParam": args.reg, "maxIter": args.iter, "seed": args.seed}
    spark = crear_spark(nucleos=args.nucleos, app="entrenar-batch")
    try:
        ratings = cargar_movielens(datos, spark, ["ratings"])["ratings"]
        res = batch_als.ejecutar(spark, ratings, args.corte, args.salida, params,
                                 desde=args.desde, fraccion=args.fraccion,
                                 min_votos=args.min_votos, relevante_min=args.relevante,
                                 grilla={"rank": args.ranks_implicito,
                                         "alpha": args.alphas,
                                         "peso": args.pesos_hibrido})
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        from recomendador.spark_utils import detener_spark
        detener_spark(spark)   # con plazo: no se cuelga al apagar la JVM

    print()
    print(batch_als.tabla_metricas(res))
    print()
    valores = [res[m][c] for m in ("als", "popularidad")
               for c in ("RMSE", "Precision@10", "NDCG@10")]
    if any(math.isnan(v) for v in valores):
        print("ERROR: hay métricas NaN", file=sys.stderr)
        return 1
    print(f"Usuarios evaluados: {res['als']['usuarios_evaluados']} · n_train={res['n_train']:,} "
          f"· n_test={res['n_test']:,} · {res['segundos']:.1f} s")
    print(f"Artefactos en {Path(args.salida).resolve()}")
    return 0


if __name__ == "__main__":
    _reejecutar_en_venv()
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
