"""Vista previa estática de CineRuta (login e Inicio) para revisar el aspecto visual.

Uso:  .venv/Scripts/python scripts/vista_previa_cineruta.py
Ejecuta la app REAL con AppTest sobre artefactos/mini (muestra ml-mini): registra una
cuenta, importa muestras/letterboxd_mini.csv y guarda el HTML que pintó la app en
artefactos/vista_previa/cineruta_login.html y cineruta_inicio.html.
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(RAIZ))
tmp = Path(tempfile.mkdtemp(prefix="cineruta_"))
os.environ.update(MODO="completo", DATOS=str(RAIZ / "muestras" / "ml-mini"),
                  ARTEFACTOS_BASE=str(RAIZ / "artefactos" / "mini"),
                  ESTADO_DB=str(tmp / "estado.db"), LLM_PROVIDER="reglas", TMDB_API_KEY="")

from streamlit.testing.v1 import AppTest  # noqa: E402

from recomendador import cuentas, eventos  # noqa: E402

eventos._productor_defecto = lambda *a, **k: None          # sin Kafka: respaldo directo
cuentas.ruta_defecto = lambda: tmp / "cuentas.db"         # no ensucia artefactos/mini
SALIDA = RAIZ / "artefactos" / "vista_previa"

PLANTILLA = """<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CineRuta · vista previa</title></head>
<body class="stApp" style="margin:0"><main class="block-container" style="margin:0 auto;
padding:1.6rem 1rem">{cuerpo}</main></body></html>"""


def guardar(at, nombre: str) -> None:
    partes = []
    for m in at.markdown:
        partes.append(str(m.value) if str(m.value).lstrip().startswith("<")
                      else f"<p>{m.value}</p>")
    SALIDA.mkdir(parents=True, exist_ok=True)
    (SALIDA / nombre).write_text(PLANTILLA.format(cuerpo="\n".join(partes)), encoding="utf-8")
    print("escrito", SALIDA / nombre)


at = AppTest.from_file(str(RAIZ / "app" / "app.py"), default_timeout=120)
at.run()
guardar(at, "cineruta_login.html")
at.text_input(key="reg_nombre").input(f"expo_{int(time.time())}")
at.text_input(key="reg_clave").input("secreta123")
at.text_input(key="reg_clave2").input("secreta123")
next(b for b in at.button if b.label == "Crear cuenta").click().run()
next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
assert not at.exception, at.exception
guardar(at, "cineruta_inicio.html")
