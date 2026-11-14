"""Experimento de escalabilidad: tiempo de ALS con 1, 2, 4 y 6 núcleos y speedup.

    .venv/Scripts/python scripts/escalabilidad.py                                    # ml-25m
    .venv/Scripts/python scripts/escalabilidad.py --nucleos 1,2,4,6 --repeticiones 3 --fraccion 0.5
    .venv/Scripts/python scripts/escalabilidad.py --datos muestras/ml-mini --nucleos 1,2 \
        --repeticiones 1 --salida artefactos/mini

Método:
- Para cada N se lanza un SUBPROCESO con su propia SparkSession ``local[N]`` y 6 GB de
  driver (dentro de un mismo proceso la JVM no puede cambiar de núcleos).
- En el subproceso se cargan los ratings (fracción determinista de usuarios), se
  cachean y materializan; luego se entrena ALS una vez de calentamiento (se descarta:
  incluye compilación JIT y carga de clases) y ``--repeticiones`` veces medidas.
- Speedup(N) = mediana(T₁) / mediana(Tₙ) (referencia: el menor N medido).

Salidas: ``<salida>/escalabilidad.json`` y ``escalabilidad.csv``, y el gráfico
``speedup.png`` (300 dpi) y ``speedup.svg`` en ``--figuras``. Por defecto las figuras
van a poster/figuras solo si se usan los datos completos (ml-25m); con otros datos
(p. ej. la muestra mini) van a ``<salida>/figuras`` para no pisar las del póster.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

MARCA = "RESULTADO_ESCALABILIDAD "   # prefijo de la línea JSON que imprime cada subproceso


def _reejecutar_en_venv() -> None:
    """Si este intérprete no tiene pyspark, reintenta con el Python del .venv."""
    try:
        import pyspark  # noqa: F401
        return
    except ImportError:
        pass
    venv = RAIZ / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if venv.exists() and Path(sys.executable).resolve() != venv.resolve():
        sys.exit(subprocess.call([str(venv), str(Path(__file__).resolve()), *sys.argv[1:]]))


def _lista_nucleos(texto: str) -> list[int]:
    try:
        ns = sorted({int(x) for x in texto.split(",") if x.strip()})
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"--nucleos no válido: {texto!r} (ej. 1,2,4,6)") from exc
    if not ns or ns[0] < 1:
        raise argparse.ArgumentTypeError("--nucleos debe tener enteros >= 1")
    disponibles = os.cpu_count() or 1
    if ns[-1] > disponibles:
        print(f"AVISO: se pidieron {ns[-1]} núcleos y el equipo informa {disponibles}",
              file=sys.stderr)
    return ns


def _fraccion(texto: str) -> float:
    valor = float(texto)
    if not 0 < valor <= 1:
        raise argparse.ArgumentTypeError("--fraccion debe estar en (0, 1]")
    return valor


def _positivo(texto: str) -> int:
    valor = int(texto)
    if valor < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o más")
    return valor


def crear_parser() -> argparse.ArgumentParser:
    from recomendador import config
    from recomendador.batch_als import PARAMS_DEFECTO

    p = argparse.ArgumentParser(description="Tiempos de ALS y speedup con 1..N núcleos.")
    p.add_argument("--datos", default=str(config.DATOS), help="carpeta MovieLens (%(default)s)")
    p.add_argument("--nucleos", type=_lista_nucleos, default=[1, 2, 4, 6],
                   help="lista de núcleos separada por comas (1,2,4,6)")
    p.add_argument("--repeticiones", type=_positivo, default=3,
                   help="corridas medidas por N, sin contar el calentamiento (%(default)s)")
    p.add_argument("--fraccion", type=_fraccion, default=1.0,
                   help="fracción determinista de usuarios (0-1] (%(default)s)")
    p.add_argument("--salida", default=str(config.ARTEFACTOS_BASE),
                   help="carpeta donde se guarda escalabilidad.json (%(default)s)")
    p.add_argument("--figuras", default=None,
                   help="carpeta del gráfico (por defecto poster/figuras con ml-25m, "
                        "o <salida>/figuras con otros datos)")
    p.add_argument("--rank", type=int, default=PARAMS_DEFECTO["rank"])
    p.add_argument("--iter", type=int, default=PARAMS_DEFECTO["maxIter"], help="maxIter")
    p.add_argument("--reg", type=float, default=PARAMS_DEFECTO["regParam"], help="regParam")
    p.add_argument("--plazo", type=float, default=4 * 3600,
                   help="segundos máximos por subproceso (%(default)s)")
    # Uso interno: modo subproceso (una sola N).
    p.add_argument("--_trabajador", type=int, default=None, help=argparse.SUPPRESS)
    return p


# --------------------------------------------------------------------------- #
# Subproceso: una SparkSession local[N], calentamiento + repeticiones
# --------------------------------------------------------------------------- #

def trabajador(args) -> int:
    from recomendador import batch_als
    from recomendador.datos import cargar_movielens
    from recomendador.spark_utils import crear_spark_vigilado, detener_spark

    n = args._trabajador
    params = {**batch_als.PARAMS_DEFECTO, "rank": args.rank, "maxIter": args.iter,
              "regParam": args.reg}
    t_arranque = time.perf_counter()
    spark = crear_spark_vigilado(plazo_s=60, intentos=3, nucleos=n, memoria="6g",
                                 app=f"escalabilidad-{n}")
    try:
        arranque = time.perf_counter() - t_arranque
        ratings = cargar_movielens(args.datos, spark, ["ratings"])["ratings"]
        ratings = batch_als.filtrar_fraccion(ratings, args.fraccion)
        ratings = ratings.select("userId", "movieId", "rating").cache()
        n_ratings = ratings.count()   # materializa la caché: la lectura no se mide
        if n_ratings == 0:
            raise ValueError("No hay ratings con esa fracción")

        def _una_corrida() -> float:
            t0 = time.perf_counter()
            modelo = batch_als.entrenar(ratings, params)
            modelo.userFactors.count()   # asegura que los factores estén calculados
            return time.perf_counter() - t0

        calentamiento = _una_corrida()
        print(f"  [N={n}] calentamiento {calentamiento:.2f} s (descartado)", flush=True)
        tiempos = []
        for r in range(args.repeticiones):
            tiempos.append(_una_corrida())
            print(f"  [N={n}] repetición {r + 1}/{args.repeticiones}: {tiempos[-1]:.2f} s",
                  flush=True)
        paralelismo = spark.sparkContext.defaultParallelism
        # El resultado se imprime ANTES de detener Spark: si el apagado de la JVM se
        # cuelga en Windows, el proceso principal ya tiene los tiempos.
        print(MARCA + json.dumps({"nucleos": n, "tiempos": tiempos,
                                  "calentamiento": calentamiento, "arranque_s": arranque,
                                  "n_ratings": n_ratings, "paralelismo": paralelismo}),
              flush=True)
    finally:
        detener_spark(spark)   # detención con plazo: mata la JVM si no termina
    return 0


PLAZO_CIERRE_S = 60   # segundos de espera al subproceso después de entregar su resultado


def _matar_subproceso(proc) -> None:
    """Mata el subproceso y su árbol (incluida la JVM de Spark en Windows)."""
    if os.name == "nt":
        subprocess.run(["taskkill", "/f", "/t", "/pid", str(proc.pid)],
                       capture_output=True, check=False)
    proc.kill()


def _correr_subproceso(args, n: int) -> dict:
    """Lanza este mismo script en modo trabajador para N núcleos y devuelve su resultado."""
    cmd = [sys.executable, str(Path(__file__).resolve()), "--_trabajador", str(n),
           "--datos", str(args.datos), "--repeticiones", str(args.repeticiones),
           "--fraccion", str(args.fraccion), "--rank", str(args.rank),
           "--iter", str(args.iter), "--reg", str(args.reg)]
    entorno = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace", env=entorno,
                            cwd=str(RAIZ))
    # Un hilo lee la salida; el hilo principal vigila el plazo aunque el subproceso
    # no imprima nada (antes el plazo solo se revisaba al llegar una línea).
    estado: dict = {"resultado": None, "cola": []}

    def _leer() -> None:
        assert proc.stdout is not None
        for linea in proc.stdout:
            if linea.startswith(MARCA):
                estado["resultado"] = json.loads(linea[len(MARCA):])
            elif linea.startswith("  [N="):
                print(linea.rstrip(), flush=True)
            else:   # se guardan las últimas líneas para diagnosticar si falla
                estado["cola"] = (estado["cola"] + [linea.rstrip()])[-25:]

    lector = threading.Thread(target=_leer, daemon=True)
    lector.start()
    limite = time.monotonic() + args.plazo
    limite_cierre = None   # tras recibir el resultado, margen para que cierre solo
    while proc.poll() is None:
        ahora = time.monotonic()
        if estado["resultado"] is not None and limite_cierre is None:
            limite_cierre = ahora + PLAZO_CIERRE_S
        if limite_cierre is not None and ahora > limite_cierre:
            print(f"  [N={n}] el subproceso no cerró tras entregar su resultado; "
                  "se termina a la fuerza", flush=True)
            _matar_subproceso(proc)
            break
        if ahora > limite:
            _matar_subproceso(proc)
            raise TimeoutError(f"N={n}: superó --plazo {args.plazo:.0f} s")
        time.sleep(0.5)
    lector.join(5)
    codigo = proc.wait()
    resultado = estado["resultado"]
    if resultado is None or (codigo != 0 and limite_cierre is None):
        raise RuntimeError(f"El subproceso con N={n} terminó con código {codigo}.\n"
                           + "\n".join(estado["cola"]))
    return resultado


# --------------------------------------------------------------------------- #
# Proceso principal
# --------------------------------------------------------------------------- #

def main(argv: list[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    datos = Path(args.datos)
    if not (datos / "ratings.csv").exists():
        print(f"ERROR: no existe {datos / 'ratings.csv'}. "
              "Descarga los datos con scripts/descargar_movielens.py", file=sys.stderr)
        return 1
    if args._trabajador is not None:
        return trabajador(args)

    from recomendador import config
    from recomendador.poster import calcular_speedup, figura_speedup

    salida = Path(args.salida)
    if args.figuras:
        figuras = Path(args.figuras)
    elif datos.resolve() == Path(config.DATOS).resolve():
        figuras = Path(config.FIGURAS)
    else:
        figuras = salida / "figuras"

    print(f"Escalabilidad ALS sobre {datos.name} · núcleos {args.nucleos} · "
          f"{args.repeticiones} repetición(es) + 1 de calentamiento · fracción {args.fraccion}")
    crudos = {}
    inicio = time.perf_counter()
    for n in args.nucleos:
        print(f"-> N={n}: subproceso con SparkSession local[{n}], driver 6g", flush=True)
        try:
            crudos[n] = _correr_subproceso(args, n)
        except (RuntimeError, TimeoutError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1

    filas = calcular_speedup({n: r["tiempos"] for n, r in crudos.items()})
    for f in filas:
        f["calentamiento_s"] = round(crudos[f["nucleos"]]["calentamiento"], 3)
    resultado = {
        "dataset": datos.name,
        "datos": str(datos.resolve()),
        "fraccion": args.fraccion,
        "n_ratings": crudos[args.nucleos[0]]["n_ratings"],
        "repeticiones": args.repeticiones,
        "calentamiento_descartado": True,
        "params": {"rank": args.rank, "maxIter": args.iter, "regParam": args.reg},
        "equipo": "Ryzen 5 3600 (6 núcleos), 16 GB RAM",
        "cpu_count": os.cpu_count(),
        "memoria_driver": "6g",
        "fecha": time.strftime("%Y-%m-%d %H:%M:%S"),
        "segundos_total": round(time.perf_counter() - inicio, 1),
        "resultados": filas,
    }
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "escalabilidad.json").write_text(json.dumps(resultado, ensure_ascii=False,
                                                          indent=2), encoding="utf-8")
    with open(salida / "escalabilidad.csv", "w", encoding="utf-8") as f:
        f.write("nucleos,mediana_s,media_s,desv_s,speedup,ideal,eficiencia\n")
        for r in filas:
            f.write(f"{r['nucleos']},{r['mediana_s']},{r['media_s']},{r['desv_s']},"
                    f"{r['speedup']},{r['ideal']},{r['eficiencia']}\n")
    rutas = figura_speedup(resultado, figuras)

    print()
    print(f"{'Núcleos':>8} {'Mediana (s)':>12} {'Speedup':>9} {'Ideal':>6} {'Eficiencia':>11}")
    for r in filas:
        print(f"{r['nucleos']:>8} {r['mediana_s']:>12.2f} {r['speedup']:>8.2f}x "
              f"{r['ideal']:>5.0f}x {100 * r['eficiencia']:>10.0f}%")
    print()
    print(f"Resultados en {(salida / 'escalabilidad.json').resolve()}")
    print("Gráfico: " + ", ".join(str(r.resolve()) for r in rutas))
    if datos.name != "ml-25m":
        print("Nota: con datos pequeños el speedup no es representativo (domina el "
              "costo fijo de Spark).")
    return 0


if __name__ == "__main__":
    _reejecutar_en_venv()
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
