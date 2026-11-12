"""Instalador del entorno en Python puro (equivalente a setup_entorno.ps1).

Existe porque algunos lanzadores no permiten abrir un PowerShell anidado
(powershell -File ...). Hace exactamente lo mismo que el .ps1:

    1. crea .venv con 'py -3.12 -m venv .venv' (o con este mismo intérprete si ya es 3.12),
    2. instala requirements.txt (versiones fijadas, pyspark 3.5.x),
    3. verifica que pyspark sea 3.5.x,
    4. (opcional) descarga el conector Kafka de Spark a .ivy2,
    5. ejecuta check_entorno.py y deja el registro en artefactos/instalacion.log.

Uso (desde la carpeta del proyecto):
    py -3.12 setup_entorno.py                 # instala todo
    py -3.12 setup_entorno.py --sin-conector  # sin descargar el conector Kafka
    py -3.12 setup_entorno.py --recrear       # borra y recrea .venv
"""

from __future__ import annotations

import argparse
import datetime as dt
import shutil
import subprocess
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
VENV = RAIZ / ".venv"
PY_VENV = VENV / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")
LOG = RAIZ / "artefactos" / "instalacion.log"

for _flujo in (sys.stdout, sys.stderr):
    try:
        _flujo.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def log(msg: str) -> None:
    """Imprime y agrega al registro de instalación (evidencia para revisar)."""
    print(msg, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(msg + "\n")


def ejecutar(descripcion: str, comando: list[str]) -> str:
    """Ejecuta un comando, guarda su salida en el log y aborta si falla."""
    log(f"==> {descripcion}: {' '.join(comando)}")
    proc = subprocess.run(comando, cwd=RAIZ, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    salida = (proc.stdout or "") + (proc.stderr or "")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(salida + "\n")
    if proc.returncode != 0:
        print(salida[-3000:])
        log(f"FALLO: {descripcion} (codigo {proc.returncode})")
        sys.exit(proc.returncode)
    return salida


def comando_python312() -> list[str]:
    """Devuelve cómo invocar Python 3.12: este intérprete si ya lo es, si no 'py -3.12'."""
    if sys.version_info[:2] == (3, 12):
        return [sys.executable]
    if shutil.which("py"):
        return ["py", "-3.12"]
    log("No se encontró Python 3.12 ni el lanzador 'py'. Instálalo desde python.org.")
    sys.exit(1)


def main() -> int:
    p = argparse.ArgumentParser(description="Crea .venv (Python 3.12) e instala requirements.txt")
    p.add_argument("--recrear", action="store_true", help="borra .venv y lo crea de nuevo")
    p.add_argument("--sin-conector", action="store_true", help="no descarga el conector Kafka")
    args = p.parse_args()

    LOG.parent.mkdir(parents=True, exist_ok=True)
    LOG.write_text(f"Instalación {dt.datetime.now():%Y-%m-%d %H:%M:%S}\n", encoding="utf-8")

    py312 = comando_python312()
    ejecutar("Verificando Python 3.12", py312 + ["--version"])

    if args.recrear and VENV.exists():
        log("==> Borrando .venv existente")
        shutil.rmtree(VENV)
    if not PY_VENV.exists():
        ejecutar("Creando .venv (py -3.12 -m venv .venv)", py312 + ["-m", "venv", str(VENV)])
    else:
        log("==> .venv ya existe (usa --recrear para rehacerlo)")

    v = ejecutar("Versión del .venv", [str(PY_VENV), "--version"])
    if "3.12" not in v:
        log(f"El .venv no es Python 3.12 ({v.strip()}); ejecuta con --recrear")
        return 1

    ejecutar("Actualizando pip", [str(PY_VENV), "-m", "pip", "install", "--upgrade", "pip"])
    ejecutar("Instalando requirements.txt",
             [str(PY_VENV), "-m", "pip", "install", "-r", str(RAIZ / "requirements.txt")])
    ejecutar("Verificando pyspark 3.5.x", [
        str(PY_VENV), "-c",
        "import pyspark,sys; v=pyspark.__version__; print('pyspark', v); "
        "sys.exit(0 if v.startswith('3.5.') else 1)"])

    if not args.sin_conector:
        # No aborta si falla (sin internet o sin Java): check_entorno lo informará.
        log("==> Descargando conector spark-sql-kafka a .ivy2 (requiere Java 17 e internet)")
        subprocess.run([str(PY_VENV), str(RAIZ / "check_entorno.py"), "--descargar-conector"],
                       cwd=RAIZ, capture_output=True)

    ejecutar("Paquetes instalados (pip freeze)", [str(PY_VENV), "-m", "pip", "freeze"])
    reporte = ejecutar("Verificación del entorno", [str(PY_VENV), str(RAIZ / "check_entorno.py")])
    print(reporte)
    log(f"Listo. Registro completo en {LOG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
