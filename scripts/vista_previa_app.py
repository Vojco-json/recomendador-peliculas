"""Vista previa estática de la app Streamlit, para revisar el diseño sin servidor.

Uso (desde la raíz del proyecto):
    .venv/Scripts/python scripts/vista_previa_app.py

Qué hace:
1. Construye los artefactos de la muestra mini en un directorio temporal (igual que
   las pruebas: ALS + perfiles k=3 + duraciones IMDb de muestras/imdb-mini).
2. Ejecuta la app REAL (app/app.py) con streamlit.testing.AppTest, proveedor 'reglas',
   usuario 1, y envía al chat «algo corto y liviano para hoy».
3. Vuelca lo que la app pintó (barra lateral, tarjetas, chat) en
   artefactos/vista_previa/app.html, con el CSS propio de la app y una maqueta de la
   estructura de Streamlit (barra lateral a la izquierda; arriba en pantallas angostas).

Es solo para revisar el aspecto: el HTML y el CSS de las tarjetas, badges y barras son
exactamente los de la app; el «marco» de Streamlit (inputs, botón) es una imitación.
"""

from __future__ import annotations

import html
import re
import sys
import tempfile
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
sys.path.insert(0, str(RAIZ / "tests"))

from recomendador import config, spark_utils  # noqa: E402

SALIDA = RAIZ / "artefactos" / "vista_previa" / "app.html"
PEDIDO = "algo corto y liviano para hoy"


def md(texto: str) -> str:
    """Markdown mínimo (negrita, código, saltos «  \\n»); el HTML de la app pasa tal cual."""
    if texto.lstrip().startswith("<"):
        return texto
    t = html.escape(texto, quote=False)
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    t = re.sub(r"`(.+?)`", r"<code>\1</code>", t)
    return "<p>" + t.replace("  \n", "<br>").replace("\n", "<br>") + "</p>"


def pintar(n) -> str:
    """Convierte (recursivamente) un nodo del árbol de AppTest a HTML."""
    partes = []
    tipo = getattr(n, "type", "")
    valor = getattr(n, "value", None)
    hijos = getattr(n, "children", None)
    if tipo == "chat_message":
        return (f'<div class="st-chat st-{html.escape(n.name)}">'
                + pintar_hijos(n) + "</div>")
    if isinstance(hijos, dict):
        return pintar_hijos(n)
    if tipo == "divider" or (tipo == "markdown" and str(valor).strip() == "---"):
        return "<hr>"
    if True:  # elemento hoja
        if tipo == "markdown":
            partes.append(md(str(valor)))
        elif tipo == "caption":
            partes.append(f'<div class="st-caption">{md(str(valor))}</div>')
        elif tipo in ("title", "header", "subheader"):
            etiqueta = {"title": "h1", "header": "h2", "subheader": "h3"}[tipo]
            partes.append(f"<{etiqueta}>{html.escape(str(valor))}</{etiqueta}>")
        elif tipo == "alert" or n.__class__.__name__ in ("Warning", "Info"):
            clase = "st-warning" if n.__class__.__name__ == "Warning" else "st-info"
            partes.append(f'<div class="{clase}">{html.escape(str(valor))}</div>')
        elif tipo == "number_input":
            partes.append(f'<label class="st-label">{html.escape(n.label)}</label>'
                          f'<div class="st-input">{valor}</div>')
        elif tipo == "button":
            partes.append(f'<div class="st-boton">{html.escape(n.label)}</div>')
        elif tipo == "divider":
            partes.append("<hr>")
    return "\n".join(partes)


def pintar_hijos(nodo) -> str:
    return "\n".join(pintar(h) for _, h in sorted(nodo.children.items()))


MARCO_CSS = """
<style>
*{box-sizing:border-box}
body{margin:0;background:#141414;color:#F2F2F2;font-family:"Source Sans Pro",Segoe UI,
  Arial,sans-serif}
.marco{display:flex;min-height:100vh}
.lateral{width:300px;flex:0 0 300px;background:#1E1E1E;padding:2rem 1.2rem}
.principal{flex:1;padding:2rem 3rem;max-width:100%;min-width:0}
h1{font-size:1.8rem;margin:.2rem 0 1rem} h2{font-size:1.6rem} h3{font-size:1.3rem}
p{margin:.3rem 0} code{background:#2b2b2b;color:#7CFC9A;padding:1px 5px;border-radius:4px}
hr{border:0;border-top:1px solid #333;margin:1rem 0}
.st-caption{color:#9a9a9a;font-size:.85rem}
.st-label{font-size:.85rem;color:#ccc}
.st-input{background:#2b2b2b;border-radius:8px;padding:.5rem .7rem;margin:.2rem 0 .8rem}
.st-boton{border:1px solid #444;border-radius:8px;padding:.45rem;text-align:center;
  margin-top:.6rem}
.st-warning{background:rgba(245,197,24,.15);color:#F5C518;border-radius:8px;
  padding:.7rem 1rem;margin:.5rem 0}
.st-info{background:rgba(60,140,255,.15);color:#9cc4ff;border-radius:8px;padding:.7rem 1rem}
.st-chat{border-radius:10px;padding:.6rem .9rem;margin:.6rem 0;background:#1b1b1b}
.st-chat.st-user{background:#232323}
.chat-input{border:1px solid #444;border-radius:10px;padding:.7rem 1rem;color:#888;
  margin-top:1rem}
.nota{color:#777;font-size:.75rem;margin-top:2rem}
@media (max-width:700px){.marco{flex-direction:column}.lateral{width:100%;flex:none}
  .principal{padding:1.2rem}}
</style>
"""


def main() -> int:
    from streamlit.testing.v1 import AppTest

    from base_mini import crear_artefactos_app

    spark = spark_utils.crear_spark_vigilado(nucleos=2, app="vista_previa",
                                             extra={"spark.ui.enabled": "false"})
    with tempfile.TemporaryDirectory() as tmp:
        artefactos = crear_artefactos_app(spark, config.MUESTRA_MINI, Path(tmp) / "art")
        spark.stop()
        # La app lee la configuración desde el módulo config (mismo proceso).
        config.MODO = "completo"
        config.DATOS = config.MUESTRA_MINI
        config.ARTEFACTOS_BASE = artefactos
        config.ESTADO_DB = Path(tmp) / "estado.db"
        config.LLM_PROVIDER = "reglas"
        at = AppTest.from_file(str(RAIZ / "app" / "app.py"), default_timeout=120)
        at.run()
        at.chat_input[0].set_value(PEDIDO).run()
        if at.exception:
            print("La app lanzó una excepción:", at.exception)
            return 1
        css_app = next(str(m.value) for m in at.markdown if "<style>" in str(m.value))
        lateral = pintar(at.sidebar)
        principal = pintar(at.main).replace(css_app, "")

    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    SALIDA.write_text(
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>Vista previa · Recomendador</title>" + MARCO_CSS + css_app + "</head><body>"
        f"<div class='marco'><aside class='lateral'>{lateral}</aside>"
        f"<main class='principal'>{principal}"
        "<div class='chat-input'>¿Qué quieres ver hoy?</div>"
        "<div class='nota'>Vista previa estática generada por scripts/vista_previa_app.py "
        "a partir de la app real (AppTest, muestra mini, proveedor «reglas»).</div>"
        "</main></div></body></html>", encoding="utf-8")
    print(f"Vista previa escrita en {SALIDA}")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    sys.exit(main())
