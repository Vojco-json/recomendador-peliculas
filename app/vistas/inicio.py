"""Inicio: tipo de espectador y las tres filas de carátulas personalizadas."""

from __future__ import annotations

import streamlit as st

from . import tarjetas
from .comun import Contexto, caratulas_clicables, esc, fila_html
from .ficha import CLAVE_MEMORIA_LLM


def tipo_html(tipo: dict | None) -> str:
    """Línea «Tu tipo de espectador: <badge>» (o una invitación a calificar)."""
    if not tipo:
        return ('<div class="cr-tipo-linea">Tu tipo de espectador: <span class="cr-tipo '
                'vacio">aún sin calcular</span> · califica algunas películas</div>')
    return (f'<div class="cr-tipo-linea">Tu tipo de espectador: <span class="cr-tipo">'
            f'{esc(tipo["tipo"])}</span> · según tus {int(tipo.get("n_ratings", 0))} '
            f'calificaciones</div>')


def mostrar(ctx: Contexto) -> None:
    mensaje = st.session_state.pop("mensaje_inicio", None)
    if mensaje:
        st.success(mensaje)
    ratings = ctx.cat.ratings_usuario(ctx.user_id)
    tipo = ctx.cat.tipo(ctx.user_id, ratings) if ratings else None
    st.markdown(tipo_html(tipo), unsafe_allow_html=True)
    filas = ctx.cat.filas_inicio(ctx.user_id)
    if not ratings:
        st.caption("Todavía no calificas nada: te mostramos lo más visto. Califica en el "
                   "primer paso o en la ficha de cada película para personalizar esto.")
    html_filas = "".join(fila_html(ctx, f["titulo"], f["peliculas"], f["clave"])
                         for f in filas if f["peliculas"])
    if html_filas:
        # Carátulas clicables: abren la ficha global sin recargar la página.
        caratulas_clicables(html_filas, "inicio", "Inicio")
    # Tarjeta 'Para ti': una probable y una improbable, explicadas con explicar().
    if ratings:
        t = ctx.cat.tarjeta_para_ti(ctx.user_id)
        textos = tarjetas.redactar_par(t)      # una sola llamada para las dos
        st.markdown(tarjetas.para_ti_html(t, textos), unsafe_allow_html=True)
