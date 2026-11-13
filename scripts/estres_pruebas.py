"""Corre suites de pytest varias veces seguidas para cazar fallas intermitentes.

Uso:
    .venv/Scripts/python scripts/estres_pruebas.py --veces 5 tests/test_entorno.py tests/test_batch.py

Cada archivo se ejecuta en su propio proceso (como lo hace Delegado). Por defecto,
los archivos de una misma vuelta corren EN PARALELO (cada uno con su propia JVM y
sus artefactos en un directorio temporal): seis corridas secuenciales de Spark
suman ~160 s y rozaban el límite de 3 minutos del ejecutor; en paralelo cada vuelta
dura lo que tarda la suite más lenta. Con --secuencial se recupera el modo anterior.
Al final imprime los códigos de salida y cuántos arranques de Spark tuvieron que
reintentarse (según artefactos/pruebas_colgadas.log).
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
LOG = RAIZ / "artefactos" / "pruebas_colgadas.log"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("archivos", nargs="+", help="archivos de prueba")
    parser.add_argument("--veces", type=int, default=3)
    parser.add_argument("--secuencial", action="store_true",
                        help="corre los archivos uno tras otro (más lento)")
    args = parser.parse_args()

    tam_log = LOG.stat().st_size if LOG.exists() else 0
    fallas = 0
    for vuelta in range(1, args.veces + 1):
        grupos = [[a] for a in args.archivos] if args.secuencial else [args.archivos]
        for grupo in grupos:
            # Lanza todos los archivos del grupo a la vez y después espera a cada uno.
            procesos = []
            for archivo in grupo:
                procesos.append((archivo, time.monotonic(), subprocess.Popen(
                    [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", archivo],
                    cwd=RAIZ, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace")))
            for archivo, inicio, proc in procesos:
                salida, _ = proc.communicate()
                resumen = [l for l in salida.splitlines() if " passed" in l or " failed" in l]
                fallas += proc.returncode != 0
                print(f"vuelta {vuelta} {archivo}: código {proc.returncode} "
                      f"({time.monotonic() - inicio:.0f}s) "
                      f"{resumen[-1] if resumen else salida[-300:]}", flush=True)

    nuevo = LOG.read_text(encoding="utf-8", errors="replace")[tam_log:] if LOG.exists() else ""
    print(f"\nCorridas con error: {fallas}. Intentos de arranque de Spark reintentados: "
          f"{nuevo.count('[arranque Spark]')}")
    for linea in nuevo.splitlines():
        if linea.startswith("[arranque Spark]"):
            print("  " + linea)
    return 1 if fallas else 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # tildes en Windows
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
