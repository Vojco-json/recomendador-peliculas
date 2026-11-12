"""Verifica el entorno del proyecto y dice qué falta, con una sugerencia para cada caso.

Uso:
    .venv\\Scripts\\python check_entorno.py              # solo reporta (código 0)
    .venv\\Scripts\\python check_entorno.py --estricto   # código 1 si falta algo obligatorio
    .venv\\Scripts\\python check_entorno.py --descargar-conector  # baja el conector Kafka a .ivy2

No levanta ni detiene contenedores: Kafka debe estar corriendo (lo levanta el usuario).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent
sys.path.insert(0, str(RAIZ))

# Evita errores de codificación al imprimir tildes en consolas de Windows.
try:
    sys.stdout.reconfigure(errors="replace")
except Exception:
    pass

resultados: list[tuple[str, bool, bool]] = []  # (nombre, ok, obligatorio)


def reportar(nombre: str, ok: bool, detalle: str, sugerencia: str = "",
             obligatorio: bool = True) -> None:
    """Imprime una línea [OK]/[FALTA] y, si falta, la sugerencia."""
    etiqueta = "[OK]   " if ok else "[FALTA]"
    extra = "" if obligatorio else " (opcional)"
    print(f"{etiqueta} {nombre}{extra}: {detalle}")
    if not ok and sugerencia:
        print(f"         -> {sugerencia}")
    resultados.append((nombre, ok, obligatorio))


def chequear(nombre: str, funcion, obligatorio: bool = True) -> None:
    """Ejecuta un chequeo sin dejar que una excepción corte el reporte."""
    try:
        funcion()
    except Exception as exc:  # el chequeo mismo falló: se informa como FALTA
        reportar(nombre, False, f"error al verificar ({type(exc).__name__}: {exc})",
                 "Revisa el mensaje anterior.", obligatorio)


# --------------------------------------------------------------------------- #
# Chequeos
# --------------------------------------------------------------------------- #

def ch_python() -> None:
    v = sys.version_info
    reportar("Python 3.12", (v.major, v.minor) == (3, 12), f"{v.major}.{v.minor}.{v.micro}",
             "Crea el entorno con: py -3.12 -m venv .venv (o ejecuta setup_entorno.ps1)")


def ch_venv() -> None:
    en_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    reportar("Entorno virtual aislado", en_venv, sys.prefix,
             "Ejecuta con .venv\\Scripts\\python check_entorno.py")


def _version_java(salida: str) -> int | None:
    m = re.search(r'version "(\d+)(?:\.(\d+))?', salida)
    if not m:
        return None
    mayor = int(m.group(1))
    return int(m.group(2)) if mayor == 1 and m.group(2) else mayor  # "1.8" -> 8


def ch_java() -> None:
    java = shutil.which("java")
    if not java:
        reportar("Java 17 (java -version)", False, "java no está en el PATH",
                 "Instala JDK 17 (p. ej. Eclipse Temurin 17) y agrega %JAVA_HOME%\\bin al PATH")
        return
    proc = subprocess.run([java, "-version"], capture_output=True, text=True, timeout=20)
    salida = (proc.stderr or "") + (proc.stdout or "")
    version = _version_java(salida)
    primera = salida.strip().splitlines()[0] if salida.strip() else "sin salida"
    reportar("Java 17 (java -version)", version == 17, primera,
             "Spark 3.5 se probó con Java 17: instala JDK 17 y ponlo primero en el PATH")


def ch_java_home() -> None:
    jh = os.environ.get("JAVA_HOME")
    ok = bool(jh) and (Path(jh) / "bin" / ("java.exe" if os.name == "nt" else "java")).exists()
    reportar("JAVA_HOME", ok, jh or "no definida",
             'Define JAVA_HOME apuntando al JDK 17, p. ej.: setx JAVA_HOME "C:\\Program Files\\Eclipse Adoptium\\jdk-17..."')


def ch_hadoop() -> None:
    hh = os.environ.get("HADOOP_HOME")
    sug_hh = ('Descarga winutils para Hadoop 3.3 (repositorio cdarlint/winutils), '
              'déjalo en C:\\hadoop\\bin y ejecuta: setx HADOOP_HOME C:\\hadoop')
    reportar("HADOOP_HOME", bool(hh) and Path(hh).is_dir(), hh or "no definida", sug_hh)
    bin_dir = Path(hh) / "bin" if hh else None
    for archivo in ("winutils.exe", "hadoop.dll"):
        ok = bool(bin_dir) and (bin_dir / archivo).exists()
        reportar(archivo, ok, str(bin_dir / archivo) if bin_dir else "sin HADOOP_HOME",
                 f"Copia {archivo} (Hadoop 3.3.x) en %HADOOP_HOME%\\bin; sin él fallan "
                 "parquet y los checkpoints del streaming en Windows")


def ch_pyspark() -> None:
    import pyspark

    v = pyspark.__version__
    reportar("pyspark importable (3.5.x)", v.startswith("3.5."), f"pyspark {v}",
             "Instala las versiones fijadas: .venv\\Scripts\\pip install -r requirements.txt")


def ch_dependencias() -> None:
    modulos = ["pandas", "pyarrow", "numpy", "matplotlib", "sklearn", "streamlit",
               "confluent_kafka", "ollama", "requests", "pytest"]
    faltan = []
    for m in modulos:
        try:
            importlib.import_module(m)
        except Exception:
            faltan.append(m)
    reportar("Dependencias de requirements.txt", not faltan,
             "todas importables" if not faltan else "faltan: " + ", ".join(faltan),
             ".venv\\Scripts\\pip install -r requirements.txt")


def ch_conector() -> None:
    from recomendador import spark_utils

    jar = spark_utils.conector_en_cache()
    reportar("Conector Kafka en caché (.ivy2)", jar is not None,
             str(jar) if jar else f"{spark_utils.conector_kafka()} no descargado",
             "Con internet, ejecuta: .venv\\Scripts\\python check_entorno.py --descargar-conector")


def ch_kafka() -> None:
    from recomendador import config, kafka_utils

    ok = kafka_utils.kafka_disponible(timeout=3)
    reportar("Kafka (broker)", ok, config.KAFKA_BOOTSTRAP + (" responde" if ok else " no responde"),
             "Levanta tu contenedor de Kafka (este script no ejecuta docker) o revisa KAFKA_BOOTSTRAP")
    if ok:
        existe = config.TOPIC in kafka_utils.listar_topics()
        reportar(f"Topic '{config.TOPIC}'", existe, "existe" if existe else "no existe",
                 "Créalo con: .venv\\Scripts\\python scripts/crear_topic.py", obligatorio=False)


def ch_ollama() -> None:
    from recomendador import config

    url = config.OLLAMA_HOST.rstrip("/") + "/api/tags"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            modelos = [m.get("name", "") for m in json.load(r).get("models", [])]
    except Exception:
        reportar("Ollama", False, f"{config.OLLAMA_HOST} no responde",
                 "Inicia Ollama; si no, la app usa el proveedor 'reglas'", obligatorio=False)
        return
    ok = any(m == config.LLM_MODEL or m.startswith(config.LLM_MODEL + ":") for m in modelos)
    reportar(f"Modelo {config.LLM_MODEL}", ok, "disponible" if ok else "no descargado",
             f"ollama pull {config.LLM_MODEL}", obligatorio=False)


def ch_datos() -> None:
    from recomendador import config

    reportar("Muestra ml-mini", (config.MUESTRA_MINI / "ratings.csv").exists(),
             str(config.MUESTRA_MINI), "Falta la carpeta muestras/ml-mini del proyecto")
    ok = (config.DATOS / "ratings.csv").exists()
    reportar("Datos ml-25m", ok, str(config.DATOS),
             "Descárgalos con: .venv\\Scripts\\python scripts/descargar_movielens.py",
             obligatorio=False)


def main() -> int:
    parser = argparse.ArgumentParser(description="Verifica el entorno del recomendador.")
    parser.add_argument("--estricto", action="store_true",
                        help="termina con código 1 si falta algo obligatorio")
    parser.add_argument("--descargar-conector", action="store_true",
                        help="descarga el conector Kafka de Spark a .ivy2 (requiere Java e internet)")
    args = parser.parse_args()

    print("== Verificación del entorno: recomendador-peliculas ==")
    if args.descargar_conector:
        try:
            from recomendador import spark_utils

            jar = spark_utils.precalentar_conector()
            print(f"Conector descargado: {jar}" if jar else "No se pudo descargar el conector")
        except Exception as exc:
            print(f"No se pudo descargar el conector: {type(exc).__name__}: {exc}")

    chequeos = [
        ("Python 3.12", ch_python, True),
        ("Entorno virtual aislado", ch_venv, True),
        ("Java 17 (java -version)", ch_java, True),
        ("JAVA_HOME", ch_java_home, True),
        ("HADOOP_HOME / winutils.exe / hadoop.dll", ch_hadoop, True),
        ("pyspark importable (3.5.x)", ch_pyspark, True),
        ("Dependencias de requirements.txt", ch_dependencias, True),
        ("Conector Kafka en caché (.ivy2)", ch_conector, True),
        ("Kafka (broker)", ch_kafka, True),
        ("Ollama", ch_ollama, False),
        ("Datos", ch_datos, True),
    ]
    for nombre, funcion, obligatorio in chequeos:
        chequear(nombre, funcion, obligatorio)

    try:
        from recomendador import config

        print("\nConfiguración efectiva:")
        for clave, valor in config.resumen().items():
            print(f"  {clave} = {valor}")
    except Exception as exc:
        print(f"\nNo se pudo leer la configuración: {exc}")

    faltan = [n for n, ok, obligatorio in resultados if not ok and obligatorio]
    opcionales = [n for n, ok, obligatorio in resultados if not ok and not obligatorio]
    print()
    if faltan:
        print(f"Resumen: faltan {len(faltan)} elemento(s) obligatorio(s): " + ", ".join(faltan))
    else:
        print("Resumen: entorno listo.")
    if opcionales:
        print("Opcionales pendientes: " + ", ".join(opcionales))
    return 1 if (args.estricto and faltan) else 0


if __name__ == "__main__":
    sys.exit(main())
