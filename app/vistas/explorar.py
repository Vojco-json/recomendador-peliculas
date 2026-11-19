"""Explorar: una película a la vez (carátula → info → tráiler → Siguiente).

La cola mezcla la tarjeta 'Para ti' (una probable y una improbable) con las filas
de Inicio, intercalando para que aparezcan ambos veredictos. Cada película se
explica con ``explicar()`` (el LLM solo redacta; la plantilla es el respaldo).
"""

from __future__ import annotations

import streamlit as st

from . import ficha
from .comun import Contexto

MAX_COLA = 40


def _clave(ctx: Contexto, nombre: str) -> str:
    return f"explorar_{nombre}_{ctx.user_id}"


def armar_cola(ctx: Contexto) -> list[int]:
    """IDs a recorrer, sin repetir ni incluir las ya calificadas."""
    vistas = set(ctx.cat.ratings_usuario(ctx.user_id))
    t = ctx.cat.tarjeta_para_ti(ctx.user_id)
    primeras = [t[k]["movieId"] for k in ("probable", "improbable") if t.get(k)]
    filas = [[p["movieId"] for p in f["peliculas"] if p] for f in
             ctx.cat.filas_inicio(ctx.user_id)]
    intercaladas = [m for grupo in zip(*filas) for m in grupo] if filas else []
    resto = [m for f in filas for m in f]
    cola = []
    for m in primeras + intercaladas + resto:
        if m not in vistas and m not in cola:
            cola.append(int(m))
    return cola[:MAX_COLA]


def mostrar(ctx: Contexto) -> None:
    st.caption("Descubre de a una: carátula, datos, por qué podría (o no) gustarte, "
               "tráiler y luego «Siguiente».")
    cola = st.session_state.get(_clave(ctx, "cola"))
    if not cola:
        cola = armar_cola(ctx)
        st.session_state[_clave(ctx, "cola")] = cola
        st.session_state[_clave(ctx, "i")] = 0
    if not cola:
        st.info("Ya calificaste todo lo que teníamos para mostrarte. ¡Prueba el buscador!")
        return
    i = st.session_state.get(_clave(ctx, "i"), 0) % len(cola)
    movieId = cola[i]
    f = ctx.cat.ficha(movieId)

    st.markdown(f'<div class="cr-paso">Película {i + 1} de {len(cola)}</div>',
                unsafe_allow_html=True)
    # 1-2. Carátula, información y explicación personalizada.
    ficha.cabecera(ctx, f, con_explicacion=True)
    # 3. Tráiler (si TMDb lo tiene).
    ficha.trailer(ctx, f)
    # Calificar y reseñar sin salir de Explorar (los avisos quedan fuera del desplegable).
    ficha.mostrar_mensajes("exp", movieId)
    with st.expander("Califícala, reséñala o lee las reseñas"):
        c1, c2 = st.columns([1, 1.6], gap="large")
        with c1:
            ficha.calificar(ctx, movieId, "exp")
        with c2:
            ficha.resenar(ctx, movieId, "exp")
        ficha.resenas(ctx, movieId)
    # 4. Siguiente.
    a, b, _ = st.columns([1, 1, 3])
    with a:
        if st.button("← Anterior", key="explorar_anterior", use_container_width=True,
                     disabled=i == 0):
            st.session_state[_clave(ctx, "i")] = i - 1
            st.rerun()
    with b:
        if st.button("Siguiente →", key="explorar_siguiente", type="primary",
                     use_container_width=True):
            st.session_state[_clave(ctx, "i")] = i + 1
            st.rerun()
