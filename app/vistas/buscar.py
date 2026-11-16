"""Buscar: por título, director o actor (sin tildes ni mayúsculas) y abrir la ficha."""

from __future__ import annotations

import streamlit as st

from . import ficha
from .comun import Contexto, caratula_html, esc

COLUMNAS = 5
CLAVE_FICHA = "buscar_ficha"      # st.session_state: (consulta, movieId) abierta


def _como(r: dict) -> str:
    """Por qué apareció: «Director: Christopher Nolan», «Actor: …» o el título."""
    if r.get("persona"):
        rol = "Director" if r["coincidencia"] == "director" else "Reparto"
        return f"{rol}: {r['persona']}"
    return "Título parecido" if r["coincidencia"] == "título parecido" else "Por título"


def mostrar(ctx: Contexto) -> None:
    consulta = st.text_input("Busca por título, director o actor", key="buscar_q",
                             placeholder="Ej.: Nolan, Tom Hanks, Toy Story")
    if not consulta:
        st.caption("Escribe y presiona Enter. No importan las tildes ni las mayúsculas.")
        return

    abierta = st.session_state.get(CLAVE_FICHA)
    if abierta and abierta[0] == consulta:
        if st.button("← Volver a los resultados", key="buscar_volver"):
            st.session_state.pop(CLAVE_FICHA, None)
            st.rerun()
        ficha.mostrar(ctx, abierta[1], prefijo="bus")
        return

    resultados = ctx.cat.buscar(consulta)
    if not resultados:
        st.info("No encontramos películas con esa búsqueda. Prueba con otro título o nombre.")
        return
    st.markdown(f'<h3 class="cr-sub">{len(resultados)} resultados para «{esc(consulta)}»</h3>',
                unsafe_allow_html=True)
    for i in range(0, len(resultados), COLUMNAS):
        columnas = st.columns(COLUMNAS)
        for col, r in zip(columnas, resultados[i:i + COLUMNAS]):
            with col:
                st.markdown(f'<div class="cr-resultado">{caratula_html(ctx, r)}'
                            f'<div class="como">{esc(_como(r))}</div></div>',
                            unsafe_allow_html=True)
                if st.button("Ver ficha", key=f"ver_{r['movieId']}", use_container_width=True):
                    st.session_state[CLAVE_FICHA] = (consulta, int(r["movieId"]))
                    st.rerun()
