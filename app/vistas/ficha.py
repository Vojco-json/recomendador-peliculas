"""Ficha de película estilo diario de cine: datos, histograma, tags, reseñas y acciones.

Se usa desde Buscar y desde Explorar; ``prefijo`` separa las claves de los widgets
para que la misma película pueda aparecer en dos pestañas a la vez. La app no tiene
ningún control para ocultar reseñas: la moderación se hace fuera (scripts/moderar.py).
"""

from __future__ import annotations

import streamlit as st

from recomendador.enriquecimiento import ATRIBUCION_TMDB
from recomendador.moderacion import MAX_CARACTERES

from . import tarjetas
from .comun import Contexto, esc, estrellas, publicar_calificacion, publicar_resena

NOTAS = [x / 2 for x in range(1, 11)]        # 0.5 … 5.0
CLAVE_MEMORIA_LLM = "redacciones_llm"        # st.session_state: redacciones ya pedidas


def _mensajes(prefijo: str, movieId: int) -> str:
    return f"msg_{prefijo}_{movieId}"


def mostrar_mensajes(prefijo: str, movieId: int) -> None:
    """Muestra (una vez) lo que dejó la última acción antes del st.rerun()."""
    for tipo, texto in st.session_state.pop(_mensajes(prefijo, movieId), []):
        getattr(st, tipo)(texto)


def _anotar(prefijo: str, movieId: int, tipo: str, texto: str) -> None:
    st.session_state.setdefault(_mensajes(prefijo, movieId), []).append((tipo, texto))


def explicacion(ctx: Contexto, movieId: int) -> tuple[dict | None, str | None]:
    """explicar() del catálogo + redacción (LLM si responde; si no, la plantilla)."""
    e = ctx.cat.explicar(ctx.user_id, movieId)
    if e is None:
        return None, None
    memoria = st.session_state.setdefault(CLAVE_MEMORIA_LLM, {})
    texto, _origen = tarjetas.redactar(e, memoria)
    return e, texto


def cabecera(ctx: Contexto, f: dict, con_explicacion: bool = True) -> None:
    """Póster + histograma a la izquierda; datos, sinopsis, tags y explicación a la derecha."""
    izq, der = st.columns([1, 2.4], gap="large")
    with izq:
        st.markdown(f'<img class="cr-poster" src="{esc(ctx.tmdb.poster_src(f))}" '
                    f'alt="Carátula de {esc(f["titulo"])}">', unsafe_allow_html=True)
        st.markdown(tarjetas.histograma_html(f["histograma"], f["promedio"], f["votos"]),
                    unsafe_allow_html=True)
    with der:
        st.markdown(f'<h2 class="cr-ficha-titulo">{esc(f["titulo"])}</h2>'
                    f'{tarjetas.datos_html(f)}', unsafe_allow_html=True)
        sinopsis = ctx.tmdb.sinopsis(f.get("tmdbId")) if f.get("tmdbId") else None
        if sinopsis:
            st.markdown(f'<p class="cr-sinopsis">{esc(sinopsis)}</p>', unsafe_allow_html=True)
        elif not ctx.tmdb.disponible:
            st.caption("Sinopsis y tráiler disponibles al configurar TMDB_API_KEY.")
        if f.get("tags_es"):
            st.markdown(f'<div class="cr-dato"><span>Tags principales</span></div>'
                        f'{tarjetas.chips(f["tags_es"][:10])}', unsafe_allow_html=True)
        if con_explicacion and ctx.user_id is not None:
            e, texto = explicacion(ctx, f["movieId"])
            if e:
                st.markdown(tarjetas.explicacion_html(e, texto), unsafe_allow_html=True)


def trailer(ctx: Contexto, f: dict) -> None:
    """Tráiler de YouTube (vía TMDb) o un aviso amable si no hay."""
    url = ctx.tmdb.trailer(f.get("tmdbId")) if f.get("tmdbId") else None
    if url:
        st.markdown('<h3 class="cr-sub">Tráiler</h3>', unsafe_allow_html=True)
        st.video(url)
    elif ctx.tmdb.disponible:
        st.caption("TMDb no tiene un tráiler para esta película.")


def calificar(ctx: Contexto, movieId: int, prefijo: str) -> None:
    """Selector de estrellas + botón. Publica en Kafka (o respaldo con aviso)."""
    actual = ctx.cat.ratings_usuario(ctx.user_id).get(int(movieId))
    opciones = [None] + NOTAS
    nota = st.selectbox("Tu calificación", opciones, key=f"{prefijo}_nota_{movieId}",
                        index=opciones.index(actual) if actual in opciones else 0,
                        format_func=lambda x: "Elige tus estrellas" if x is None
                        else f"{estrellas(x)}  ({x:g})")
    if st.button("Guardar calificación", key=f"{prefijo}_calificar_{movieId}",
                 type="primary", disabled=nota is None, use_container_width=True):
        if not ctx.limitador.permitir("calificar"):
            _anotar(prefijo, movieId, "warning", ctx.limitador.aviso())
        else:
            r = publicar_calificacion(ctx.user_id, movieId, nota, ctx.estado_db)
            _anotar(prefijo, movieId, "success",
                    f"Guardamos tu calificación: {estrellas(nota)}.")
            if r.aviso:
                _anotar(prefijo, movieId, "warning", r.aviso)
            # El tipo de espectador se recalcula con TODAS sus notas (incluida esta).
            t = ctx.cat.tipo(ctx.user_id)
            if t:
                _anotar(prefijo, movieId, "info",
                        f"Tu tipo de espectador ahora: {t['tipo']} "
                        f"(según {int(t.get('n_ratings', 0))} calificaciones).")
        st.rerun()
    if actual is not None:
        st.caption(f"Ya la calificaste con {estrellas(actual)}. Puedes cambiarla.")


def resenar(ctx: Contexto, movieId: int, prefijo: str) -> None:
    """Caja de reseña (contador de 500 caracteres) + botón. El filtro lo aplica eventos."""
    vuelta = st.session_state.get(f"{prefijo}_vuelta_{movieId}", 0)   # para vaciar la caja
    texto = st.text_area(f"Tu reseña (máximo {MAX_CARACTERES} caracteres, pública)",
                         key=f"{prefijo}_resena_{movieId}_{vuelta}", max_chars=MAX_CARACTERES,
                         placeholder="¿Qué te pareció? Evita contar el final.", height=110)
    if st.button("Publicar reseña", key=f"{prefijo}_publicar_{movieId}",
                 use_container_width=True):
        if not (texto or "").strip():
            _anotar(prefijo, movieId, "warning", "Escribe algo antes de publicar.")
        elif not ctx.limitador.permitir("resenar"):
            _anotar(prefijo, movieId, "warning", ctx.limitador.aviso())
        else:
            r = publicar_resena(ctx.user_id, movieId, texto, ctx.estado_db)
            if not r.ok:                       # no pasó el filtro: no se guarda nada
                _anotar(prefijo, movieId, "error", r.aviso)
            else:
                _anotar(prefijo, movieId, "success", "Publicamos tu reseña.")
                if r.aviso:
                    _anotar(prefijo, movieId, "warning", r.aviso)
                st.session_state[f"{prefijo}_vuelta_{movieId}"] = vuelta + 1
        st.rerun()


def resenas(ctx: Contexto, movieId: int) -> None:
    lista = ctx.cat.resenas(movieId)
    st.markdown(f'<h3 class="cr-sub">Reseñas ({len(lista)})</h3>'
                f'{tarjetas.resenas_html(lista)}', unsafe_allow_html=True)


def mostrar(ctx: Contexto, movieId: int, prefijo: str = "ficha",
            con_explicacion: bool = True) -> None:
    """Ficha completa de una película."""
    f = ctx.cat.ficha(movieId)
    if f is None:
        st.info("No encontramos esa película en el catálogo.")
        return
    cabecera(ctx, f, con_explicacion)
    trailer(ctx, f)
    st.markdown('<h3 class="cr-sub">Califica y reseña</h3>', unsafe_allow_html=True)
    mostrar_mensajes(prefijo, movieId)
    c1, c2 = st.columns([1, 1.6], gap="large")
    with c1:
        calificar(ctx, movieId, prefijo)
    with c2:
        resenar(ctx, movieId, prefijo)
    resenas(ctx, movieId)
    st.markdown(f'<div class="cr-atrib">Póster, sinopsis y tráiler: TMDb. '
                f'{esc(ATRIBUCION_TMDB)}</div>', unsafe_allow_html=True)
