"""Corte temporal en todos los modos (demo o no) sobre la muestra mini.

- Herramientas siempre aplica un corte: FECHA_CORTE por defecto, FECHA_CORTE_MINI con la
  muestra mini, o el que se pase con el parámetro 'corte'.
- Historial, peliculas_calificadas, tags_usuario y popularidad ignoran timestamps >= corte.
- Los eventos en vivo de estado.db se incluyen siempre, aunque sean posteriores al corte.
- demo.json guarda rutas relativas; productor_kafka y correr_25m.ps1 respetan el corte.
"""

from __future__ import annotations

import json
import re
import time

import pandas as pd
import pytest

from recomendador import config, demo, estado
from recomendador.datos import fecha_a_epoch
from recomendador.herramientas import Herramientas, corte_por_defecto

RAIZ = config.RAIZ
CORTE_MINI = config.FECHA_CORTE_MINI          # 2016-01-01
EPOCH_MINI = fecha_a_epoch(CORTE_MINI)
EPOCH_2017 = fecha_a_epoch("2017-01-01")


@pytest.fixture(scope="module")
def ratings_mini(ruta_mini):
    return pd.read_csv(ruta_mini / "ratings.csv")


def _herr(ruta_mini, tmp_path, corte=None):
    return Herramientas(ruta_mini, tmp_path / "art", tmp_path / "estado.db", corte=corte)


@pytest.fixture(params=["completo", "demo"])
def modo(request, monkeypatch):
    """Corre la prueba con MODO=completo y con MODO=demo: el corte no depende del modo."""
    monkeypatch.setattr(config, "MODO", request.param)
    return request.param


# --------------------------------------------------------------------------- #
# Corte por defecto
# --------------------------------------------------------------------------- #

def test_corte_por_defecto(ruta_mini, tmp_path, modo):
    assert corte_por_defecto(ruta_mini) == CORTE_MINI
    assert corte_por_defecto(tmp_path) == config.FECHA_CORTE
    assert _herr(ruta_mini, tmp_path).corte == CORTE_MINI
    assert Herramientas(tmp_path, tmp_path, tmp_path / "e.db").corte == config.FECHA_CORTE
    assert _herr(ruta_mini, tmp_path, corte="2015-06-01").corte == "2015-06-01"
    with pytest.raises(ValueError):
        _herr(ruta_mini, tmp_path, corte="2015-13-01")


# --------------------------------------------------------------------------- #
# Historial, ratings calificados, tags y popularidad
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("uid", [12, 15])
def test_ratings_2017_no_aparecen(ruta_mini, tmp_path, ratings_mini, modo, uid):
    """Los ratings de 2017 de los usuarios 12 y 15 quedan fuera del historial."""
    propios = ratings_mini[ratings_mini["userId"] == uid]
    futuros = set(propios.loc[propios["timestamp"] >= EPOCH_2017, "movieId"])
    assert futuros, "la muestra mini debe tener ratings de 2017 para este usuario"
    previos = propios[propios["timestamp"] < EPOCH_MINI]

    herr = _herr(ruta_mini, tmp_path)
    hist = herr._historial(uid)
    assert {f["movieId"] for f in hist} == set(previos["movieId"])
    assert all(f["timestamp"] < EPOCH_MINI for f in hist)

    calif = herr.peliculas_calificadas(uid, n=200)
    vistos = {p["movieId"] for p in calif["peliculas"]}
    assert calif["total"] == len(previos)
    assert not vistos & futuros
    assert all(p["fecha"] < CORTE_MINI for p in calif["peliculas"])


def test_corte_explicito_cambia_el_historial(ruta_mini, tmp_path, ratings_mini, modo):
    """Con un corte posterior (2018) los ratings de 2017 sí aparecen: el filtro es el corte."""
    propios = ratings_mini[ratings_mini["userId"] == 12]
    esperado = set(propios.loc[propios["timestamp"] < fecha_a_epoch("2018-01-01"), "movieId"])
    herr = _herr(ruta_mini, tmp_path, corte="2018-01-01")
    assert {f["movieId"] for f in herr._historial(12)} == esperado


def test_tags_respetan_el_corte(ruta_mini, tmp_path, modo):
    tags = pd.read_csv(ruta_mini / "tags.csv")
    herr = _herr(ruta_mini, tmp_path)
    for uid in sorted(tags["userId"].unique()):
        propios = tags[tags["userId"] == uid]
        res = herr.tags_usuario(int(uid), n=200)
        assert res["total"] == int((propios["timestamp"] < EPOCH_MINI).sum())
        posteriores = set(propios.loc[propios["timestamp"] >= EPOCH_MINI, "tag"]) - \
            set(propios.loc[propios["timestamp"] < EPOCH_MINI, "tag"])
        assert not posteriores & {t["tag"] for t in res["tags"]}


def test_popularidad_respeta_el_corte(ruta_mini, tmp_path, ratings_mini, modo):
    previos = ratings_mini[ratings_mini["timestamp"] < EPOCH_MINI]
    solo_futuras = set(ratings_mini["movieId"]) - set(previos["movieId"])
    pop = _herr(ruta_mini, tmp_path)._popularidad()
    assert set(pop) == set(previos["movieId"])
    assert not set(pop) & solo_futuras
    conteos = previos["movieId"].value_counts()
    assert conteos[pop[0]] == conteos.max()


# --------------------------------------------------------------------------- #
# Eventos en vivo
# --------------------------------------------------------------------------- #

def test_eventos_en_vivo_se_incluyen_siempre(ruta_mini, tmp_path, ratings_mini, modo):
    """Un evento de estado.db posterior al corte aparece igual en el historial del usuario."""
    con = estado.conectar(tmp_path / "estado.db")
    ts = int(time.time())                       # muy posterior a cualquier corte
    con.execute("INSERT INTO eventos (userId, movieId, rating, timestamp, recibido_en) "
                "VALUES (?,?,?,?,?)", (12, 999999, 4.5, ts, float(ts)))
    con.commit()
    con.close()
    herr = _herr(ruta_mini, tmp_path)
    calif = herr.peliculas_calificadas(12, n=200)
    vivos = [p for p in calif["peliculas"] if p["fuente"] == "en vivo"]
    assert [p["movieId"] for p in vivos] == [999999]
    previos = ratings_mini[(ratings_mini["userId"] == 12) &
                           (ratings_mini["timestamp"] < EPOCH_MINI)]
    assert calif["total"] == len(previos) + 1


# --------------------------------------------------------------------------- #
# demo.json relativo
# --------------------------------------------------------------------------- #

def test_demo_json_relativo(ruta_mini, tmp_path):
    base = tmp_path / "base"
    base.mkdir()
    pd.DataFrame({"id": [1], "features": [[0.1, 0.2]]}).to_parquet(base / "item_factors.parquet")
    salida = RAIZ / "artefactos" / "_prueba_demo_relativo"
    try:
        demo.preparar_demo(base=base, datos=ruta_mini, desde="2015-01-01", salida=salida,
                           corte=CORTE_MINI, log=lambda *_: None)
        texto = (salida / "demo.json").read_text(encoding="utf-8")
        info = json.loads(texto)
        for clave in ("base", "datos"):
            assert not re.match(r"^([A-Za-z]:|[/\\])", info[clave]), info[clave]
        assert "C:" not in texto and ":\\\\" not in texto
        assert info["datos"] == "muestras/ml-mini"
        leido = demo.leer_demo_json(salida)
        assert leido["datos"] == ruta_mini.resolve()
        assert leido["base"] == base.resolve()
        assert leido["corte"] == CORTE_MINI
    finally:
        import shutil
        shutil.rmtree(salida, ignore_errors=True)


def test_resolver_ruta_contra_la_raiz():
    assert demo.resolver_ruta("muestras/ml-mini") == (RAIZ / "muestras" / "ml-mini").resolve()
    assert demo.ruta_relativa(RAIZ / "muestras" / "ml-mini") == "muestras/ml-mini"
    assert demo.leer_demo_json(RAIZ / "no_existe_carpeta") is None


# --------------------------------------------------------------------------- #
# Valores por defecto de los scripts de los 25M
# --------------------------------------------------------------------------- #

def test_productor_desde_por_defecto_es_el_corte():
    import importlib.util

    spec = importlib.util.spec_from_file_location("productor_kafka",
                                                  RAIZ / "scripts" / "productor_kafka.py")
    prod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(prod)
    args = prod.construir_parser().parse_args([])
    assert args.desde == config.FECHA_CORTE
    assert prod.construir_parser().parse_args(["--desde", "2015-01-01"]).desde == "2015-01-01"


def test_correr_25m_pasa_hasta_a_perfilar():
    texto = (RAIZ / "correr_25m.ps1").read_text(encoding="utf-8")
    linea = next(l for l in texto.splitlines() if "perfilar_usuarios.py" in l and "args" in l)
    assert '"--hasta", $corte' in linea
    assert "config.FECHA_CORTE" in texto
