"""Pruebas de integración de la corrección por niveles (requisitos 13, 14, 17 y 18).

Usan la muestra mini, sin Spark, Ollama ni red.
"""
from __future__ import annotations

import re

import numpy as np
import pytest

from recomendador import batch_als, config, foldin, llm
from recomendador.enriquecimiento import ClienteTMDb
from recomendador.herramientas import Herramientas

USUARIO = 1


class SesionProhibida:
    def get(self, *a, **k):  # pragma: no cover
        raise AssertionError("no debe haber llamadas de red")


def _herr(tmp, **kw) -> Herramientas:
    tmdb = ClienteTMDb(api_key="", cache=tmp / "cache.json", sesion=SesionProhibida())
    return Herramientas(config.MUESTRA_MINI, tmp / "art", tmp / "estado.db", tmdb=tmdb, **kw)


@pytest.fixture(scope="module")
def herr(tmp_path_factory):
    return _herr(tmp_path_factory.mktemp("niveles"))


# ----------------------------- Requisito 13 -------------------------------- #

def test_poco_votadas_siempre_al_final(tmp_path):
    """Con un mínimo de votos alto, ninguna poco votada queda antes de una bien votada,
    aunque cumpla más condiciones."""
    h0 = _herr(tmp_path)
    votos = sorted(h0._votos().values())
    umbral = votos[len(votos) // 2]          # mediana: la mitad queda "poco votada"
    h = _herr(tmp_path, min_votos=umbral)
    res = h.recomendar(USUARIO, n=40, generos=["Drama", "Crime"], tags=["twist ending"])
    recs = res["recomendaciones"]
    assert recs
    poco = [r["votos"] < umbral for r in recs]
    # Una vez que aparece una poco votada, todas las siguientes también lo son.
    if True in poco:
        assert all(poco[poco.index(True):]), [(r["titulo"], r["votos"], r["nivel"])
                                              for r in recs]
    # Dentro del bloque de bien votadas el nivel no sube.
    niveles = [r["nivel"] for r in recs if r["votos"] >= umbral]
    assert niveles == sorted(niveles, reverse=True)


def test_poco_votada_que_cumple_todo_no_adelanta_a_bien_votadas(tmp_path, monkeypatch):
    """Caso del revisor: las Drama+Crime tienen < min_votos (cumplen todo, nivel máximo)
    y aun así quedan después de todas las bien votadas que cumplen menos."""
    h = _herr(tmp_path, min_votos=50)
    cat = h.catalogo()
    dc = {m for m, f in cat.items() if {"Drama", "Crime"} <= set(f.get("generos", []))}
    assert dc
    votos_falsos = {m: (1 if m in dc else 1000) for m in cat}
    monkeypatch.setattr(h, "_votos", lambda: votos_falsos)
    recs = h.recomendar(USUARIO, n=30, generos=["Drama", "Crime"])["recomendaciones"]
    poco = [r["movieId"] in dc for r in recs]
    assert False in poco and True in poco, [(r["titulo"], r["nivel"]) for r in recs]
    assert all(poco[poco.index(True):])


def test_orden_hibrido_es_la_formula_de_recs_hibrido(herr, monkeypatch):
    """orden_hibrido = peso·z(ALS) + (1-peso)·z(log(1+votos)), igual que recs_hibrido."""
    votos = herr._votos()
    mids = sorted(votos)[:30]
    rng = np.random.default_rng(0)
    als = rng.normal(size=len(mids))
    monkeypatch.setattr(foldin, "recomendar_foldin",
                        lambda *a, **k: list(zip(mids, als.tolist())))
    monkeypatch.setattr(herr, "min_votos", 0)          # todas en la base de z-scores
    orden = herr.orden_hibrido({1: {"rating": 4.0}}, np.array(mids), np.zeros((30, 2)))
    peso = herr._peso_hibrido()
    pop = np.log1p(np.array([votos[m] for m in mids], dtype=float))
    esperado = (peso * batch_als._zscore(als[None, :], 1)[0]
                + (1 - peso) * batch_als._zscore(pop[None, :], 1)[0])
    ids_esperados = [mids[i] for i in np.argsort(-esperado, kind="stable")]
    assert [m for m, _ in orden] == ids_esperados


# ----------------------------- Requisito 14 -------------------------------- #

def test_nivel_en_recomendar_cuenta_tag_sin_datos_como_no_cumplido(herr):
    res = herr.recomendar(USUARIO, n=20, generos=["Drama"], tags=["twist ending"])
    recs = res["recomendaciones"]
    assert recs and all("nivel" in r and r["condiciones_pedidas"] == 2 for r in recs)
    for r in recs:
        if "tag: twist ending" in r["no_evaluables"]:
            assert r["nivel"] <= 1, r          # sin datos genome = no cumplido
    niveles = [r["nivel"] for r in recs]
    assert niveles == sorted(niveles, reverse=True)


def test_drama_policial_exige_ambos_generos_para_nivel_maximo(herr):
    res = herr.recomendar(USUARIO, n=10, generos=["Drama", "Crime"])
    for r in res["recomendaciones"]:
        if r["nivel"] == r["condiciones_pedidas"]:
            assert {"Drama", "Crime"} <= set(r["generos"])


# ----------------------------- Requisito 17 -------------------------------- #

def test_titulo_no_encontrado_aporta_sus_generos(herr, monkeypatch):
    """Si X se tomó como título y no está, igual se extraen sus géneros."""
    real = llm.interpretar_pedido

    def forzar_titulo(mensaje, **kw):
        p = real(mensaje, **kw)
        if "mecanismo" in mensaje.lower() and mensaje != "El mecanismo de terror":
            p = {**p, "titulo_mencionado": "El mecanismo de terror", "generos": []}
        return p

    monkeypatch.setattr(llm, "interpretar_pedido", forzar_titulo)
    a = llm.analizar("parecida a El mecanismo de terror", herr)
    assert a["frase_titulo"].startswith("No encontré")
    assert "Horror" in a["generos"]
    res = llm.recomendar_detectado(USUARIO, herr, a)
    assert res["recomendaciones"]


# ----------------------------- Requisito 18 -------------------------------- #

def test_explicaciones_no_repiten_referencia(herr):
    r = llm.responder("recomiéndame 6 dramas", USUARIO, herr, proveedor="reglas")
    assert len(r.recomendaciones) >= 3
    refs = [m.group(1) for rec in r.recomendaciones
            for m in [re.search(r"a (.+?), que también es", rec["explicacion"])] if m]
    assert len(refs) >= 2, [rec["explicacion"] for rec in r.recomendaciones]
    assert len(refs) == len(set(refs)), refs
    for rec in r.recomendaciones:
        assert not re.search(r"\b\d+[.,]\d+\s*★?\s*de afinidad", rec["explicacion"])
