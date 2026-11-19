"""Primer ingreso: calificar ~10 populares o importar el CSV de Letterboxd.

Al guardar, las notas entran como eventos (Kafka o respaldo directo) y en la
siguiente ejecución Inicio ya muestra el fold-in y el tipo de espectador.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import streamlit as st

from recomendador import config

from .comun import Contexto, calificar_varias, esc, estrellas, importar_letterboxd

N_POPULARES = 10                 # películas que se ofrecen para calificar
MIN_CALIFICACIONES = 3           # mínimo para salir del onboarding calificando
NOTAS = [None] + [x / 2 for x in range(1, 11)]          # None = no la vi
CSV_EJEMPLO = config.RAIZ / "muestras" / "letterboxd_mini.csv"
CLAVE_OMITIDO = "onboarding_omitido"


def populares(ctx: Contexto, n: int = N_POPULARES) -> list[dict]:
    """Las n películas más votadas antes del corte (las más fáciles de haber visto)."""
    ids = ctx.cat._peliculas()["populares"][:n]
    return [f for f in (ctx.cat.ficha_corta(m) for m in ids) if f]


CLAVE_AVISOS = "onboarding_avisos"   # [(nivel, texto)] para mostrar tras un callback


def _avisar(nivel: str, texto: str) -> None:
    st.session_state.setdefault(CLAVE_AVISOS, []).append((nivel, texto))


# Las acciones van en callbacks (on_click): corren ANTES de la siguiente ejecución,
# así esa ejecución ya muestra Inicio sin un st.rerun() adicional.

def _guardar(ctx: Contexto, ids: list[int]) -> None:
    notas = [(m, st.session_state.get(f"onb_{m}")) for m in ids]
    notas = [(m, float(r)) for m, r in notas if r is not None]
    if len(notas) < MIN_CALIFICACIONES:
        _avisar("warning", f"Califica al menos {MIN_CALIFICACIONES} películas para empezar.")
        return
    r = calificar_varias(ctx.user_id, notas, ctx.estado_db, ctx.limitador)
    for aviso in r["avisos"]:
        _avisar("warning" if r["limite"] else "info", aviso)
    if r["registradas"] and not r["limite"]:
        st.session_state["mensaje_inicio"] = (
            f"Guardamos tus {len(r['registradas'])} calificaciones.")


def _importar(ctx: Contexto, ejemplo: bool) -> None:
    if ejemplo:
        r = importar_letterboxd(CSV_EJEMPLO, ctx.user_id, ctx.datos, ctx.estado_db,
                                ctx.limitador)
    else:
        archivo = st.session_state.get("onb_csv")
        if archivo is None:
            _avisar("warning", "Primero elige el archivo ratings.csv.")
            return
        with tempfile.TemporaryDirectory() as tmp:
            ruta = Path(tmp) / "ratings.csv"
            ruta.write_bytes(archivo.getvalue())
            r = importar_letterboxd(ruta, ctx.user_id, ctx.datos, ctx.estado_db,
                                    ctx.limitador)
    if not r["ok"]:
        _avisar("error", r["error"])
        return
    for aviso in r["avisos"]:
        _avisar("info", aviso)
    st.session_state["mensaje_inicio"] = (
        f"Importamos {len(r['importadas'])} calificaciones de Letterboxd."
        + (f" No encontramos: {', '.join(r['sueltas'][:5])}." if r["sueltas"] else ""))


def _saltar() -> None:
    st.session_state[CLAVE_OMITIDO] = True


def mostrar_avisos() -> None:
    """Muestra (y consume) los avisos que dejaron los callbacks del onboarding."""
    for nivel, texto in st.session_state.pop(CLAVE_AVISOS, []):
        {"warning": st.warning, "info": st.info, "error": st.error}[nivel](texto)


def mostrar(ctx: Contexto) -> None:
    usuario = ctx.usuario
    st.markdown('<div class="cr-paso">Primer paso</div>', unsafe_allow_html=True)
    st.subheader(f"Hola, {usuario['nombre']}: cuéntanos qué has visto")
    st.caption("Califica las que hayas visto (con 3 basta; con 10 aciertas más) o importa "
               "tu historial de Letterboxd. Con eso calculamos tus recomendaciones y tu "
               "tipo de espectador al instante.")

    mostrar_avisos()
    calificar, importar = st.tabs(["Calificar populares", "Importar de Letterboxd"])

    # ---- calificar las populares --------------------------------------- #
    with calificar:
        pelis = populares(ctx)
        por_fila = 5
        for i in range(0, len(pelis), por_fila):
            cols = st.columns(por_fila)
            for col, p in zip(cols, pelis[i:i + por_fila]):
                with col:
                    st.markdown(
                        f'<div class="cr-onb"><img src="{esc(ctx.tmdb.poster_src(p))}" '
                        f'alt="Carátula de {esc(p["titulo"])}"><div class="t">'
                        f'{esc(p["titulo"])}</div></div>', unsafe_allow_html=True)
                    st.selectbox("Tu nota", NOTAS, format_func=estrellas,
                                 key=f"onb_{p['movieId']}", label_visibility="collapsed")
        n = sum(st.session_state.get(f"onb_{p['movieId']}") is not None for p in pelis)
        st.caption(f"Llevas {n} de {len(pelis)}.")
        st.button("Guardar y ver mis recomendaciones", type="primary", key="onb_guardar",
                  on_click=_guardar, args=(ctx, [p["movieId"] for p in pelis]))

    # ---- importar de Letterboxd ---------------------------------------- #
    with importar:
        st.markdown("En Letterboxd: *Settings → Import & Export → Export your data*. "
                    "Sube el archivo **ratings.csv** del ZIP.")
        archivo = st.file_uploader("CSV de Letterboxd", type=["csv"], key="onb_csv")
        c1, c2 = st.columns(2)
        with c1:
            st.button("Importar mi CSV", type="primary", key="onb_importar",
                      disabled=archivo is None, use_container_width=True,
                      on_click=_importar, args=(ctx, False))
        with c2:
            st.button("Probar con un CSV de ejemplo", key="onb_ejemplo",
                      use_container_width=True, on_click=_importar, args=(ctx, True))

    st.button("Saltar por ahora", key="onb_saltar", on_click=_saltar)
