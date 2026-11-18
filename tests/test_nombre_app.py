"""La app se llama MoviePredict: ningún archivo de código o documentación usa el nombre anterior."""
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
# El nombre anterior se arma por partes para que este archivo no se detecte a sí mismo.
NOMBRE_VIEJO = ("cine" + "ruta").encode()
SUFIJOS = {".py", ".md", ".txt", ".json", ".toml", ".ps1", ".bat", ".html", ".css"}
# ESPEC.md y ARQUITECTURA.md los mantiene Delegado; _capturas y artefactos son generados.
EXCLUIR_DIRS = {".venv", ".git", "_capturas", "artefactos", "datos", "__pycache__", ".pytest_cache"}
EXCLUIR_ARCHIVOS = {"ESPEC.md", "ARQUITECTURA.md"}


def test_sin_referencias_al_nombre_anterior():
    con_nombre_viejo = []
    for p in RAIZ.rglob("*"):
        if not p.is_file() or p.suffix not in SUFIJOS or p.name in EXCLUIR_ARCHIVOS:
            continue
        if EXCLUIR_DIRS & set(p.relative_to(RAIZ).parts):
            continue
        if NOMBRE_VIEJO in p.read_bytes().lower() or NOMBRE_VIEJO in p.name.lower().encode():
            con_nombre_viejo.append(str(p.relative_to(RAIZ)))
    assert con_nombre_viejo == []


def test_nombre_en_estilo_titulo_y_pie():
    import sys
    sys.path.insert(0, str(RAIZ / "app"))
    import estilo
    assert estilo.NOMBRE_APP == "MoviePredict"
    assert "MoviePredict" in estilo.pie_html()
    assert "MoviePredict" in (RAIZ / "README.md").read_text(encoding="utf-8")


def test_cabecera_visible_sin_etiquetas():
    """El texto visible de la cabecera (sin etiquetas HTML) dice MoviePredict y no el nombre viejo."""
    import re
    import sys
    sys.path.insert(0, str(RAIZ / "app"))
    import estilo
    html = estilo.cabecera_html()
    assert "Movie<b>Predict</b>" in html
    visible = re.sub(r"<[^>]+>", "", html).lower()
    assert "moviepredict" in visible
    assert NOMBRE_VIEJO.decode() not in visible
    # Lo mismo para el pie: ningún resto del nombre anterior partido por etiquetas.
    assert NOMBRE_VIEJO.decode() not in re.sub(r"<[^>]+>", "", estilo.pie_html()).lower()
