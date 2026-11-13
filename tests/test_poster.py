"""Pruebas del paquete 6: figuras del póster y cálculo del speedup.

No usan Spark: las figuras se generan con artefactos sintéticos con el mismo formato
que producen entrenar_batch.py, perfilar_usuarios.py y escalabilidad.py.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import poster  # noqa: E402

FIGURAS = ["arquitectura", "metricas", "tipos_espectador", "speedup"]


# --------------------------------------------------------------------------- #
# Artefactos sintéticos
# --------------------------------------------------------------------------- #

def _escribir_artefactos(carpeta: Path) -> Path:
    carpeta.mkdir(parents=True, exist_ok=True)
    metricas = {
        "als": {"RMSE": 0.81, "Precision@10": 0.062, "NDCG@10": 0.071,
                "usuarios_evaluados": 12345},
        "popularidad": {"RMSE": 0.95, "Precision@10": 0.041, "NDCG@10": 0.048,
                        "usuarios_evaluados": 12345},
        "corte": "2018-01-01", "n_train": 22_000_000, "n_test": 1_500_000,
        "k": 10, "relevante_min": 4.0,
    }
    (carpeta / "metricas.json").write_text(json.dumps(metricas), encoding="utf-8")

    rasgos = ["genero:Action", "genero:Drama", "tag:funny", "actividad"]
    rng = np.random.default_rng(0)
    centros = {0: [1.2, -0.8, 0.0, 0.1], 1: [-0.9, 1.1, -0.2, -0.1], 2: [-0.1, -0.2, 1.4, 0.0]}
    nombres = {0: "Acción y adrenalina", 1: "Drama y romance", 2: "Comedia ligera"}
    media = [0.0, 0.0, 0.0, 3.0]
    std = [0.3, 0.3, 0.1, 0.5]
    filas, tamanos = [], {0: 0, 1: 0, 2: 0}
    for uid in range(1, 91):
        c = uid % 3
        tamanos[c] += 1
        z = np.array(centros[c]) + rng.normal(0, 0.2, 4)
        crudo = z * np.array(std) + np.array(media)
        filas.append({
            "userId": uid, "cluster": c, "tipo": nombres[c],
            "n_ratings": int(max(1, round(np.expm1(crudo[3])))),
            "pesos_generos": json.dumps({"Action": crudo[0], "Drama": crudo[1]}),
            "pesos_tags": json.dumps({"funny": crudo[2]}),
        })
    pd.DataFrame(filas).to_parquet(carpeta / "perfiles.parquet", index=False)
    tipos = {
        "k": 3, "n_usuarios": 90, "rasgos": rasgos, "media_features": media,
        "std_features": std,
        "tipos": [{"cluster": c, "nombre": nombres[c], "tamaño": tamanos[c],
                   "proporcion": tamanos[c] / 90,
                   "centroide": dict(zip(rasgos, centros[c]))} for c in range(3)],
    }
    (carpeta / "tipos.json").write_text(json.dumps(tipos, ensure_ascii=False),
                                        encoding="utf-8")

    filas_sp = poster.calcular_speedup({1: [120.0, 122.0], 2: [65.0, 64.0],
                                        4: [38.0, 37.0], 6: [30.0, 31.0]})
    esc = {"dataset": "ml-25m", "fraccion": 0.5, "n_ratings": 12_500_000,
           "repeticiones": 2, "params": {"rank": 10, "maxIter": 10},
           "resultados": filas_sp}
    (carpeta / "escalabilidad.json").write_text(json.dumps(esc), encoding="utf-8")
    return carpeta


@pytest.fixture(scope="module")
def generadas(tmp_path_factory):
    base = tmp_path_factory.mktemp("poster")
    artefactos = _escribir_artefactos(base / "artefactos")
    figuras = base / "figuras"
    res = poster.generar_todas(artefactos, figuras, log=lambda *_: None)
    return figuras, res


def _svg(figuras: Path, nombre: str) -> str:
    return (figuras / f"{nombre}.svg").read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Pruebas
# --------------------------------------------------------------------------- #

def test_genera_png_y_svg_no_vacios(generadas):
    figuras, res = generadas
    assert res["avisos"] == []
    for nombre in FIGURAS:
        for ext in ("png", "svg"):
            ruta = figuras / f"{nombre}.{ext}"
            assert ruta.exists(), ruta
            assert ruta.stat().st_size > 5_000, f"{ruta} está casi vacío"


def test_png_a_300_dpi(generadas):
    from PIL import Image

    figuras, _ = generadas
    for nombre in FIGURAS:
        with Image.open(figuras / f"{nombre}.png") as img:
            dpi = img.info.get("dpi")
            assert dpi is not None and round(dpi[0]) == 300, (nombre, dpi)
            assert img.width > 2500   # tamaño de póster, no miniatura


def test_titulos_en_espanol(generadas):
    figuras, _ = generadas
    esperados = {
        "arquitectura": "Arquitectura del recomendador distribuido",
        "metricas": "ALS frente a la línea base de popularidad",
        "tipos_espectador": "Tipos de espectador",
        "speedup": "Escalabilidad del entrenamiento ALS en Spark",
    }
    for nombre, titulo in esperados.items():
        assert titulo in _svg(figuras, nombre), nombre


def test_fuentes_de_14_pt_o_mas(generadas):
    figuras, _ = generadas
    for nombre in FIGURAS:
        texto = _svg(figuras, nombre)
        tamanos = [float(t) for t in re.findall(r"font[^;\"]*?([\d.]+)px", texto)]
        assert tamanos, f"{nombre}: no se encontraron tamaños de fuente en el SVG"
        assert min(tamanos) >= poster.PT_MIN, (nombre, min(tamanos))
        assert "DejaVu Sans" in texto


def test_paleta_del_diseno(generadas):
    figuras, _ = generadas
    for nombre in FIGURAS:
        texto = _svg(figuras, nombre).lower()
        assert "#141414" in texto, f"{nombre}: falta el fondo de cine"
    assert "#e50914" in _svg(figuras, "speedup").lower()      # rojo: speedup medido
    assert "#f5c518" in _svg(figuras, "metricas").lower()     # ámbar: valores


def test_arquitectura_tiene_los_componentes(generadas):
    figuras, _ = generadas
    texto = _svg(figuras, "arquitectura")
    for comp in ("Kafka", "Spark batch", "Structured", "SQLite", "Streamlit", "Ollama"):
        assert comp in texto, comp


def test_speedup_anota_dataset_fraccion_y_cpu(generadas):
    figuras, _ = generadas
    texto = _svg(figuras, "speedup")
    for dato in ("ml-25m", "50 %", "Ryzen 5 3600", "Ideal", "Medido"):
        assert dato in texto, dato


def test_tipos_muestra_nombres_y_tamanos(generadas):
    figuras, _ = generadas
    texto = _svg(figuras, "tipos_espectador")
    for nombre in ("Acción y adrenalina", "Drama y romance", "Comedia ligera"):
        assert nombre in texto
    assert "30 usuarios" in texto
    assert "Componente principal 1" in texto


def test_calcular_speedup():
    filas = poster.calcular_speedup({1: [100.0, 102.0, 98.0], 2: [50.0], 4: [40.0]})
    por_n = {f["nucleos"]: f for f in filas}
    assert por_n[1]["speedup"] == pytest.approx(1.0)
    assert por_n[2]["speedup"] == pytest.approx(2.0)
    assert por_n[4]["speedup"] == pytest.approx(2.5)
    assert por_n[4]["eficiencia"] == pytest.approx(0.625)
    assert por_n[4]["ideal"] == 4


def test_matriz_usuarios_reconstruye_el_espacio_del_kmeans(tmp_path):
    artefactos = _escribir_artefactos(tmp_path / "a")
    tipos = json.loads((artefactos / "tipos.json").read_text(encoding="utf-8"))
    perfiles = pd.read_parquet(artefactos / "perfiles.parquet")
    x, cl = poster.matriz_usuarios(tipos, perfiles)
    centros = np.array([[t["centroide"][r] for r in tipos["rasgos"]] for t in tipos["tipos"]])
    cercano = np.argmin(((x[:, None, :] - centros[None]) ** 2).sum(-1), axis=1)
    assert (cercano == cl).mean() > 0.95


def test_falta_artefacto_avisa_y_genera_las_demas(tmp_path):
    artefactos = _escribir_artefactos(tmp_path / "a")
    (artefactos / "metricas.json").unlink()
    (artefactos / "escalabilidad.json").unlink()
    avisos = []
    res = poster.generar_todas(artefactos, tmp_path / "f", log=avisos.append)
    assert set(res["generadas"]) == {"arquitectura", "tipos_espectador"}
    assert any("metricas.json" in a for a in avisos)
    assert any("escalabilidad.json" in a for a in avisos)
    assert (tmp_path / "f" / "tipos_espectador.png").stat().st_size > 5_000


def test_script_sin_artefactos_termina_bien(tmp_path):
    figuras = tmp_path / "f"
    proc = subprocess.run([sys.executable, str(RAIZ / "scripts" / "figuras_poster.py"),
                           "--artefactos", str(tmp_path / "no_existe"),
                           "--figuras", str(figuras)],
                          capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "AVISO" in proc.stdout
    assert (figuras / "arquitectura.png").exists() and (figuras / "arquitectura.svg").exists()


def test_artefactos_reales_de_la_muestra_si_existen(tmp_path):
    """Si ya se corrieron entrenar_batch y perfilar_usuarios sobre ml-mini, sus
    artefactos reales también producen las figuras."""
    mini = RAIZ / "artefactos" / "mini"
    if not (mini / "metricas.json").exists() or not (mini / "tipos.json").exists():
        pytest.skip("No hay artefactos de la muestra (artefactos/mini)")
    res = poster.generar_todas(mini, tmp_path / "f", log=lambda *_: None)
    for nombre in ("arquitectura", "metricas", "tipos_espectador"):
        for ruta in res["generadas"][nombre]:
            assert ruta.stat().st_size > 5_000
