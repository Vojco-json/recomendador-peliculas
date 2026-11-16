"""Capa batch de la app: genera con Spark los parquets que lee CineRuta.

    .venv/Scripts/python scripts/precalcular_app.py --perfil mini    # muestra (segundos)
    .venv/Scripts/python scripts/precalcular_app.py --perfil 25m     # datos completos

Escribe en artefactos/<perfil>/app/: peliculas.parquet, personas.parquet,
populares_por_tipo.parquet y vecinos.parquet (ver recomendador/precalculo.py).
Necesita que antes existan perfiles.parquet e item_factors.parquet del perfil
(entrenar_batch.py y perfilar_usuarios.py). Con --perfil 25m, si faltan title.crew,
title.principals o name.basics de IMDb en data/imdb, los descarga (uso personal y no
comercial); --sin-descargar lo evita (personas.parquet quedará vacío).
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config, precalculo, spark_utils  # noqa: E402

# Rutas y corte de cada perfil (se pueden sobrescribir con las opciones).
PERFILES = {
    "mini": dict(datos=config.MUESTRA_MINI, imdb=config.RAIZ / "muestras" / "imdb-mini",
                 artefactos=config.ARTEFACTOS / "mini", corte=config.FECHA_CORTE_MINI,
                 min_votos=0),
    "25m": dict(datos=config.DATOS, imdb=config.DATOS_IMDB, artefactos=config.ARTEFACTOS_BASE,
                corte=config.FECHA_CORTE, min_votos=20),
}


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--perfil", choices=sorted(PERFILES), default="25m")
    p.add_argument("--datos", help="carpeta de MovieLens (por defecto, la del perfil)")
    p.add_argument("--imdb", help="carpeta con los TSV de IMDb")
    p.add_argument("--artefactos", help="carpeta con perfiles.parquet e item_factors.parquet")
    p.add_argument("--corte", help="fecha de corte AAAA-MM-DD")
    p.add_argument("--min-votos-vecinos", type=int,
                   help="votos mínimos antes del corte para entrar a vecinos (LSH)")
    p.add_argument("--top-populares", type=int, default=precalculo.TOP_POPULARES)
    p.add_argument("--sin-descargar", action="store_true",
                   help="no descarga los archivos de personas de IMDb si faltan")
    return p


def _descargar_personas(imdb_dir: Path) -> None:
    """Baja title.crew/title.principals/name.basics si no están (solo datos completos)."""
    from recomendador import enriquecimiento

    faltan = [n for n in precalculo.ARCHIVOS_IMDB_PERSONAS
              if precalculo.archivo_imdb(imdb_dir, n.removesuffix(".tsv.gz")) is None]
    if faltan:
        print(enriquecimiento.ATRIBUCION_IMDB)
        enriquecimiento.descargar_imdb(imdb_dir, archivos=tuple(faltan))


def main(argv: list[str] | None = None) -> int:
    try:  # la consola de Windows puede no ser UTF-8
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    a = construir_parser().parse_args(argv)
    perfil = PERFILES[a.perfil]
    datos = Path(a.datos or perfil["datos"])
    imdb = Path(a.imdb or perfil["imdb"])
    artefactos = Path(a.artefactos or perfil["artefactos"])
    corte = a.corte or perfil["corte"]
    min_votos = perfil["min_votos"] if a.min_votos_vecinos is None else a.min_votos_vecinos

    for requerido in ("perfiles.parquet", "item_factors.parquet"):
        if not (artefactos / requerido).exists():
            print(f"Error: falta {artefactos / requerido}. Corre antes entrenar_batch.py y "
                  "perfilar_usuarios.py para este perfil.", file=sys.stderr)
            return 1
    if a.perfil != "mini" and not a.sin_descargar:
        try:
            _descargar_personas(imdb)
        except Exception as exc:  # sin internet: se sigue sin personas
            print(f"Aviso: no se pudieron descargar los archivos de personas de IMDb ({exc})")

    print(f"Precálculo de la app · perfil {a.perfil} · corte {corte} · datos {datos}")
    inicio = time.monotonic()
    spark = spark_utils.crear_spark_vigilado(app="precalcular-app")
    try:
        rutas = precalculo.ejecutar(spark, datos, artefactos, corte, imdb_dir=imdb,
                                    top_populares=a.top_populares,
                                    min_votos_vecinos=min_votos)
        for nombre, ruta in rutas.items():
            filas = spark.read.parquet(str(ruta)).count()
            print(f"  {nombre:<20} {filas:>10,} filas  ->  {ruta}")
    finally:
        spark_utils.detener_spark(spark)
    print(f"Listo en {time.monotonic() - inicio:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
