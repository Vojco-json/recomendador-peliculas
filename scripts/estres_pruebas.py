"""Corre suites de pytest varias veces seguidas para cazar fallas intermitentes.

Uso:
    .venv/Scripts/python scripts/estres_pruebas.py --veces 5 tests/test_entorno.py tests/test_batch.py

Cada archivo se ejecuta en su propio proceso (como lo hace Delegado). Al final
imprime los códigos de salida y cuántos arranques de Spark tuvieron que reintentarse
(según artefactos/pruebas_colgadas.log).
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
    args = parser.parse_args()

    tam_log = LOG.stat().st_size if LOG.exists() else 0
    fallas = 0
    for vuelta in range(1, args.veces + 1):
        for archivo in args.archivos:
            inicio = time.monotonic()
            proc = subprocess.run([sys.executable, "-m", "pytest", "-q", archivo],
                                  cwd=RAIZ, capture_output=True, text=True,
                                  encoding="utf-8", errors="replace")
            resumen = [l for l in proc.stdout.splitlines() if " passed" in l or " failed" in l]
            fallas += proc.returncode != 0
            print(f"vuelta {vuelta} {archivo}: código {proc.returncode} "
                  f"({time.monotonic() - inicio:.0f}s) {resumen[-1] if resumen else proc.stdout[-300:]}",
                  flush=True)

    nuevo = LOG.read_text(encoding="utf-8", errors="replace")[tam_log:] if LOG.exists() else ""
    print(f"\nCorridas con error: {fallas}. Intentos de arranque de Spark reintentados: "
          f"{nuevo.count('[arranque Spark]')}")
    for linea in nuevo.splitlines():
        if linea.startswith("[arranque Spark]"):
            print("  " + linea)
    return 1 if fallas else 0


if __name__ == "__main__":
    sys.exit(main())
