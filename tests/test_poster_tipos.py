"""Pruebas del paquete 5: figura tipos_espectador con barras horizontales legibles.

Renderiza la figura con datos de la muestra ml-mini (sin Spark: los perfiles se arman
con pandas a partir de muestras/ml-mini) y mide con ``get_window_extent`` que ninguna
caja de texto del panel de barras se cruce con otra.
"""

from __future__ import annotations

import json
import math
import sys
from itertools import combinations
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import poster  # noqa: E402

MINI = RAIZ / "muestras" / "ml-mini"
ARTEFACTOS_MINI = RAIZ / "artefactos" / "mini"


# --------------------------------------------------------------------------- #
# Datos de la muestra mini
# --------------------------------------------------------------------------- #

def _tipos_desde_mini(nombres: dict[int, str] | None = None, k: int = 4):
    """tipos.json + perfiles con el mismo formato que perfilar_usuarios.py.

    Cada usuario de ml-mini tiene pesos por género (fracción de sus ratings) y
    actividad; su tipo es su género dominante entre los ``k`` más frecuentes.
    ``nombres`` permite forzar nombres largos para la prueba de estrés.
    """
    ratings = pd.read_csv(MINI / "ratings.csv")
    movies = pd.read_csv(MINI / "movies.csv")
    df = ratings.merge(movies[["movieId", "genres"]], on="movieId")
    df = df.assign(genero=df["genres"].str.split("|")).explode("genero")
    df = df[df["genero"] != "(no genres listed)"]
    top = df["genero"].value_counts().index[:k].tolist()
    conteo = (df[df["genero"].isin(top)].groupby(["userId", "genero"]).size()
              .unstack(fill_value=0).reindex(columns=top, fill_value=0))
    pesos = conteo.div(conteo.sum(axis=1).replace(0, 1), axis=0)
    n_rat = ratings.groupby("userId").size().reindex(pesos.index)
    cluster = pesos.to_numpy().argmax(axis=1)

    rasgos = [f"genero:{g}" for g in top] + ["actividad"]
    crudo = np.column_stack([pesos.to_numpy(), np.log1p(n_rat.to_numpy())])
    media, std = crudo.mean(axis=0), crudo.std(axis=0)
    z = np.where(std > 0, (crudo - media) / np.where(std > 0, std, 1), 0.0)

    perfiles = pd.DataFrame({
        "userId": pesos.index, "cluster": cluster, "n_ratings": n_rat.to_numpy(),
        "pesos_generos": [json.dumps(dict(zip(top, fila))) for fila in pesos.to_numpy()],
        "pesos_tags": ["{}"] * len(pesos),
    })
    tipos = {"k": k, "n_usuarios": len(perfiles), "rasgos": rasgos,
             "media_features": media.tolist(), "std_features": std.tolist(), "tipos": []}
    for c in range(k):
        miembros = z[cluster == c]
        centro = miembros.mean(axis=0) if len(miembros) else np.zeros(len(rasgos))
        nombre = (nombres or {}).get(c, f"Fans de {top[c]}")
        tipos["tipos"].append({"cluster": c, "nombre": nombre,
                               "tamaño": int((cluster == c).sum()),
                               "centroide": dict(zip(rasgos, centro.tolist()))})
    return tipos, perfiles


# --------------------------------------------------------------------------- #
# Medición de las etiquetas
# --------------------------------------------------------------------------- #

def _cajas(fig):
    """Cajas (en píxeles) de las etiquetas del panel de barras, ya dibujado."""
    fig.canvas.draw()
    rend = fig.canvas.get_renderer()
    ax = next(a for a in fig.axes if a.get_label() == "barras_tipos")
    nombres = [t for t in ax.get_yticklabels() if t.get_visible() and t.get_text()]
    valores = [t for t in ax.texts if t.get_gid() == "valor_tipo"]
    return ax, rend, ([(t.get_text(), t.get_window_extent(rend)) for t in nombres],
                      [(t.get_text(), t.get_window_extent(rend)) for t in valores])


def _se_cruzan(a, b) -> bool:
    """True si dos cajas comparten área (tocarse en el borde no cuenta)."""
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


def _comprobar_figura(fig, n_tipos: int):
    ax, rend, (nombres, valores) = _cajas(fig)
    assert len(nombres) == n_tipos, "falta la etiqueta de algún tipo"
    assert len(valores) == n_tipos, "falta el rótulo de usuarios de algún tipo"
    todas = nombres + valores
    for (ta, a), (tb, b) in combinations(todas, 2):
        assert not _se_cruzan(a, b), f"se superponen {ta!r} y {tb!r}"
    caja_ax = ax.get_window_extent(rend)
    for texto, caja in nombres:            # nombres a la izquierda del panel
        assert caja.x1 <= caja_ax.x0 + 1, f"{texto!r} no está a la izquierda"
    for texto, caja in valores:            # rótulos de valor dentro del panel
        assert caja.x1 <= caja_ax.x1 + 1, f"{texto!r} se sale del panel"


def _barras(fig):
    ax = next(a for a in fig.axes if a.get_label() == "barras_tipos")
    # Todas las barras, incluso las de ancho 0 (un tipo sin usuarios también se rotula).
    return ax, list(ax.patches)


# --------------------------------------------------------------------------- #
# Pruebas
# --------------------------------------------------------------------------- #

def test_mini_barras_horizontales_ordenadas_y_sin_superposicion():
    tipos, perfiles = _tipos_desde_mini()
    fig = poster.dibujar_tipos(tipos, perfiles)
    try:
        ax, barras = _barras(fig)
        assert len(barras) == len(tipos["tipos"])
        # barh: todas parten de x = 0 y son más altas que anchas en datos de eje y.
        assert all(b.get_x() == 0 for b in barras)
        # Ordenadas: de arriba (y mayor) hacia abajo, ancho no creciente.
        por_y = sorted(barras, key=lambda b: -b.get_y())
        anchos = [b.get_width() for b in por_y]
        assert anchos == sorted(anchos, reverse=True)
        # La etiqueta de la barra de arriba es el tipo más grande.
        mayor = max(tipos["tipos"], key=lambda t: t["tamaño"])["nombre"]
        arriba = max(ax.get_yticklabels(), key=lambda t: t.get_position()[1])
        assert arriba.get_text().replace("\n", " ") == mayor
        _comprobar_figura(fig, len(tipos["tipos"]))
    finally:
        poster._plt().close(fig)


def test_estres_diez_tipos_con_nombres_largos_no_se_superponen():
    largos = {c: n for c, n in enumerate([
        "Cinéfilos del drama psicológico y el thriller de autor",
        "Familias con animación y aventura los domingos",
        "Fanáticos de la acción, explosiones y persecuciones",
        "Románticos empedernidos de la comedia ligera",
        "Amantes del terror y el suspenso sobrenatural",
        "Documentalistas curiosos por la historia real",
        "Exploradores de ciencia ficción y mundos distópicos",
        "Nostálgicos del cine clásico en blanco y negro",
        "Musicales, fantasía y cuentos para toda la vida",
        "Espectadores ocasionales de grandes éxitos",
    ])}
    tipos, perfiles = _tipos_desde_mini(nombres=largos, k=10)
    fig = poster.dibujar_tipos(tipos, perfiles)
    try:
        _comprobar_figura(fig, 10)
    finally:
        poster._plt().close(fig)


def test_artefactos_reales_de_la_muestra_si_existen():
    """Con artefactos/mini (perfilar_usuarios sobre ml-mini) también se lee bien."""
    if not (ARTEFACTOS_MINI / "tipos.json").exists():
        pytest.skip("No hay artefactos de la muestra (artefactos/mini)")
    tipos = json.loads((ARTEFACTOS_MINI / "tipos.json").read_text(encoding="utf-8"))
    ruta_p = ARTEFACTOS_MINI / "perfiles.parquet"
    perfiles = pd.read_parquet(ruta_p) if ruta_p.exists() else None
    fig = poster.dibujar_tipos(tipos, perfiles)
    try:
        _comprobar_figura(fig, len(tipos["tipos"]))
    finally:
        poster._plt().close(fig)


def test_figura_tipos_guarda_png_y_svg(tmp_path):
    tipos, perfiles = _tipos_desde_mini()
    rutas = poster.figura_tipos(tipos, perfiles, tmp_path)
    assert [r.name for r in rutas] == ["tipos_espectador.png", "tipos_espectador.svg"]
    assert all(r.stat().st_size > 5_000 for r in rutas)
    svg = rutas[1].read_text(encoding="utf-8")
    for t in tipos["tipos"]:
        primera = poster._envolver(t["nombre"]).split("\n")[0]
        assert primera in svg
    assert "usuarios ·" in svg
    assert not math.isnan(sum(t["tamaño"] for t in tipos["tipos"]))
