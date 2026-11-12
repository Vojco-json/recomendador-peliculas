"""Descarga MovieLens 25M (ml-25m.zip), verifica su md5 y lo descomprime en data/ml-25m.

- Fuente oficial: https://files.grouplens.org/datasets/movielens/ml-25m.zip (+ .md5).
- No repite trabajo: si data/ml-25m/ratings.csv ya existe, termina sin descargar;
  si el zip ya está y su md5 coincide, solo descomprime.
- Descarga a un archivo .part y lo renombra al terminar (una descarga cortada no
  deja un zip corrupto con el nombre final).
- --dry-run muestra lo que haría, sin tocar la red ni el disco.

    .venv/Scripts/python scripts/descargar_movielens.py
    .venv/Scripts/python scripts/descargar_movielens.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import sys
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
URL = "https://files.grouplens.org/datasets/movielens/ml-25m.zip"
NOMBRE = "ml-25m"
ARCHIVOS_ESPERADOS = ("ratings.csv", "movies.csv", "tags.csv", "links.csv",
                      "genome-scores.csv", "genome-tags.csv")
AGENTE = "recomendador-peliculas/1.0 (descarga de MovieLens)"


def leer_md5(texto: str) -> str:
    """Extrae el hash de un .md5 ('MD5 (ml-25m.zip) = abc...' o 'abc...  ml-25m.zip')."""
    m = re.search(r"\b([0-9a-fA-F]{32})\b", texto)
    if not m:
        raise ValueError("el archivo .md5 no contiene un hash válido")
    return m.group(1).lower()


def md5_archivo(ruta: Path, bloque: int = 1 << 20) -> str:
    """md5 de un archivo, leído por bloques de 1 MB."""
    h = hashlib.md5()
    with open(ruta, "rb") as f:
        for trozo in iter(lambda: f.read(bloque), b""):
            h.update(trozo)
    return h.hexdigest()


def _abrir(url: str, timeout: float = 60):
    return urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": AGENTE}),
                                  timeout=timeout)


def descargar(url: str, destino: Path) -> None:
    """Descarga url a destino con barra de progreso simple (vía archivo .part)."""
    parcial = destino.with_name(destino.name + ".part")
    with _abrir(url) as r, open(parcial, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        hecho, ultimo = 0, -1
        while True:
            trozo = r.read(1 << 20)
            if not trozo:
                break
            f.write(trozo)
            hecho += len(trozo)
            if total:
                pct = hecho * 100 // total
                if pct != ultimo and pct % 5 == 0:
                    print(f"  {pct:3d}%  ({hecho / 2**20:,.0f} de {total / 2**20:,.0f} MB)",
                          flush=True)
                    ultimo = pct
    parcial.replace(destino)


def descomprimir(zip_path: Path, carpeta: Path) -> None:
    """Descomprime validando que ninguna ruta salga de `carpeta`."""
    base = carpeta.resolve()
    with zipfile.ZipFile(zip_path) as z:
        for miembro in z.namelist():
            final = (base / miembro).resolve()
            if base != final and base not in final.parents:
                raise ValueError(f"ruta sospechosa dentro del zip: {miembro}")
        z.extractall(base)


def datos_completos(carpeta: Path) -> bool:
    return all((carpeta / a).exists() for a in ARCHIVOS_ESPERADOS)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Descarga y verifica MovieLens 25M.")
    p.add_argument("--destino", default=str(RAIZ / "data"),
                   help="carpeta donde queda ml-25m.zip y ml-25m/ (%(default)s)")
    p.add_argument("--url", default=URL, help="URL del zip (%(default)s)")
    p.add_argument("--forzar", action="store_true", help="descarga aunque ya exista")
    p.add_argument("--dry-run", action="store_true", help="solo muestra lo que haría")
    args = p.parse_args(argv)

    destino = Path(args.destino)
    zip_path = destino / Path(args.url).name
    carpeta = destino / NOMBRE

    if datos_completos(carpeta) and not args.forzar:
        print(f"Ya existe {carpeta} con los {len(ARCHIVOS_ESPERADOS)} archivos; "
              "no se descarga de nuevo (usa --forzar para repetir).")
        return 0

    if args.dry_run:
        print("[dry-run] Plan:")
        if zip_path.exists() and not args.forzar:
            print(f"  1. Usar el zip existente {zip_path}")
        else:
            print(f"  1. Descargar {args.url} (~250 MB) a {zip_path}")
        print(f"  2. Verificar el md5 contra {args.url}.md5")
        print(f"  3. Descomprimir en {carpeta}")
        print("[dry-run] No se descargó ni escribió nada.")
        return 0

    try:
        destino.mkdir(parents=True, exist_ok=True)
        print(f"Leyendo md5 oficial: {args.url}.md5")
        with _abrir(args.url + ".md5", timeout=30) as r:
            esperado = leer_md5(r.read().decode("utf-8", "replace"))

        if zip_path.exists() and not args.forzar and md5_archivo(zip_path) == esperado:
            print(f"El zip {zip_path} ya estaba descargado y su md5 coincide.")
        else:
            print(f"Descargando {args.url} ...")
            descargar(args.url, zip_path)
            obtenido = md5_archivo(zip_path)
            if obtenido != esperado:
                zip_path.unlink(missing_ok=True)
                print(f"ERROR: md5 no coincide (esperado {esperado}, obtenido {obtenido}). "
                      "Se borró el zip; vuelve a intentarlo.", file=sys.stderr)
                return 3
            print(f"md5 verificado: {obtenido}")

        print(f"Descomprimiendo en {carpeta} ...")
        if carpeta.exists() and args.forzar:
            shutil.rmtree(carpeta)
        descomprimir(zip_path, destino)  # el zip ya trae la carpeta ml-25m/
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"ERROR de red o disco: {exc}. Revisa la conexión y vuelve a intentarlo.",
              file=sys.stderr)
        return 1
    except (ValueError, zipfile.BadZipFile) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if not datos_completos(carpeta):
        print(f"ERROR: tras descomprimir faltan archivos en {carpeta}.", file=sys.stderr)
        return 1
    print(f"Listo: MovieLens 25M en {carpeta}.")
    print("Siguiente paso: .venv/Scripts/python scripts/ordenar_ratings.py")
    return 0


if __name__ == "__main__":
    # Salida en UTF-8 aunque la capture otro proceso (si no, Windows usa cp1252: 'descarg�').
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
