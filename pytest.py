"""Reenvío de ``python -m pytest`` al intérprete del .venv del proyecto.

Por qué existe
--------------
En Windows, cuando un proceso lanza ``.venv/Scripts/python`` (ruta relativa) con
``cwd=<proyecto>``, CreateProcess resuelve el ejecutable desde el directorio del
proceso *padre*, no desde ``cwd``. Por eso las pruebas terminaban usando otro
``.venv`` (sin pytest ni pyspark) y fallaban con "No module named pytest".

``python -m pytest`` busca primero en el directorio actual, así que este archivo se
encuentra antes que el pytest real:

* Si el intérprete NO es el del .venv del proyecto y ese .venv existe, vuelve a
  ejecutar ``<proyecto>/.venv/Scripts/python -m pytest <args>`` y devuelve su código.
* Si ya es el del proyecto, se aparta del ``sys.path`` y cede el control al pytest
  real instalado en el .venv (sin cambiar ningún comportamiento).
"""

from __future__ import annotations

import os
import subprocess
import sys

RAIZ = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(RAIZ, ".venv")
PYTHON_VENV = (
    os.path.join(VENV, "Scripts", "python.exe")
    if os.name == "nt"
    else os.path.join(VENV, "bin", "python")
)


def _normalizar(ruta: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(ruta or os.getcwd())))


def _es_venv_del_proyecto() -> bool:
    """True si el intérprete actual es el del .venv del proyecto."""
    return _normalizar(sys.prefix) == _normalizar(VENV)


def _cargar_pytest_real():
    """Quita la raíz del proyecto del sys.path e importa el pytest instalado."""
    raiz = _normalizar(RAIZ)
    sys.path[:] = [p for p in sys.path if _normalizar(p) != raiz]
    sys.modules.pop("pytest", None)
    import pytest as real  # noqa: PLC0415  (import diferido a propósito)

    return real


if __name__ == "__main__":
    ya_reenviado = os.environ.get("RECOMENDADOR_PYTEST_REENVIO") == "1"
    if not ya_reenviado and not _es_venv_del_proyecto() and os.path.isfile(PYTHON_VENV):
        # Otro intérprete: reejecutar con el del proyecto, mismos argumentos.
        # La variable de entorno evita un bucle si la comparación de rutas fallara.
        entorno = dict(os.environ, RECOMENDADOR_PYTEST_REENVIO="1")
        sys.exit(subprocess.call([PYTHON_VENV, "-m", "pytest", *sys.argv[1:]], env=entorno))
    _pytest = _cargar_pytest_real()
    sys.exit(_pytest.console_main())
else:
    # Importado como módulo (p. ej. 'import pytest' con la raíz en sys.path):
    # reemplazarse por el pytest real para no ocultarlo.
    sys.modules[__name__] = _cargar_pytest_real()
