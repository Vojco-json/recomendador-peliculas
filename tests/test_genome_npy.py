"""Requisito 91: genome-scores.csv se convierte una vez en genome.npy + índices.

Con la muestra mini (copiada a una carpeta temporal, para no tocar artefactos reales):
- construir() deja genome.npy y genome_indices.json con los mismos valores que el CSV;
- cargar() la construye si falta y no la reconstruye si el CSV no cambió;
- herramientas._genome y perfiles.features_tipo leen desde la matriz, no del CSV.
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pandas as pd
import pytest

from recomendador import config, genome, herramientas, perfiles


@pytest.fixture
def datos(tmp_path, monkeypatch, ruta_mini):
    """Copia de la muestra mini y artefactos en tmp (la matriz se construye ahí)."""
    destino = tmp_path / "ml"
    shutil.copytree(ruta_mini, destino)
    monkeypatch.setattr(config, "ARTEFACTOS", tmp_path / "art")
    genome._abrir.cache_clear()
    herramientas._genome.cache_clear()
    return destino


def test_construir_mismos_valores_que_el_csv(datos, tmp_path):
    salida = genome.construir(datos, tmp_path / "g")
    assert (salida / genome.NOMBRE_MATRIZ).exists()
    indices = json.loads((salida / genome.NOMBRE_INDICES).read_text(encoding="utf-8"))
    matriz = np.load(salida / genome.NOMBRE_MATRIZ)
    assert matriz.dtype == np.float32
    assert matriz.shape == (len(indices["movieIds"]), len(indices["tagIds"]))

    csv = pd.read_csv(datos / "genome-scores.csv")
    tags = pd.read_csv(datos / "genome-tags.csv")
    assert indices["tags"] == [str(t).lower() for t in
                               tags.set_index("tagId").loc[indices["tagIds"], "tag"]]
    g = genome.Genome(matriz, indices)
    rel = g.relevancias(indices["tags"])
    # Cada fila del CSV aparece con su valor exacto (5 decimales).
    nombre = dict(zip(tags["tagId"], tags["tag"].str.lower()))
    for m, t, r in csv.sample(min(len(csv), 500), random_state=0).itertuples(index=False):
        assert rel[nombre[t]][int(m)] == pytest.approx(round(float(r), 5), abs=1e-6)
    assert sum(len(v) for v in rel.values()) == len(csv)


def test_cargar_construye_una_sola_vez(datos, monkeypatch):
    carpeta = genome.carpeta_destino(datos)
    assert not (carpeta / genome.NOMBRE_MATRIZ).exists()
    llamadas = []
    original = genome.construir
    monkeypatch.setattr(genome, "construir",
                        lambda *a, **k: llamadas.append(1) or original(*a, **k))
    g1 = genome.cargar(datos)
    assert g1 is not None and (carpeta / genome.NOMBRE_MATRIZ).exists()
    g2 = genome.cargar(datos)
    assert g2 is g1 and len(llamadas) == 1          # segunda vez: desde caché, sin CSV


def test_herramientas_y_perfiles_usan_la_matriz(datos, monkeypatch):
    tags = pd.read_csv(datos / "genome-tags.csv")["tag"].str.lower().tolist()[:2]
    csv = pd.read_csv(datos / "genome-scores.csv")
    genome.cargar(datos)                             # matriz construida

    # Desde aquí, leer el CSV de scores con pandas o pyarrow es un error.
    real_read_csv = pd.read_csv

    def sin_scores(ruta, *a, **k):
        assert "genome-scores" not in str(ruta), "se volvió a leer genome-scores.csv"
        return real_read_csv(ruta, *a, **k)

    monkeypatch.setattr(pd, "read_csv", sin_scores)
    monkeypatch.setattr(genome, "construir",
                        lambda *a, **k: pytest.fail("no debía reconstruir genome.npy"))

    rs, rt = datos / "genome-scores.csv", datos / "genome-tags.csv"
    rel = herramientas._genome(str(rt), str(rs), tuple(tags), 1.0, 2.0)
    assert set(rel) == set(tags)
    primero = csv.iloc[0]
    nombre = dict(zip(real_read_csv(rt)["tagId"], real_read_csv(rt)["tag"].str.lower()))
    if nombre[primero.tagId] in rel:
        assert rel[nombre[primero.tagId]][int(primero.movieId)] == pytest.approx(
            float(primero.relevance), abs=1e-6)

    feats = perfiles.features_tipo(datos, [f"tag:{t}" for t in tags] + ["genero:Drama"])
    con_genome = {m: r for m, (_, r) in feats.items() if r is not None}
    assert con_genome, "features_tipo no trajo relevancias desde genome.npy"
    m, r = next(iter(con_genome.items()))
    for t in tags:
        assert r[t] == pytest.approx(rel[t][m], abs=1e-6)
