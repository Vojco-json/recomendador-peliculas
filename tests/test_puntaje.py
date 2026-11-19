"""Paquete 3: Herramientas.recomendar por niveles de coincidencia (muestra mini).

- Nivel = condiciones pedidas - incumplidas (lo no evaluable no baja el nivel).
- Orden: nivel de mayor a menor y, dentro del nivel, afinidad ALS (orden híbrido).
- Sin condiciones: exactamente el orden híbrido (fold-in sin vistas).
- La lista nunca queda vacía si el catálogo tiene películas.
"""

from __future__ import annotations

import json

import pytest

from recomendador import foldin
from recomendador.herramientas import Herramientas
from base_mini import crear_artefactos_app

CORTE_TODO = "2100-01-01"


class TMDbFalso:
    """Sin clave: nunca sale a la red."""
    disponible = False

    def detalle(self, *_):
        return None

    def titulos_es(self):
        return {}

    def buscar(self, *_a, **_k):
        return None


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_app(spark, ruta_mini, tmp_path_factory.mktemp("art_puntaje"))


@pytest.fixture
def herr(artefactos, ruta_mini, tmp_path):
    return Herramientas(ruta_mini, artefactos, tmp_path / "estado.db", tmdb=TMDbFalso(),
                        corte=CORTE_TODO)


def _orden_hibrido(herr, uid, n):
    """Salida previa del híbrido: fold-in con todos los ratings, sin las vistas."""
    ratings = herr._ratings_usuario(uid)
    ids, matriz, _ = herr._factores_vigentes()
    orden = foldin.recomendar_foldin((ids, matriz), [(m, f["rating"]) for m, f in
                                                    ratings.items()],
                                     n=len(ids), reg=herr._reg())
    catalogo = herr.catalogo()
    return [m for m, _ in orden if m not in ratings and m in catalogo][:n]


def test_sin_condiciones_orden_hibrido(herr):
    for n in (5, 20):
        res = herr.recomendar(1, n=n)
        recs = res["recomendaciones"]
        assert [r["movieId"] for r in recs] == _orden_hibrido(herr, 1, n)
        assert all(r["nivel"] == 0 and r["cumple"] == [] and r["no_cumple"] == []
                   and r["no_evaluables"] == [] for r in recs)
        scores = [r["score"] for r in recs]
        assert scores == sorted(scores, reverse=True)


def test_niveles_y_als_dentro_del_nivel(herr):
    res = herr.recomendar(1, n=5, generos=["Comedy"], tags=["romance"],
                          max_duracion_min=110)
    recs = res["recomendaciones"]
    assert len(recs) == 5                            # las 5 sin ver de la muestra
    niveles = [r["nivel"] for r in recs]
    # Un nivel mayor nunca aparece después de uno menor.
    assert niveles == sorted(niveles, reverse=True)
    assert len(set(niveles)) >= 2                    # hay más de un grupo
    for nivel in set(niveles):
        scores = [r["score"] for r in recs if r["nivel"] == nivel]
        assert scores == sorted(scores, reverse=True), nivel
    for r in recs:
        assert {"nivel", "cumple", "no_cumple", "no_evaluables"} <= set(r)
        assert r["nivel"] == 3 - len(r["no_cumple"])
        assert len(r["cumple"]) + len(r["no_cumple"]) + len(r["no_evaluables"]) == 3
    json.dumps(res, default=str)                     # serializable para el LLM


def test_tag_genome_sube_de_nivel(herr):
    # En la muestra, de las 5 sin ver del usuario 1 solo 527 (La lista de Schindler)
    # no tiene 'romance' con relevancia >= 0.5: queda al final.
    res = herr.recomendar(1, n=5, tags=["romance"])
    recs = res["recomendaciones"]
    con_tag = [r for r in recs if "tag: romance" in r["cumple"]]
    assert con_tag and recs[0] in con_tag
    assert recs[-1]["movieId"] == 527 and recs[-1]["no_cumple"] == ["tag: romance"]


def test_lista_nunca_vacia(herr):
    # Condiciones imposibles: igual devuelve n películas (todas de nivel 0).
    res = herr.recomendar(1, n=5, generos=["Film-Noir"], tags=["no existe"],
                          max_duracion_min=1)
    assert len(res["recomendaciones"]) == 5
    assert res["avisos"]
    # Usuario sin historial con condiciones: popularidad, igual no vacía.
    assert len(herr.recomendar(999999, n=4, generos=["Horror"])["recomendaciones"]) == 4


def test_duracion_sin_dato_no_baja_nivel(herr, monkeypatch):
    catalogo = {m: dict(f) for m, f in herr.catalogo().items()}
    sin_dato = _orden_hibrido(herr, 1, 3)
    for m in sin_dato:
        catalogo[m]["duracion_min"] = None
    monkeypatch.setattr(herr, "catalogo", lambda: catalogo)
    res = herr.recomendar(1, n=5, max_duracion_min=100)
    por_id = {r["movieId"]: r for r in res["recomendaciones"]}
    for m in sin_dato:
        r = por_id[m]
        assert r["no_evaluables"] == ["duración ≤ 100 min"]
        assert r["nivel"] == 1 and r["no_cumple"] == []
    assert any("duración" in a for a in res["avisos"])
