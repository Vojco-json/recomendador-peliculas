"""Pruebas del paquete 5: modo demo (preparar_demo y reentrenamiento sobre el subconjunto).

Valores esperados de muestras/ml-mini/LEEME.md: con --desde 2015-01-01 y corte
2016-01-01, base_demo.parquet tiene 90 ratings (6 por usuario).
"""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest

from base_mini import CORTE, crear_base_mini
from recomendador import config, estado, streaming
from recomendador.datos import fecha_a_epoch
from recomendador.demo import preparar_demo
from scripts import preparar_demo as script_demo
from scripts.productor_kafka import a_json, leer_eventos

N_DEMO_LEEME = 90


@pytest.fixture(scope="module")
def base(spark, ruta_mini, tmp_path_factory):
    return crear_base_mini(spark, ruta_mini, tmp_path_factory.mktemp("base_demo"))


@pytest.fixture(scope="module")
def demo(base, ruta_mini, tmp_path_factory):
    salida = tmp_path_factory.mktemp("demo")
    codigo = script_demo.main(["--base", str(base), "--datos", str(ruta_mini),
                               "--desde", "2015-01-01", "--corte", CORTE,
                               "--salida", str(salida)])
    assert codigo == 0
    return salida


def test_subconjunto_desde_2015_conteo_leeme(demo):
    sub = pd.read_parquet(demo / "base_demo.parquet")
    assert len(sub) == N_DEMO_LEEME
    assert sub["timestamp"].min() >= fecha_a_epoch("2015-01-01")
    assert sub["timestamp"].max() < fecha_a_epoch(CORTE)
    assert set(sub.groupby("userId").size()) == {6}
    info = json.loads((demo / "demo.json").read_text(encoding="utf-8"))
    assert info["n_base_demo"] == N_DEMO_LEEME and info["desde"] == "2015-01-01"
    for nombre in ("item_factors.parquet", "perfiles.parquet", "tipos.json"):
        assert (demo / nombre).exists(), nombre


def test_desde_configurable_por_entorno(base, ruta_mini, tmp_path, monkeypatch):
    # DEMO_DESDE se lee al importar config; aquí se simula el valor ya cargado.
    monkeypatch.setattr(config, "DEMO_DESDE", "2015-07-01")
    res = preparar_demo(base, ruta_mini, None, tmp_path / "d", CORTE, log=lambda *_: None)
    r = pd.read_csv(ruta_mini / "ratings.csv")
    esperado = int(((r["timestamp"] >= fecha_a_epoch("2015-07-01"))
                    & (r["timestamp"] < fecha_a_epoch(CORTE))).sum())
    assert res["desde"] == "2015-07-01"
    assert res["n_base_demo"] == esperado < N_DEMO_LEEME


def test_reentrenamiento_demo_cambia_version(spark, demo, ruta_mini, tmp_path):
    db = tmp_path / "estado.db"
    ctx = streaming.crear_contexto(spark, demo, db, "demo", consulta="demo_test",
                                   log=lambda *_: None)
    assert estado.get_meta(ctx.con, "modo") == "demo"
    assert estado.get_meta(ctx.con, "version_modelo") == "0"

    # Eventos en vivo (posteriores al corte) para dos usuarios.
    eventos = [e for e in leer_eventos(ruta_mini, desde=CORTE) if e["userId"] in (1, 2)]
    archivo = tmp_path / "eventos.json"
    archivo.write_text("\n".join(a_json(e) for e in eventos), encoding="utf-8")
    df = spark.read.schema(streaming.ESQUEMA_EVENTO).json(str(archivo))
    assert streaming.procesar_lote(df, 0, ctx) == len(eventos) == 12

    # Reentrenamiento en el hilo aparte: base demo (90) + 12 eventos.
    hilo = streaming.Reentrenador(ctx, intervalo_s=0.5)
    hilo.start()
    limite = time.time() + 80
    while not hilo.versiones and not hilo.errores and time.time() < limite:
        time.sleep(0.5)
    hilo.detener(esperar=30)
    assert not hilo.errores, hilo.errores
    assert hilo.versiones == [1]

    m = estado.meta(ctx.con)
    assert m["version_modelo"] == "1"
    assert m["ultimo_reentrenamiento"] is not None
    nueva = demo / "modelos" / "v0001"
    assert m["item_factors"] == str(nueva / "item_factors.parquet")
    info = json.loads((nueva / "info.json").read_text(encoding="utf-8"))
    assert info["n_ratings"] == N_DEMO_LEEME + 12 and info["modo"] == "demo"
    # Los factores originales copiados de la base no se tocan.
    assert (demo / "item_factors.parquet").exists()
    # El siguiente uso toma el modelo nuevo y las recomendaciones siguen sin vistas.
    assert len(estado.leer_recomendaciones(ctx.con, 1)) >= 1
    ctx.cerrar()
