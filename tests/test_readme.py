"""Pruebas del paquete 9: el README tiene todas las secciones y solo cita scripts reales.

No usan Spark ni Kafka: solo leen README.md y revisan el árbol del proyecto.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
README = RAIZ / "README.md"

# Títulos de sección (nivel ##) que exige el criterio de aceptación C1.
SECCIONES = [
    "Instalación",
    "check_entorno.py",
    "Corrida completa de 25M paso a paso",
    "Guía de la demo en vivo (5 minutos)",
    "Licencias",
    "Solución de problemas en Windows",
]

# Pasos de la corrida de 25M, en el orden en que deben aparecer.
PASOS_25M = [
    "descargar_movielens.py", "ordenar_ratings.py", "crear_topic.py",
    "entrenar_batch.py", "perfilar_usuarios.py", "escalabilidad.py",
    "figuras_poster.py", "preparar_demo.py", "streaming.py", "productor_kafka.py",
]

# Cualquier ruta o nombre de archivo .py / .ps1 citado en el texto.
PATRON_SCRIPT = re.compile(r"[\w./\\-]*\w\.(?:py|ps1)\b")


@pytest.fixture(scope="module")
def texto() -> str:
    assert README.exists(), "falta README.md"
    return README.read_text(encoding="utf-8")


def _titulos(texto: str) -> list[str]:
    return [m.group(1).strip() for m in re.finditer(r"^##\s+(.+?)\s*$", texto, re.M)]


def _seccion(texto: str, titulo: str) -> str:
    """Texto entre el título ``## titulo`` y el siguiente título de nivel 2."""
    m = re.search(rf"^##\s+{re.escape(titulo)}\s*$(.*?)(?=^##\s|\Z)", texto, re.M | re.S)
    assert m, f"no se encontró la sección {titulo!r}"
    return m.group(1)


def test_tiene_todas_las_secciones(texto):
    titulos = _titulos(texto)
    faltan = [s for s in SECCIONES if s not in titulos]
    assert not faltan, f"faltan secciones en README.md: {faltan}"


def test_instalacion_explica_setup_y_venv(texto):
    sec = _seccion(texto, "Instalación")
    for clave in ("setup_entorno.ps1", "setup_entorno.py", "py -3.12 -m venv .venv",
                  "requirements.txt"):
        assert clave in sec, clave


def test_corrida_25m_en_orden(texto):
    sec = _seccion(texto, "Corrida completa de 25M paso a paso")
    posiciones = [sec.find(p) for p in PASOS_25M]
    assert all(p >= 0 for p in posiciones), dict(zip(PASOS_25M, posiciones))
    assert posiciones == sorted(posiciones), "los pasos de 25M no están en orden"


def test_guia_demo_cubre_los_5_minutos(texto):
    sec = _seccion(texto, "Guía de la demo en vivo (5 minutos)")
    for marca in ("0:00", "5:00", "MODO", "reglas"):
        assert marca in sec, marca


def test_licencias_mencionan_las_tres_fuentes(texto):
    sec = _seccion(texto, "Licencias")
    for fuente in ("MovieLens", "IMDb", "no comercial", "TMDb", "TMDB_API_KEY"):
        assert fuente in sec, fuente


def test_problemas_windows_cubre_lo_tipico(texto):
    sec = _seccion(texto, "Solución de problemas en Windows")
    for clave in ("HADOOP_HOME", "winutils", "hadoop.dll", "JAVA_HOME", "ExecutionPolicy",
                  "localhost:9092"):
        assert clave in sec, clave


def _existe(cita: str) -> bool:
    ruta = cita.replace("\\", "/").lstrip("./")
    if "/" in ruta:
        return (RAIZ / ruta).is_file()
    # Nombre suelto: puede estar en la raíz o en scripts/.
    return (RAIZ / ruta).is_file() or (RAIZ / "scripts" / ruta).is_file()


def test_cada_script_citado_existe(texto):
    citas = sorted(set(PATRON_SCRIPT.findall(texto)))
    assert citas, "el README no cita ningún script"
    inexistentes = [c for c in citas if not _existe(c)]
    assert not inexistentes, f"el README cita scripts que no existen: {inexistentes}"


def test_detector_de_citas_reconoce_rutas_y_nombres():
    """El patrón encuentra rutas con / o \\ y nombres sueltos, y detecta inexistentes."""
    muestra = "corre scripts/entrenar_batch.py, .\\setup_entorno.ps1 y check_entorno.py"
    assert PATRON_SCRIPT.findall(muestra) == [
        "scripts/entrenar_batch.py", ".\\setup_entorno.ps1", "check_entorno.py"]
    assert all(_existe(c) for c in PATRON_SCRIPT.findall(muestra))
    assert not _existe("scripts/no_existe.py")
