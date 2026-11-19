"""Paquete 11 (req. 43): el chat entiende pedidos por persona ('algo de Nolan', 'con Tom Hanks').

personas.parquet se arma aquí con pandas (mismas columnas que escribe precalculo.py),
así que estas pruebas no usan Spark. La persona es una condición más del orden por
niveles y aparece en la línea «Entendí:».
"""

from __future__ import annotations

import pandas as pd
import pytest

from recomendador import llm, pedido
from recomendador.herramientas import Herramientas

USUARIO_NUEVO = 999_999          # sin historial: candidatos por popularidad
DARK_KNIGHT, TOY_STORY, FORREST_GUMP = 58559, 1, 356


@pytest.fixture
def artefactos(tmp_path):
    """Carpeta de artefactos con solo app/personas.parquet (directores y reparto)."""
    filas = [
        (DARK_KNIGHT, "tt0468569", "nm0634240", "Christopher Nolan", "director", 1),
        (TOY_STORY, "tt0114709", "nm0000158", "Tom Hanks", "actor", 1),
        (FORREST_GUMP, "tt0109830", "nm0000158", "Tom Hanks", "actor", 1),
        (TOY_STORY, "tt0114709", "nm0005124", "John Lasseter", "director", 1),
        (296, "tt0110912", "nm0000233", "Quentin Tarantino", "director", 1),
        (2571, "tt0133093", "nm9999999", "Pedro Almodóvar", "director", 1),
    ]
    df = pd.DataFrame(filas, columns=["movieId", "tconst", "nconst", "nombre", "rol", "orden"])
    df["nombre_norm"] = df["nombre"].map(pedido.normalizar)
    (tmp_path / "app").mkdir()
    df.to_parquet(tmp_path / "app" / "personas.parquet", index=False)
    return tmp_path


def _analizar(texto, artefactos):
    return pedido.analizar(texto, tags_validos=set(),
                           personas=artefactos / "app" / "personas.parquet")


def test_algo_de_nolan_es_condicion_de_persona(artefactos):
    p = _analizar("algo de Nolan", artefactos)
    assert [x["nombre"] for x in p["personas"]] == ["Christopher Nolan"]
    assert p["personas"][0]["rol"] == "director"
    assert DARK_KNIGHT in p["personas"][0]["movieIds"]
    assert "persona: de Christopher Nolan" in p["condiciones"]
    assert p["pide_recomendacion"]


@pytest.mark.parametrize("texto", ["una con Tom Hanks", "algo con TOM HANKS",
                                   "peliculas con tom hanks", "with Tom Hanks"])
def test_con_tom_hanks_es_actor(artefactos, texto):
    p = _analizar(texto, artefactos)
    assert [x["nombre"] for x in p["personas"]] == ["Tom Hanks"]
    assert p["personas"][0]["rol"] == "actor"
    assert set(p["personas"][0]["movieIds"]) == {TOY_STORY, FORREST_GUMP}


def test_sin_importar_tildes(artefactos):
    assert [x["nombre"] for x in _analizar("dirigida por Christopher Nolán", artefactos)
            ["personas"]] == ["Christopher Nolan"]
    assert [x["nombre"] for x in _analizar("algo de almodovar", artefactos)
            ["personas"]] == ["Pedro Almodóvar"]


def test_persona_mas_genero_y_sin_falsos_positivos(artefactos):
    p = _analizar("una comedia con Tom Hanks", artefactos)
    assert p["generos"] == ["Comedy"] and len(p["personas"]) == 1
    for texto in ("algo de terror", "una de guerra", "drama policial con un twist final",
                  "algo corto para hoy", "hola"):
        assert _analizar(texto, artefactos)["personas"] == [], texto


def test_sin_parquet_no_hay_personas(tmp_path):
    p = pedido.analizar("algo de Nolan", tags_validos=set(),
                        personas=tmp_path / "no_existe.parquet")
    assert p["personas"] == []


def test_chat_recomienda_nolan_y_entendi_lo_menciona(artefactos, ruta_mini, tmp_path):
    h = Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")
    r = llm.responder("algo de Nolan", USUARIO_NUEVO, h, proveedor="reglas")
    assert r.recomendaciones[0]["movieId"] == DARK_KNIGHT
    assert "de Christopher Nolan" in r.recomendaciones[0]["cumple"]
    assert "Entendí:" in r.texto and "Christopher Nolan" in r.texto.split("Entendí:")[1]
    assert r.filtros["personas"] == ["Christopher Nolan"]


def test_persona_cuenta_como_nivel(artefactos, ruta_mini, tmp_path):
    """'comedia con Tom Hanks': Toy Story (comedia + Hanks) cumple las 2 condiciones."""
    h = Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")
    r = llm.responder("una comedia con Tom Hanks", USUARIO_NUEVO, h, proveedor="reglas")
    primera = r.recomendaciones[0]
    assert primera["movieId"] == TOY_STORY
    assert primera["condiciones_pedidas"] == 2 and primera["nivel"] == 2
    niveles = [x["nivel"] for x in r.recomendaciones]
    assert niveles == sorted(niveles, reverse=True)
    assert "con Tom Hanks" in r.texto.split("Entendí:")[1]


def test_herramienta_recomendar_acepta_nombres(artefactos, ruta_mini, tmp_path):
    """El LLM (o cualquiera) puede pasar la persona por nombre a 'recomendar'."""
    h = Herramientas(ruta_mini, artefactos, tmp_path / "estado.db")
    res = h.recomendar(USUARIO_NUEVO, n=3, personas=["nolan"])
    assert res["recomendaciones"][0]["movieId"] == DARK_KNIGHT
    assert res["filtros"]["personas"] == ["Christopher Nolan"]
