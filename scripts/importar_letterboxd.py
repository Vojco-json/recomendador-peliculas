"""Importa la exportación de Letterboxd (ratings.csv) como un usuario nuevo.

    .venv/Scripts/python scripts/importar_letterboxd.py --csv ratings.csv
    # Muestra (crea los artefactos de ALS y perfiles si faltan):
    .venv/Scripts/python scripts/importar_letterboxd.py --csv muestras/letterboxd_ratings.csv \
        --datos muestras/ml-mini --artefactos artefactos/mini_letterboxd

El CSV debe tener las columnas Date, Name, Year, Letterboxd URI y Rating (se exporta
desde letterboxd.com > Settings > Import & Export). Cada película se empareja con
movies.csv por título normalizado y año (±1). Se crea un userId >= 1.000.000, se calcula
su perfil de gusto (géneros, tags y tipo de espectador) y sus recomendaciones por fold-in
sobre los factores de ALS, sin reentrenar. Todo queda en <artefactos>/letterboxd/.

Si faltan item_factors.parquet o perfiles en --artefactos, se entrenan con Spark sobre
todos los ratings de --datos (rápido en la muestra; con 25M usa los artefactos ya hechos).
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
    p.add_argument("--csv", required=True, help="ratings.csv exportado de Letterboxd")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta de MovieLens")
    p.add_argument("--artefactos", default=str(config.artefactos_activos()),
                   help="carpeta con item_factors.parquet (y perfiles/tipos)")
    p.add_argument("--n", type=int, default=enriquecimiento.N_RECS_LETTERBOXD,
                   help="número de recomendaciones")
    p.add_argument("--usuario", type=int, default=None,
                   help="userId a usar (>= 1000000); por defecto uno nuevo")
    p.add_argument("--estado", default=None,
                   help="si se indica, copia el usuario a ese estado.db (p. ej. "
                        "artefactos/estado.db) para verlo en la app")
    p.add_argument("--k", type=int, default=None,
                   help="tipos de KMeans si hay que crear los perfiles (auto: 3 en la "
                        "muestra, 8 si hay muchos usuarios)")
    return p


def crear_artefactos_si_faltan(datos: Path, artefactos: Path, k: int | None) -> None:
    """Entrena ALS (todos los ratings) y perfiles con Spark si no existen en ``artefactos``."""
    import json

    falta_als = not (artefactos / "item_factors.parquet").exists()
    faltan_perfiles = not (artefactos / "tipos.json").exists()
    if not (falta_als or faltan_perfiles):
        return
    from recomendador import batch_als, perfiles
    from recomendador.datos import cargar_movielens
    from recomendador.spark_utils import crear_spark_vigilado

    print(f"Faltan artefactos en {artefactos}: se crean con Spark sobre {datos} ...")
    artefactos.mkdir(parents=True, exist_ok=True)
    spark = crear_spark_vigilado(plazo_s=30, nucleos=2, app="importar-letterboxd",
                                 extra={"spark.ui.enabled": "false"})
    try:
        spark.conf.set("spark.sql.execution.arrow.pyspark.enabled", "true")
        tablas = cargar_movielens(datos, spark)
        if falta_als:
            modelo = batch_als.entrenar(tablas["ratings"])
            items, _ = batch_als.factores(modelo)
            items.toPandas().to_parquet(artefactos / "item_factors.parquet", index=False)
            (artefactos / "metricas.json").write_text(
                json.dumps({"params": batch_als.PARAMS_DEFECTO,
                            "nota": "ALS con todos los ratings (importar_letterboxd)"}),
                encoding="utf-8")
            print("  ALS entrenado: item_factors.parquet")
        if faltan_perfiles:
            if k is None:
                n_usuarios = tablas["ratings"].select("userId").distinct().count()
                k = 3 if n_usuarios < 1000 else perfiles.K_DEFECTO
            perfiles.ejecutar(spark, tablas, artefactos, k=k, log=lambda *_: None)
            print(f"  Perfiles y tipos de espectador (k={k}): perfiles.parquet, tipos.json")
    finally:
        spark.stop()


def imprimir(res: dict) -> None:
    print(f"Emparejadas: {res['n_emparejadas']} de {res['total']}")
    for f in res["no_emparejadas"]:
        anio = f" ({f['anio']})" if f["anio"] else ""
        print(f"  sin pareja en MovieLens: {f['titulo']}{anio}")
    print(f"userId nuevo: {res['userId']}")
    perfil = res["perfil"]
    if perfil:
        if perfil.get("tipo"):
            print(f"Tipo de espectador: {perfil['tipo']}")
        if perfil.get("top_generos_es"):
            print(f"Géneros favoritos: {', '.join(perfil['top_generos_es'])}")
    if res["recomendaciones"]:
        print("Recomendaciones (fold-in sobre ALS):")
        for r in res["recomendaciones"]:
            anio = f" ({r['anio']})" if r.get("anio") else ""
            print(f"  {r['rank']:>2}. {r.get('titulo') or r['movieId']}{anio}"
                  f"  · puntaje {r['score']:.2f}")


def main(argv: list[str] | None = None) -> int:
    try:  # la consola de Windows puede no ser UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    a = construir_parser().parse_args(argv)
    datos, artefactos = Path(a.datos), Path(a.artefactos)
    try:
        if not Path(a.csv).exists():
            raise FileNotFoundError(f"No existe el CSV {a.csv}")
        if not (datos / "movies.csv").exists():
            raise FileNotFoundError(f"No existe {datos / 'movies.csv'}")
        enriquecimiento.leer_letterboxd(a.csv)       # valida columnas antes de usar Spark
        crear_artefactos_si_faltan(datos, artefactos, a.k)
        res = enriquecimiento.importar_letterboxd(a.csv, datos, artefactos, n=a.n,
                                                  user_id=a.usuario)
        if a.estado:
            enriquecimiento.guardar_en_estado(res, a.estado)
    except (FileNotFoundError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    imprimir(res)
    print(f"Guardado en "
          f"{artefactos / enriquecimiento.DIR_LETTERBOXD / ('usuario_%d.json' % res['userId'])}")
    if a.estado:
        print(f"Copiado al estado en vivo: {a.estado}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
