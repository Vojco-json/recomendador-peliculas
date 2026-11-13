"""Herramientas de datos sobre ml-mini (artefactos mini en un directorio temporal)."""

from __future__ import annotations

import json

import pytest

from recomendador import estado, perfiles
from recomendador.herramientas import ESQUEMAS, Herramientas
from base_mini import crear_artefactos_app

VISTAS_U1 = {1, 6, 32, 47, 50, 110, 260, 296, 318, 356, 364, 480, 589, 593, 1036, 1721,
             2571, 4306, 6377, 58559}


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_app(spark, ruta_mini, tmp_path_factory.mktemp("art_herr"))


@pytest.fixture
def herr(artefactos, ruta_mini, tmp_path):
    return Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")


def test_esquemas_completos():
    nombres = {e["function"]["name"] for e in ESQUEMAS}
    assert nombres == {"peliculas_calificadas", "tags_usuario", "perfil_gustos",
                       "recomendar", "info_pelicula"}


def test_datos_reales_usuario_1(herr):
    cal = herr.peliculas_calificadas(1, n=50)
    assert cal["existe"] and cal["total"] == 20
    assert {p["movieId"] for p in cal["peliculas"]} == VISTAS_U1

    tags = herr.tags_usuario(1)
    assert {t["tag"] for t in tags["tags"]} == {"heist", "twist ending", "classic action",
                                                "cyberpunk"}

    perfil = herr.perfil_gustos(1)
    assert perfil["existe"] and perfil["tipo"]
    assert set(perfil["top_generos"][:2]) == {"Action", "Thriller"}

    recs = herr.recomendar(1, n=5)
    ids = [r["movieId"] for r in recs["recomendaciones"]]
    assert ids and not set(ids) & VISTAS_U1

    corto = herr.recomendar(1, n=5, max_duracion_min=100,
                            generos=["Comedy", "Animation", "Family"])
    assert [r["movieId"] for r in corto["recomendaciones"]] == [1307]   # Harry y Sally, 95 min
    assert corto["recomendaciones"][0]["duracion_min"] == 95

    info = herr.info_pelicula(1)
    assert info["titulo"] == "Toy Story" and info["duracion_min"] == 81
    json.dumps([cal, tags, perfil, recs, info], default=str)   # serializable para el LLM


def test_usuario_inexistente_no_rompe(herr):
    assert herr.peliculas_calificadas(999999)["existe"] is False
    assert herr.tags_usuario(999999)["tags"] == []
    assert herr.perfil_gustos(999999)["existe"] is False
    recs = herr.recomendar(999999, n=3)
    assert len(recs["recomendaciones"]) == 3 and recs["avisos"]       # popularidad
    assert recs["recomendaciones"][0]["movieId"] == 1                  # Toy Story
    assert herr.info_pelicula(424242)["existe"] is False
    assert "error" in herr.ejecutar("no_existe", {})
    assert "error" in herr.ejecutar("recomendar", {"userId": "abc"})


def test_estado_en_vivo_tiene_prioridad(herr):
    con = estado.conectar(herr.estado_db)
    estado.guardar_eventos(con, [{"userId": 1, "movieId": 1307, "rating": 5.0,
                                  "timestamp": 1600000000}])
    perfil_vivo = {**perfiles.perfil_usuario(1, herr.artefactos), "tipo": "Tipo en vivo"}
    estado.guardar_perfil(con, perfil_vivo)
    estado.guardar_recomendaciones(con, 1, [(539, 9.9)])
    con.close()

    cal = herr.peliculas_calificadas(1, n=50)
    assert cal["total"] == 21
    assert any(p["movieId"] == 1307 and p["fuente"] == "en vivo" for p in cal["peliculas"])
    assert herr.perfil_gustos(1)["tipo"] == "Tipo en vivo"
    recs = herr.recomendar(1, n=3)["recomendaciones"]
    assert recs[0]["movieId"] == 539 and recs[0]["fuente"] == "estado en vivo"
    assert 1307 not in {r["movieId"] for r in recs}
    assert herr.ultima_actualizacion(1) is not None
