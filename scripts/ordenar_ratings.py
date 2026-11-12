"""Ordena ratings.csv por timestamp con Spark y lo guarda como ratings_ordenados.parquet.

El productor Kafka lee este parquet por lotes, en orden, sin cargar los 25M de
ratings en memoria. El orden es (timestamp, userId, movieId) para que sea
determinista ante empates. Spark escribe un orderBy global en archivos
part-00000, part-00001... cuyo orden de nombre coincide con el orden de los datos.

    .venv/Scripts/python scripts/ordenar_ratings.py                       # data/ml-25m
    .venv/Scripts/python scripts/ordenar_ratings.py --datos muestras/ml-mini --salida artefactos/mini

--salida es una carpeta (se escribe <salida>/ratings_ordenados.parquet) o una ruta que
termina en .parquet. Por defecto, la misma carpeta de --datos.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402

NOMBRE = "ratings_ordenados.parquet"


def ruta_salida(salida: str | Path | None, datos: Path) -> Path:
    """Resuelve el destino: carpeta -> carpeta/ratings_ordenados.parquet."""
    if salida is None:
        return datos / NOMBRE
    salida = Path(salida)
    return salida if salida.suffix.lower() == ".parquet" else salida / NOMBRE


def ordenar(datos, salida=None, spark=None, archivos: int | None = None) -> tuple[Path, int]:
    """Lee ratings.csv de `datos`, ordena por timestamp y escribe el parquet.

    archivos: número de archivos de salida (por defecto 1 por cada ~2M de ratings).
    Devuelve (ruta del parquet, número de filas).
    """
    from recomendador.datos import cargar_movielens

    datos = Path(datos)
    destino = ruta_salida(salida, datos)
    if spark is None:
        from recomendador.spark_utils import crear_spark

        spark = crear_spark(app="ordenar-ratings")
    ratings = cargar_movielens(datos, spark, ["ratings"])["ratings"]
    n = ratings.count()
    archivos = archivos or max(1, n // 2_000_000)
    # repartitionByRange + sortWithinPartitions: cada archivo cubre un rango de
    # timestamps y part-0000k queda antes que part-0000(k+1).
    (ratings
     .repartitionByRange(archivos, "timestamp", "userId", "movieId")
     .sortWithinPartitions("timestamp", "userId", "movieId")
     .write.mode("overwrite").parquet(str(destino)))
    return destino, n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Ordena los ratings por timestamp (Spark -> parquet).")
    p.add_argument("--datos", default=str(config.DATOS),
                   help="carpeta con ratings.csv (%(default)s)")
    p.add_argument("--salida", default=None,
                   help="carpeta o ruta .parquet de salida (por defecto, la carpeta de --datos)")
    p.add_argument("--nucleos", type=int, default=None, help="núcleos de Spark local[N]")
    args = p.parse_args(argv)

    datos = Path(args.datos)
    if not (datos / "ratings.csv").exists():
        print(f"ERROR: no existe {datos / 'ratings.csv'}. "
              "Descarga los datos con scripts/descargar_movielens.py", file=sys.stderr)
        return 1
    try:
        from recomendador.spark_utils import crear_spark

        inicio = time.perf_counter()
        spark = crear_spark(nucleos=args.nucleos, app="ordenar-ratings")
        destino, n = ordenar(datos, args.salida, spark)
        spark.stop()
    except ImportError as exc:
        print(f"ERROR: falta una dependencia ({exc}). Ejecuta setup_entorno.ps1.",
              file=sys.stderr)
        return 1
    except Exception as exc:  # errores de la JVM (p. ej. falta hadoop.dll en Windows)
        primera = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
        print(f"ERROR: Spark no pudo ordenar los ratings: {primera}\n"
              "Revisa el entorno con: .venv/Scripts/python check_entorno.py "
              "(Java 17, HADOOP_HOME, winutils.exe y hadoop.dll).", file=sys.stderr)
        return 1
    print(f"Listo: {n:,} ratings ordenados por timestamp en {destino} "
          f"({time.perf_counter() - inicio:.1f} s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
