"""Paquete de correcciones: parámetros del reentrenamiento y orden de 'Para ti' / Explorar.

Muestra mini con corte 2016-01-01.
"""
from __future__ import annotations

import json

import pandas as pd
import pytest

from base_mini import CORTE, crear_artefactos_moviepredict
from recomendador import batch_als, streaming
from recomendador.catalogo_app import Catalogo

HIB = {"rank": 6, "alpha": 5.0, "peso_als": 0.5}


# ---- 1. Parámetros del reentrenamiento ---------------------------------- #
def test_params_base_lee_params_produccion(tmp_path):
    pp = batch_als.params_produccion({"rank": 8, "alpha": 20.0})
    (tmp_path / "metricas.json").write_text(json.dumps(
        {"params": batch_als.PARAMS_DEFECTO, "hibrido": HIB, "params_produccion": pp}))
    assert streaming._params_base(tmp_path) == {**batch_als.PARAMS_DEFECTO, **pp}
    assert pp["implicitPrefs"] is True and pp["rank"] == 8 and pp["alpha"] == 20.0


def test_params_base_se_arma_desde_hibrido(tmp_path):
    (tmp_path / "metricas.json").write_text(json.dumps(
        {"params": batch_als.PARAMS_DEFECTO, "hibrido": HIB}))
    assert streaming._params_base(tmp_path) == {
        "rank": 6, "alpha": 5.0, "implicitPrefs": True,
        "regParam": 0.1, "maxIter": 10, "seed": 42}


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("pt"))


def test_reentreno_usa_rank_del_hibrido_e_implicito(spark, artefactos, ruta_mini, tmp_path,
                                                     monkeypatch):
    (artefactos / "metricas.json").write_text(json.dumps(
        {"params": batch_als.PARAMS_DEFECTO, "hibrido": HIB}))
    usados = []
    original = batch_als.entrenar

    def espia(datos, params=None):
        usados.append(dict(params))
        return original(datos, params)

    monkeypatch.setattr(batch_als, "entrenar", espia)
    ctx = streaming.crear_contexto(spark, artefactos, tmp_path / "estado.db", "completo",
                                   consulta="pt", datos=ruta_mini, corte=CORTE,
                                   log=lambda *_: None)
    ctx.base_reentreno = ctx.historia
    info = streaming.reentrenar(ctx)
    ctx.cerrar()
    assert usados and usados[0]["implicitPrefs"] is True and usados[0]["rank"] == 6
    guardado = json.loads((artefactos / "modelos" / f"v{info['version']:04d}" / "info.json")
                          .read_text(encoding="utf-8"))
    assert guardado["params"]["implicitPrefs"] is True
    fac = pd.read_parquet(artefactos / "modelos" / f"v{info['version']:04d}"
                          / "item_factors.parquet")
    assert len(fac["features"].iloc[0]) == 6


# ---- 2. Orden de 'Para ti', tarjeta y Explorar --------------------------- #
@pytest.fixture()
def cat(artefactos, ruta_mini, tmp_path):
    return Catalogo(artefactos, ruta_mini, tmp_path / "e.db", CORTE, tmp_path / "c.db")


def _orden_chat(cat, uid):
    ratings = cat.ratings_usuario(uid)
    ids, matriz, _, _ = cat._factores()
    fichas = cat._peliculas()["fichas"]
    orden = [m for m, _ in cat._h.orden_hibrido(
        {m: {"rating": r} for m, r in ratings.items()}, ids, matriz)
        if m in fichas and m not in ratings]
    con = [m for m in orden if cat._con_votos(m)]
    return con + [m for m in orden if m not in con]


@pytest.mark.parametrize("min_votos", [50, 3])
def test_fila_y_tarjeta_usan_orden_hibrido(cat, min_votos):
    cat._h.min_votos = min_votos
    esperado = _orden_chat(cat, 1)
    fila = next(f for f in cat.filas_inicio(1) if f["clave"] == "para_ti")
    ids_fila = [p["movieId"] for p in fila["peliculas"]]
    assert ids_fila == esperado[:len(ids_fila)] and ids_fila
    t = cat.tarjeta_para_ti(1)
    assert t["probable"]["movieId"] == esperado[0]
    if min_votos == 3:
        # Con candidatas de >= 3 votos en la mini, ellas van primero y la improbable sale de ellas.
        assert any(cat._con_votos(m) for m in esperado)
        assert cat._con_votos(esperado[0])
        if t["improbable"]:
            assert cat._con_votos(t["improbable"]["movieId"])
