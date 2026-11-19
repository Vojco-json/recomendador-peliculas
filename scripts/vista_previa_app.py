"""Vista previa estática de MoviePredict en MODO PÚBLICO, con la pestaña Chat.

Uso (desde la raíz del proyecto):
    .venv/Scripts/python scripts/vista_previa_app.py

Ejecuta la app REAL (app/app.py) con streamlit.testing.AppTest sobre artefactos/mini
(muestra ml-mini, precalculada con scripts/precalcular_app.py), MODO_PUBLICO=1 y el
proveedor 'reglas'. Guarda en artefactos/vista_previa/:
- app_publico_login.html: lo único que se ve sin sesión.
- app_chat.html: tras registrar una cuenta e importar el CSV de ejemplo, el chat
  responde «algo de Nolan» (pedido por persona) y «una comedia con Tom Hanks».
- app_barra_lateral.txt: lo que muestra la barra lateral (sin controles de admin).
Para login, Inicio, Ficha y Explorar está scripts/vista_previa_moviepredict.py.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
tmp = Path(tempfile.mkdtemp(prefix="moviepredict_publico_"))
os.environ.update(MODO="completo", MODO_PUBLICO="1", DATOS=str(RAIZ / "muestras" / "ml-mini"),
                  ARTEFACTOS_BASE=str(RAIZ / "artefactos" / "mini"),
                  ESTADO_DB=str(tmp / "estado.db"), LLM_PROVIDER="reglas", TMDB_API_KEY="")

from streamlit.testing.v1 import AppTest  # noqa: E402

from recomendador import cuentas, eventos  # noqa: E402

eventos._productor_defecto = lambda *a, **k: None          # sin Kafka: respaldo directo
cuentas.ruta_defecto = lambda: tmp / "cuentas.db"         # no ensucia artefactos/mini
SALIDA = RAIZ / "artefactos" / "vista_previa"

PLANTILLA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MoviePredict · vista previa (modo público)</title></head>
<body class="stApp" style="margin:0"><main class="block-container" style="margin:0 auto;
padding:1.6rem 1rem">{cuerpo}</main></body></html>"""


def guardar(at, nombre: str, bloque=None) -> None:
    """Escribe el HTML de los markdown de la app (o solo los de una pestaña)."""
    partes = [m.value for m in at.markdown if "<style" in str(m.value)][:1] if bloque else []
    for m in (bloque or at).markdown:
        partes.append(str(m.value) if str(m.value).lstrip().startswith("<")
                      else f"<p>{m.value}</p>")
    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / nombre).write_text(PLANTILLA.format(cuerpo="\n".join(partes)), encoding="utf-8")
    print("escrito", SALIDA / nombre)


def main() -> int:
    at = AppTest.from_file(str(RAIZ / "app" / "app.py"), default_timeout=120)
    at.run()
    guardar(at, "app_publico_login.html")
    at.text_input(key="reg_nombre").input(f"expo_{int(time.time())}")
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
    for pedido in ("algo de Nolan", "una comedia con Tom Hanks"):
        at.chat_input(key="chat_entrada").set_value(pedido).run()
    assert not at.exception, at.exception
    guardar(at, "app_chat.html", at.tabs[3])
    lateral = [str(x.value) for x in at.sidebar.markdown] + \
              [str(x.value) for x in at.sidebar.caption] + \
              [f"[botón] {b.label}" for b in at.sidebar.button]
    (SALIDA / "app_barra_lateral.txt").write_text("\n".join(lateral), encoding="utf-8")
    print("escrito", SALIDA / "app_barra_lateral.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
