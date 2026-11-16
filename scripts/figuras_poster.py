"""Genera las figuras del póster en PNG (300 dpi) y SVG.

    .venv/Scripts/python scripts/figuras_poster.py                        # artefactos/25m -> poster/figuras
    .venv/Scripts/python scripts/figuras_poster.py --artefactos artefactos/mini --figuras artefactos/mini/figuras

Figuras (en --figuras):
- arquitectura.png/.svg      Kafka, Spark batch y streaming, SQLite, Streamlit y Ollama.
- metricas.png/.svg          ALS vs popularidad (RMSE, Precision@10, NDCG@10), de metricas.json.
- tipos_espectador.png/.svg  PCA 2D de los tipos con nombres y tamaños, de tipos.json y
                             perfiles.parquet.
- speedup.png/.svg           speedup medido vs ideal, de escalabilidad.json.
- qr_publico.png/.svg        QR hacia URL_PUBLICA (la URL de Cloudflare Tunnel). Si la
                             variable no está definida (ni --url), avisa y no lo genera.

Si falta un artefacto, avisa y genera las demás (termina con código 0).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def crear_parser() -> argparse.ArgumentParser:
    from recomendador import config

    p = argparse.ArgumentParser(description="Figuras del póster (PNG 300 dpi y SVG).")
    p.add_argument("--artefactos", default=str(config.ARTEFACTOS_BASE),
                   help="carpeta con metricas.json, tipos.json, perfiles.parquet y "
                        "escalabilidad.json (%(default)s)")
    p.add_argument("--figuras", default=str(config.FIGURAS),
                   help="carpeta de salida de las figuras (%(default)s)")
    p.add_argument("--escalabilidad", default=None,
                   help="ruta de escalabilidad.json si no está en --artefactos")
    p.add_argument("--url", default=None,
                   help="URL del QR; por defecto la variable URL_PUBLICA")
    return p


AVISO_SIN_URL = ("AVISO: URL_PUBLICA no está definida; no se genera qr_publico.png "
                 "(define URL_PUBLICA=https://... con la URL de cloudflared)")


def generar_qr(url: str, carpeta) -> list[Path]:
    """QR (segno, corrección M) hacia ``url``: qr_publico.png y qr_publico.svg."""
    import segno

    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    qr = segno.make(url, error="m")
    rutas = [carpeta / "qr_publico.png", carpeta / "qr_publico.svg"]
    qr.save(str(rutas[0]), scale=20, border=4, dark="#111111", light="#ffffff")
    qr.save(str(rutas[1]), scale=10, border=4, dark="#111111", light="#ffffff")
    return rutas


def main(argv: list[str] | None = None) -> int:
    args = crear_parser().parse_args(argv)
    from recomendador.poster import generar_todas

    artefactos = Path(args.artefactos)
    if not artefactos.is_dir():
        print(f"AVISO: no existe la carpeta {artefactos}; solo se puede generar la "
              "arquitectura")
    res = generar_todas(artefactos, args.figuras, args.escalabilidad)
    from recomendador import config

    url = (args.url if args.url is not None else config.URL_PUBLICA).strip()
    if url:
        res["generadas"]["qr_publico"] = generar_qr(url, args.figuras)
        print(f"QR hacia {url}")
    else:
        print(AVISO_SIN_URL)
        res["avisos"].append(AVISO_SIN_URL)
    print()
    print(f"Figuras generadas en {Path(args.figuras).resolve()}:")
    for nombre, rutas in res["generadas"].items():
        print(f"  {nombre:<17} " + ", ".join(r.name for r in rutas))
    if res["avisos"]:
        print(f"{len(res['avisos'])} figura(s) con aviso (ver arriba).")
    return 0


if __name__ == "__main__":
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
