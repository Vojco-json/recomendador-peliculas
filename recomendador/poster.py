"""Figuras del póster (matplotlib, PNG a 300 dpi y SVG con texto editable).

Identidad visual común (ARQUITECTURA.md, «Diseño»):
- fondo oscuro de cine #141414, paneles #1F1F1F;
- rojo #E50914 para la serie principal (ALS, speedup medido);
- ámbar #F5C518 para métricas y valores destacados;
- texto #F2F2F2 (principal) y #B3B3B3 (secundario);
- paleta tab10 para los tipos de espectador (apta para daltónicos);
- DejaVu Sans, nada por debajo de 14 pt, títulos en español.

Funciones públicas (cada una guarda ``<nombre>.png`` y ``<nombre>.svg`` y devuelve
las rutas):
- ``figura_speedup(resultado, carpeta)``        speedup medido vs ideal + tiempos.
- ``figura_metricas(metricas, carpeta)``        ALS vs popularidad (RMSE, P@10, NDCG@10).
- ``figura_tipos(tipos, perfiles, carpeta)``    PCA 2D de los tipos de espectador.
- ``figura_arquitectura(carpeta)``              diagrama de componentes.
- ``generar_todas(artefactos, carpeta)``        las cuatro; si falta un artefacto, avisa
                                                y sigue con las demás.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

# --------------------------------------------------------------------------- #
# Paleta y tipografía
# --------------------------------------------------------------------------- #

FONDO = "#141414"
PANEL = "#1F1F1F"
REJILLA = "#333333"
ROJO = "#E50914"
AMBAR = "#F5C518"
TEXTO = "#F2F2F2"
TEXTO_2 = "#B3B3B3"
GRIS = "#8C8C8C"
FUENTE = "DejaVu Sans"
PT_MIN = 14          # ningún texto del póster por debajo de este tamaño
DPI = 300
CPU = "Ryzen 5 3600 (6 núcleos / 12 hilos), 16 GB RAM"


def _plt():
    """Importa pyplot con el backend sin ventana (sirve en pruebas y sin pantalla)."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def estilo() -> dict:
    """rcParams del póster (se aplican con ``matplotlib.rc_context``)."""
    return {
        "font.family": FUENTE,
        "font.size": 16,
        "axes.titlesize": 20,
        "axes.titleweight": "bold",
        "axes.labelsize": 16,
        "xtick.labelsize": 15,
        "ytick.labelsize": 15,
        "legend.fontsize": 15,
        "legend.title_fontsize": 15,
        "figure.titlesize": 26,
        "figure.titleweight": "bold",
        "figure.facecolor": FONDO,
        "savefig.facecolor": FONDO,
        "axes.facecolor": PANEL,
        "axes.edgecolor": REJILLA,
        "axes.labelcolor": TEXTO,
        "axes.titlecolor": TEXTO,
        "text.color": TEXTO,
        "xtick.color": TEXTO_2,
        "ytick.color": TEXTO_2,
        "grid.color": REJILLA,
        "grid.linewidth": 1.0,
        "legend.facecolor": PANEL,
        "legend.edgecolor": REJILLA,
        "legend.labelcolor": TEXTO,
        "axes.spines.top": False,
        "axes.spines.right": False,
        # Texto como texto en el SVG: editable en Inkscape/Illustrator y verificable.
        "svg.fonttype": "none",
    }


def guardar(fig, carpeta, nombre: str) -> list[Path]:
    """Guarda ``nombre``.png (300 dpi) y ``nombre``.svg en ``carpeta``."""
    plt = _plt()
    carpeta = Path(carpeta)
    carpeta.mkdir(parents=True, exist_ok=True)
    rutas = [carpeta / f"{nombre}.png", carpeta / f"{nombre}.svg"]
    fig.savefig(rutas[0], dpi=DPI, bbox_inches="tight", pad_inches=0.3)
    fig.savefig(rutas[1], bbox_inches="tight", pad_inches=0.3)
    plt.close(fig)
    return rutas


def _miles(n) -> str:
    """12345 -> '12.345' (separador de miles chileno)."""
    return f"{int(n):,}".replace(",", ".")


def _decimal(x: float, d: int = 2) -> str:
    """1.5 -> '1,50' (coma decimal)."""
    return f"{x:.{d}f}".replace(".", ",")


# --------------------------------------------------------------------------- #
# Speedup
# --------------------------------------------------------------------------- #

def calcular_speedup(tiempos: dict) -> list[dict]:
    """Speedup a partir de los tiempos por núcleo.

    ``tiempos``: {N: [segundos de cada repetición]}. La referencia es el menor N
    medido (normalmente 1): speedup(N) = T(ref) / T(N) * ref, ideal(N) = N, y
    eficiencia = speedup / N. Se usa la mediana de las repeticiones (robusta ante
    una corrida lenta aislada).
    """
    if not tiempos:
        raise ValueError("No hay tiempos para calcular el speedup")
    filas = []
    ns = sorted(int(n) for n in tiempos)
    ref = ns[0]
    t_ref = float(np.median(tiempos[ref] if ref in tiempos else tiempos[str(ref)]))
    for n in ns:
        ts = [float(t) for t in (tiempos[n] if n in tiempos else tiempos[str(n)])]
        mediana = float(np.median(ts))
        speedup = t_ref / mediana * ref if mediana > 0 else float("nan")
        filas.append({"nucleos": n, "tiempos": ts, "mediana_s": round(mediana, 3),
                      "media_s": round(float(np.mean(ts)), 3),
                      "desv_s": round(float(np.std(ts)), 3),
                      "speedup": round(speedup, 3), "ideal": float(n),
                      "eficiencia": round(speedup / n, 3)})
    return filas


def figura_speedup(resultado: dict, carpeta, nombre: str = "speedup") -> list[Path]:
    """Speedup medido vs ideal (línea punteada) y tiempos por número de núcleos.

    ``resultado`` es el contenido de escalabilidad.json (ver scripts/escalabilidad.py).
    """
    import matplotlib

    plt = _plt()
    filas = resultado["resultados"]
    ns = np.array([f["nucleos"] for f in filas])
    sp = np.array([f["speedup"] for f in filas])
    med = np.array([f["mediana_s"] for f in filas])
    desv = np.array([f.get("desv_s", 0.0) for f in filas])

    with matplotlib.rc_context(estilo()):
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(15, 6.6),
                                       gridspec_kw={"width_ratios": [1.15, 1]})
        fig.suptitle("Escalabilidad del entrenamiento ALS en Spark", x=0.02, ha="left")

        # Panel 1: speedup medido vs ideal.
        tope = max(float(ns.max()), float(np.nanmax(sp)) if len(sp) else 1.0)
        ideal_x = np.array([ns.min(), ns.max()])
        ax1.plot(ideal_x, ideal_x, linestyle=(0, (4, 3)), color=TEXTO_2, linewidth=2.5,
                 label="Ideal (lineal)")
        ax1.plot(ns, sp, color=ROJO, marker="o", markersize=11, linewidth=3.5,
                 label="Medido")
        for x, y in zip(ns, sp):
            ax1.annotate(f"{_decimal(y)}×", (x, y), textcoords="offset points",
                         xytext=(0, 14), ha="center", fontsize=16, color=AMBAR,
                         fontweight="bold")
        ax1.set_xticks(ns)
        ax1.set_xlim(0.5, ns.max() + 0.5)
        ax1.set_ylim(0, tope * 1.18 + 0.2)
        ax1.set_xlabel("Núcleos (Spark local[N])")
        ax1.set_ylabel("Speedup (T₁ / Tₙ)")
        ax1.set_title("Speedup medido vs ideal", loc="left")
        ax1.grid(True, axis="y")
        ax1.legend(loc="upper left", frameon=True)

        # Panel 2: tiempos (mediana y desviación de las repeticiones).
        posiciones = np.arange(len(ns))
        ax2.bar(posiciones, med, color=AMBAR, width=0.62, yerr=desv,
                error_kw={"ecolor": TEXTO, "elinewidth": 2, "capsize": 6})
        for x, y in zip(posiciones, med):
            ax2.annotate(f"{_decimal(y, 1)} s", (x, y), textcoords="offset points",
                         xytext=(0, 8), ha="center", fontsize=15, color=TEXTO)
        ax2.set_xticks(posiciones, [str(n) for n in ns])
        ax2.set_ylim(0, float(med.max() + desv.max()) * 1.22 if len(med) else 1)
        ax2.set_xlabel("Núcleos")
        ax2.set_ylabel("Tiempo de entrenamiento (s)")
        ax2.set_title("Tiempo por corrida (mediana)", loc="left")
        ax2.grid(True, axis="y")

        fraccion = resultado.get("fraccion")
        frac_txt = "100 %" if not fraccion or fraccion >= 1 else f"{_decimal(fraccion * 100, 0)} %"
        nota = (f"Datos: {resultado.get('dataset', '?')} · fracción de usuarios {frac_txt} · "
                f"{_miles(resultado.get('n_ratings', 0))} ratings · "
                f"{resultado.get('repeticiones', '?')} repetición(es) medida(s) + 1 de "
                f"calentamiento descartada\nEquipo: {CPU} · Spark local[N], driver 6 GB · "
                f"ALS rank={resultado.get('params', {}).get('rank', '?')}, "
                f"maxIter={resultado.get('params', {}).get('maxIter', '?')}")
        fig.text(0.02, -0.04, nota, ha="left", va="top", fontsize=14, color=TEXTO_2)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        return guardar(fig, carpeta, nombre)


# --------------------------------------------------------------------------- #
# Métricas ALS vs popularidad
# --------------------------------------------------------------------------- #

def figura_metricas(metricas: dict, carpeta, nombre: str = "metricas") -> list[Path]:
    """Barras ALS vs popularidad para RMSE (menor es mejor), Precision@10 y NDCG@10."""
    import matplotlib

    plt = _plt()
    k = metricas.get("k", 10)
    paneles = [("RMSE", "RMSE", "menor es mejor"),
               (f"Precision@{k}", f"Precision@{k}", "mayor es mejor"),
               (f"NDCG@{k}", f"NDCG@{k}", "mayor es mejor")]
    modelos = [("als", "ALS (MLlib)", ROJO), ("popularidad", "Popularidad", GRIS)]
    # Si la corrida trae ALS implícito e híbrido (validados en el tiempo), también van.
    if "hibrido" in metricas:
        modelos = [("als", "ALS\nexplícito", ROJO),
                   ("als_implicito", "ALS\nimplícito", AMBAR),
                   ("hibrido", "Híbrido\nALS+pop.", "#4C9BE8"),
                   ("popularidad", "Popula-\nridad", GRIS)]
    n_mod = len(modelos)
    # Posiciones según la cantidad de modelos (2 o 4), nunca fijas.
    posiciones = np.arange(n_mod)
    ancho_fig = 15 if n_mod <= 2 else 18

    with matplotlib.rc_context(estilo()):
        fig, ejes = plt.subplots(1, 3, figsize=(ancho_fig, 6.8))
        fig.suptitle("ALS frente a la línea base de popularidad", x=0.02, ha="left")
        for ax, (clave, titulo, sentido) in zip(ejes, paneles):
            # None = la métrica no aplica (p. ej. RMSE del ALS implícito o del híbrido,
            # que no predicen ratings): se rotula "n/a" en vez de dibujar un 0 engañoso.
            valores = []
            for m, _, _ in modelos:
                v = (metricas.get(m) or {}).get(clave)
                try:
                    v = float(v)
                    valores.append(None if math.isnan(v) else v)
                except (TypeError, ValueError):
                    valores.append(None)
            alturas = [v if v is not None else 0.0 for v in valores]
            barras = ax.bar(posiciones, alturas, color=[c for *_, c in modelos],
                            width=0.62 if n_mod <= 2 else 0.7)
            tope = max(alturas) if max(alturas) > 0 else 1.0
            for b, v in zip(barras, valores):
                ax.annotate(_decimal(v, 3) if v is not None else "n/a",
                            (b.get_x() + b.get_width() / 2, v or 0.0),
                            textcoords="offset points", xytext=(0, 8), ha="center",
                            fontsize=16 if n_mod <= 2 else 14,
                            color=AMBAR if v is not None else TEXTO_2, fontweight="bold")
            ax.set_xticks(posiciones, [etq for _, etq, _ in modelos],
                          fontsize=None if n_mod <= 2 else 14)
            ax.set_ylim(0, tope * 1.25)
            ax.set_title(titulo, loc="left", pad=34)
            ax.text(0, 1.02, sentido, transform=ax.transAxes, ha="left", va="bottom",
                    fontsize=14, color=TEXTO_2)
            ax.grid(True, axis="y")
        n_eval = metricas.get("als", {}).get("usuarios_evaluados")
        nota = (f"Partición temporal: entrenamiento antes del {metricas.get('corte', '?')}, "
                f"prueba desde esa fecha · {_miles(metricas.get('n_train', 0))} ratings de "
                f"entrenamiento · {_miles(metricas.get('n_test', 0))} de prueba"
                + (f" · {_miles(n_eval)} usuarios evaluados" if n_eval is not None else "")
                + f"\nRelevante = rating ≥ {_decimal(metricas.get('relevante_min', 4.0), 1)} · "
                  f"top-{k} sin películas ya vistas")
        fig.text(0.02, -0.03, nota, ha="left", va="top", fontsize=14, color=TEXTO_2)
        fig.tight_layout(rect=(0, 0, 1, 0.94))
        if sin_usuarios_evaluados(metricas):
            # Con 0 usuarios evaluados las barras valen 0 y no significan nada:
            # se marca la figura para que nadie la imprima en el póster por error.
            fig.text(0.5, 0.5, AVISO_SIN_EVALUADOS, ha="center", va="center",
                     fontsize=26, fontweight="bold", color=TEXTO, zorder=10,
                     bbox={"boxstyle": "round,pad=0.8", "facecolor": ROJO,
                           "edgecolor": AMBAR, "linewidth": 3, "alpha": 0.95})
        return guardar(fig, carpeta, nombre)


#: Texto que se superpone a la figura de métricas cuando no hubo usuarios evaluados.
AVISO_SIN_EVALUADOS = "Sin usuarios evaluados: no usar en el póster"


def sin_usuarios_evaluados(metricas: dict) -> bool:
    """True si la evaluación declara 0 usuarios evaluados (ALS o popularidad).

    Si el dato no viene (artefactos antiguos), no se asume nada y no se avisa.
    """
    for modelo in ("als", "popularidad"):
        n = (metricas.get(modelo) or {}).get("usuarios_evaluados")
        if n is not None and int(n) == 0:
            return True
    return False


# --------------------------------------------------------------------------- #
# Tipos de espectador (PCA 2D)
# --------------------------------------------------------------------------- #

def matriz_usuarios(tipos: dict, perfiles) -> tuple[np.ndarray, np.ndarray]:
    """Reconstruye las features estandarizadas de cada usuario desde perfiles.parquet.

    Mismo espacio que el KMeans de perfiles.py: pesos por género, pesos por tag genome
    y actividad = log(1 + n_ratings), estandarizados con la media y desviación
    guardadas en tipos.json (desviación 0 -> 0, como StandardScaler de Spark).
    Devuelve (X, cluster).
    """
    rasgos = tipos["rasgos"]
    media = np.asarray(tipos["media_features"], dtype=float)
    std = np.asarray(tipos["std_features"], dtype=float)
    filas = []
    for pg_txt, pt_txt, n in zip(perfiles["pesos_generos"], perfiles["pesos_tags"],
                                 perfiles["n_ratings"]):
        pg = json.loads(pg_txt) if isinstance(pg_txt, str) else dict(pg_txt or {})
        pt = json.loads(pt_txt) if isinstance(pt_txt, str) else dict(pt_txt or {})
        fila = []
        for r in rasgos:
            tipo, _, nombre = r.partition(":")
            if tipo == "genero":
                fila.append(float(pg.get(nombre, 0.0)))
            elif tipo == "tag":
                fila.append(float(pt.get(nombre, 0.0)))
            else:  # actividad
                fila.append(math.log1p(float(n)))
        filas.append(fila)
    crudo = np.asarray(filas, dtype=float)
    seguro = np.where(std > 0, std, 1.0)
    x = np.where(std > 0, (crudo - media) / seguro, 0.0)
    return x, perfiles["cluster"].to_numpy(dtype=int)


def figura_tipos(tipos: dict, perfiles, carpeta, nombre: str = "tipos_espectador",
                 max_puntos: int = 6000, seed: int = 42) -> list[Path]:
    """Proyección PCA 2D de los usuarios coloreados por tipo, con nombre y tamaño.

    ``perfiles`` (DataFrame de perfiles.parquet) es opcional: sin él solo se dibujan
    los centroides. En 25M se muestrea ``max_puntos`` usuarios para que se lea.
    """
    import matplotlib
    from matplotlib import colormaps

    plt = _plt()
    rasgos = tipos["rasgos"]
    lista = sorted(tipos["tipos"], key=lambda t: t["cluster"])
    centros = np.array([[t["centroide"].get(r, 0.0) for r in rasgos] for t in lista])

    x = cl = None
    if perfiles is not None and len(perfiles):
        x, cl = matriz_usuarios(tipos, perfiles)

    # PCA (SVD centrada) ajustada con los usuarios si los hay; si no, con los centroides.
    base = x if x is not None and len(x) >= 2 else centros
    mu = base.mean(axis=0)
    _, s, vt = np.linalg.svd(base - mu, full_matrices=False)
    comp = vt[:2] if vt.shape[0] >= 2 else np.vstack([vt, np.zeros_like(vt)])
    varianza = (s ** 2) / max(float((s ** 2).sum()), 1e-12)
    var2 = list(varianza[:2]) + [0.0] * (2 - len(varianza[:2]))
    proy_c = (centros - mu) @ comp.T

    paleta = colormaps["tab10"]
    total = sum(t["tamaño"] for t in lista) or 1
    with matplotlib.rc_context(estilo()):
        fig, ax = plt.subplots(figsize=(13, 9))
        fig.suptitle("Tipos de espectador (KMeans sobre perfiles de gusto)", x=0.02, ha="left")
        if x is not None:
            idx = np.arange(len(x))
            if len(idx) > max_puntos:
                idx = np.random.default_rng(seed).choice(idx, max_puntos, replace=False)
            p = (x[idx] - mu) @ comp.T
            colores = [paleta(int(c) % 10) for c in cl[idx]]
            ax.scatter(p[:, 0], p[:, 1], s=26 if len(idx) > 500 else 90, c=colores,
                       alpha=0.45 if len(idx) > 500 else 0.75, linewidths=0)
        maximo = max(t["tamaño"] for t in lista) or 1
        for t, (cx, cy) in zip(lista, proy_c):
            color = paleta(int(t["cluster"]) % 10)
            ax.scatter([cx], [cy], s=500 + 2500 * t["tamaño"] / maximo, color=color,
                       edgecolors=TEXTO, linewidths=2.5, zorder=3)
            texto = (f"{t['nombre']}\n{_miles(t['tamaño'])} usuarios · "
                     f"{_decimal(100 * t['tamaño'] / total, 0)} %")
            ax.annotate(texto, (cx, cy), textcoords="offset points", xytext=(0, 34),
                        ha="center", va="bottom", fontsize=15, color=TEXTO,
                        fontweight="bold", zorder=4,
                        bbox={"boxstyle": "round,pad=0.35", "fc": FONDO, "ec": color,
                              "lw": 2, "alpha": 0.9})
        ax.set_xlabel(f"Componente principal 1 ({_decimal(100 * var2[0], 0)} % de la varianza)")
        ax.set_ylabel(f"Componente principal 2 ({_decimal(100 * var2[1], 0)} %)")
        ax.grid(True)
        ax.margins(0.18)
        nota = (f"{_miles(tipos.get('n_usuarios', total))} usuarios · k = {tipos.get('k', len(lista))}"
                " · rasgos: pesos por género y tag genome según sus calificaciones, más "
                "actividad\nTamaño del círculo grande = número de usuarios del tipo"
                + (f" · se muestran {_miles(min(len(x), max_puntos))} usuarios al azar"
                   if x is not None and len(x) > max_puntos else ""))
        fig.text(0.02, -0.02, nota, ha="left", va="top", fontsize=14, color=TEXTO_2)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        return guardar(fig, carpeta, nombre)


# --------------------------------------------------------------------------- #
# Arquitectura
# --------------------------------------------------------------------------- #

# Componentes: clave -> (x, y, título, subtítulo, color del borde). Coordenadas en un
# lienzo de 16 x 9.
_CAJAS = {
    "movielens": (1.6, 7.0, "MovieLens 25M", "ratings.csv · movies · genome", TEXTO_2),
    "productor": (1.6, 4.3, "Productor Kafka", "Python · orden por timestamp", TEXTO_2),
    "kafka": (5.3, 4.3, "Apache Kafka", "topic «ratings»", ROJO),
    "batch": (5.3, 7.0, "Spark batch (MLlib)", "ALS · perfiles · KMeans", ROJO),
    "streaming": (9.0, 4.3, "Spark Structured\nStreaming", "fold-in · reentreno periódico",
                  ROJO),
    "artefactos": (9.0, 7.0, "Artefactos", "modelo ALS · perfiles · tipos", AMBAR),
    "sqlite": (9.0, 1.6, "SQLite (WAL)", "estado en vivo", AMBAR),
    "app": (13.2, 4.3, "App Streamlit", "tarjetas + chat", TEXTO),
    "ollama": (13.2, 1.6, "Ollama local", "qwen3.5:9b · herramientas", TEXTO),
    "enriq": (13.2, 7.0, "IMDb · TMDb", "duración · póster (opcional)", TEXTO_2),
}
_FLECHAS = [
    ("movielens", "batch", ""),
    ("movielens", "productor", ""),
    ("productor", "kafka", ""),
    ("kafka", "streaming", ""),
    ("batch", "artefactos", ""),
    ("artefactos", "streaming", ""),
    ("streaming", "sqlite", "recs y perfiles"),
    ("artefactos", "app", ""),
    ("sqlite", "app", ""),
    ("ollama", "app", ""),
    ("enriq", "app", ""),
]
_ANCHO, _ALTO = 3.1, 1.55


def _borde(origen, destino):
    """Punto en el borde de la caja ``origen`` en dirección a ``destino``."""
    (x0, y0), (x1, y1) = origen, destino
    dx, dy = x1 - x0, y1 - y0
    if dx == 0 and dy == 0:
        return x0, y0
    fx = (_ANCHO / 2) / abs(dx) if dx else math.inf
    fy = (_ALTO / 2) / abs(dy) if dy else math.inf
    f = min(fx, fy)
    return x0 + dx * f, y0 + dy * f


def figura_arquitectura(carpeta, nombre: str = "arquitectura") -> list[Path]:
    """Diagrama de componentes: Kafka, Spark batch y streaming, SQLite, Streamlit, Ollama."""
    import matplotlib
    from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

    plt = _plt()
    with matplotlib.rc_context(estilo()):
        fig, ax = plt.subplots(figsize=(16, 9.4))
        ax.set_facecolor(FONDO)
        ax.set_xlim(-0.2, 15.2)
        ax.set_ylim(0.0, 8.65)
        ax.axis("off")
        fig.suptitle("Arquitectura del recomendador distribuido", x=0.02, ha="left")

        # Zonas de fondo: batch (arriba) y tiempo real (abajo).
        for y0, alto, rotulo in ((5.95, 2.25, "Batch (sobre los 25M)"),
                                 (0.55, 4.75, "Tiempo real (demo en vivo)")):
            ax.add_patch(FancyBboxPatch((-0.05, y0), 11.3, alto,
                                        boxstyle="round,pad=0,rounding_size=0.25",
                                        fc="#191919", ec=REJILLA, lw=1.5, zorder=0))
            # Rótulo sobre el borde superior de la zona, centrado en la columna de
            # Kafka/Spark batch, donde no pasan flechas verticales.
            ax.text(5.3, y0 + alto + 0.06, rotulo, ha="center", va="bottom", fontsize=15,
                    color=TEXTO_2, style="italic", zorder=1)

        for clave, (x, y, titulo, sub, color) in _CAJAS.items():
            ax.add_patch(FancyBboxPatch((x - _ANCHO / 2, y - _ALTO / 2), _ANCHO, _ALTO,
                                        boxstyle="round,pad=0.02,rounding_size=0.18",
                                        fc=PANEL, ec=color, lw=3, zorder=2))
            dos_lineas = "\n" in titulo
            ax.text(x, y + (0.2 if dos_lineas else 0.17), titulo, ha="center",
                    va="center", fontsize=16 if dos_lineas else 17, fontweight="bold",
                    color=TEXTO, zorder=3, linespacing=1.0)
            ax.text(x, y - (0.5 if dos_lineas else 0.33), sub, ha="center", va="center",
                    fontsize=14, color=TEXTO_2, zorder=3)

        for a, b, etiqueta in _FLECHAS:
            pa, pb = _CAJAS[a][:2], _CAJAS[b][:2]
            inicio, fin = _borde(pa, pb), _borde(pb, pa)
            ax.add_patch(FancyArrowPatch(inicio, fin, arrowstyle="-|>", mutation_scale=24,
                                         color=AMBAR if b == "app" else TEXTO_2, lw=2.4,
                                         zorder=1, shrinkA=4, shrinkB=4))
            if etiqueta:
                mx, my = (inicio[0] + fin[0]) / 2, (inicio[1] + fin[1]) / 2
                vertical = abs(fin[0] - inicio[0]) < 0.1
                ax.text(mx + (0.12 if vertical else 0), my + (0 if vertical else 0.2),
                        etiqueta, ha="left" if vertical else "center", va="center",
                        fontsize=14, color=TEXTO_2, zorder=3)

        ax.text(-0.05, 0.3, "El LLM solo explica con datos obtenidos por herramientas "
                "(historial, perfil, recomendaciones); no inventa películas.",
                ha="left", va="center", fontsize=14, color=TEXTO_2)
        fig.tight_layout(rect=(0, 0, 1, 0.95))
        return guardar(fig, carpeta, nombre)


# --------------------------------------------------------------------------- #
# Todas las figuras
# --------------------------------------------------------------------------- #

def _leer_json(ruta: Path) -> dict:
    return json.loads(Path(ruta).read_text(encoding="utf-8"))


def generar_todas(artefactos, carpeta, escalabilidad=None, log=print) -> dict:
    """Genera las figuras que se puedan. Si falta un artefacto, avisa y sigue.

    artefactos:    carpeta con metricas.json, tipos.json, perfiles.parquet y, si se corrió
                   el experimento, escalabilidad.json.
    escalabilidad: ruta alternativa a escalabilidad.json (opcional).
    Devuelve {'generadas': {nombre: [rutas]}, 'avisos': [textos]}.
    """
    artefactos, carpeta = Path(artefactos), Path(carpeta)
    generadas: dict[str, list[Path]] = {}
    avisos: list[str] = []

    def _aviso(texto: str) -> None:
        avisos.append(texto)
        log(f"AVISO: {texto}")

    generadas["arquitectura"] = figura_arquitectura(carpeta)

    ruta_m = artefactos / "metricas.json"
    if ruta_m.exists():
        generadas["metricas"] = figura_metricas(_leer_json(ruta_m), carpeta)
    else:
        _aviso(f"falta {ruta_m} (corre scripts/entrenar_batch.py): se omite metricas.png")

    ruta_t = artefactos / "tipos.json"
    if ruta_t.exists():
        perfiles = None
        ruta_p = artefactos / "perfiles.parquet"
        if ruta_p.exists():
            import pandas as pd

            perfiles = pd.read_parquet(ruta_p, columns=["userId", "cluster", "n_ratings",
                                                        "pesos_generos", "pesos_tags"])
        else:
            _aviso(f"falta {ruta_p}: tipos_espectador.png muestra solo los centroides")
        generadas["tipos_espectador"] = figura_tipos(_leer_json(ruta_t), perfiles, carpeta)
    else:
        _aviso(f"falta {ruta_t} (corre scripts/perfilar_usuarios.py): "
               "se omite tipos_espectador.png")

    ruta_e = Path(escalabilidad) if escalabilidad else artefactos / "escalabilidad.json"
    if ruta_e.exists():
        generadas["speedup"] = figura_speedup(_leer_json(ruta_e), carpeta)
    else:
        _aviso(f"falta {ruta_e} (corre scripts/escalabilidad.py): se omite speedup.png")

    return {"generadas": generadas, "avisos": avisos}
