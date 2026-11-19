"""Buscar: por título, director o actor (sin tildes ni mayúsculas).

Cada carátula de los resultados es clicable (componente de comun.py) y abre la ficha
global (clave de sesión «ficha_abierta»); «← Volver» regresa a estos resultados.
"""

from __future__ import annotations

import streamlit as st

from .comun import Contexto, caratula_html, caratulas_clicables, esc, precargar_posters

ORIGEN = "Buscar"


def _como(r: dict) -> str:
    """Por qué apareció: «Director: Christopher Nolan», «Actor: …» o el título."""
    if r.get("persona"):
        rol = "Director" if r["coincidencia"] == "director" else "Reparto"
        return f"{rol}: {r['persona']}"
    return "Título parecido" if r["coincidencia"] == "título parecido" else "Por título"


def resultados_html(ctx: Contexto, resultados: list[dict]) -> str:
    """Rejilla de carátulas (cada una con data-movie, clicable) y el motivo de cada una."""
    return '<div class="cr-resultados">' + "".join(
        f'<div class="cr-resultado">{caratula_html(ctx, r)}'
        f'<div class="como">{esc(_como(r))}</div></div>' for r in resultados) + "</div>"


def mostrar(ctx: Contexto) -> None:
    consulta = st.text_input("Busca por título, director o actor", key="buscar_q",
                             placeholder="Ej.: Nolan, Tom Hanks, Toy Story")
    if not consulta:
        st.caption("Escribe y presiona Enter. No importan las tildes ni las mayúsculas.")
        return
    resultados = ctx.cat.buscar(consulta)
    if not resultados:
        st.info("No encontramos películas con esa búsqueda. Prueba con otro título o nombre.")
        return
    st.markdown(f'<h3 class="cr-sub">{len(resultados)} resultados para «{esc(consulta)}»</h3>',
                unsafe_allow_html=True)
    st.caption("Haz clic en una carátula para abrir su ficha.")
    precargar_posters(ctx, resultados)          # pósters faltantes: en paralelo, no en serie
    caratulas_clicables(resultados_html(ctx, resultados), "buscar", ORIGEN)
