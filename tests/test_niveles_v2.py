"""Pruebas de la corrección por niveles (requisitos 12 a 21)."""
import subprocess
import sys
from pathlib import Path

from recomendador import pedido
from recomendador.herramientas import _afinidad, _evaluar, _grupos_generos

RAIZ = Path(__file__).resolve().parents[1]


def test_cada_genero_es_una_condicion():
    grupos = _grupos_generos(["Drama", "Crime"])
    assert grupos == [["Drama"], ["Crime"]]
    solo_drama = {"movieId": 1, "generos": ["Drama"]}
    ev = _evaluar(solo_drama, grupos, [], {}, None)
    assert len(ev["cumple"]) == 1 and len(ev["no_cumple"]) == 1


def test_tag_sin_datos_no_evaluable_y_duracion_neutral():
    ficha = {"movieId": 7, "generos": ["Drama"], "duracion_min": None}
    ev = _evaluar(ficha, [["Drama"]], ["twist ending"], {"twist ending": {}}, 90)
    assert "tag: twist ending" in ev["no_evaluables"]
    assert any(c.startswith("duración") for c in ev["no_evaluables"])


def test_algo_como_descripcion_no_es_titulo():
    p = pedido.interpretar_pedido("algo como de drama policial con un twist épico")
    assert not p.get("titulo_mencionado")
    assert "Drama" in p["generos"] and "policial" in p["temas"]


def test_afinidad_textual():
    assert _afinidad(2.0) == "afinidad alta"
    assert _afinidad(0.1) == "afinidad media"
    assert _afinidad(None) is None


def test_spark_timeouts():
    texto = (RAIZ / "recomendador" / "spark_utils.py").read_text(encoding="utf-8")
    assert '"spark.executor.heartbeatInterval": "60s"' in texto
    assert '"spark.network.timeout": "600s"' in texto


def test_probar_pedidos_corre_con_mini():
    r = subprocess.run([sys.executable, str(RAIZ / "scripts" / "probar_pedidos.py"),
                        "--datos", str(RAIZ / "muestras" / "ml-mini")],
                       capture_output=True, text=True, encoding="utf-8", timeout=300,
                       env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8",
                            "PYTHONUTF8": "1"})
    assert r.returncode == 0, r.stderr[-2000:]
    assert "drama policial con un twist épico" in r.stdout
