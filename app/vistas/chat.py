"""Pestaña Chat: el mismo chat LLM con herramientas, ahora dentro de MoviePredict.

La lógica no cambia (llm.responder). Lo nuevo: cada mensaje es una acción del
Limitador; el mensaje 21 dentro del minuto se rechaza con aviso y no se guarda.
"""

from __future__ import annotations

import streamlit as st

from recomendador import llm
from recomendador.herramientas import _afinidad

from .comun import Contexto, esc


def tarjeta(peli: dict, explicacion: str | None = None) -> str:
    """HTML de una tarjeta de película (todo el texto escapado)."""
    meta = [str(peli["anio"])] if peli.get("anio") else []
    if peli.get("duracion_min"):
        meta.append(f"{peli['duracion_min']} min")
    if peli.get("imdb_rating"):
        meta.append(f"IMDb {peli['imdb_rating']:.1f}")
    chips = "".join(f'<span class="chip">{esc(g)}</span>'
                    for g in (peli.get("generos_es") or peli.get("generos") or [])[:4])
    poster = (f'<img src="{esc(peli["poster_url"])}" alt="Póster de {esc(peli["titulo"])}">'
              if peli.get("poster_url") else "")
    # La afinidad se muestra como texto («afinidad alta/media»), no como un número.
    afinidad = peli.get("afinidad") or _afinidad(peli.get("score"))
    puntaje = f" · {afinidad}" if afinidad else ""
    expl = f'<div class="expl">{esc(explicacion)}</div>' if explicacion else ""
    # Qué condiciones del pedido cumple, cuáles no y cuáles no se pudieron evaluar.
    cond = (f'<div class="cond">{esc(peli["condiciones_texto"])}</div>'
            if explicacion and peli.get("condiciones_texto") else "")
    return (f'<div class="tarjeta">{poster}<div class="rank">#{peli.get("rank", "")}</div>'
            f'<div class="titulo">{esc(peli["titulo"])}</div>'
            f'<div class="meta">{esc(" · ".join(meta))}</div><div>{chips}</div>{cond}{expl}'
            f'<div class="fuente">{esc(peli.get("fuente", ""))}{esc(puntaje)}</div></div>')


def rejilla(pelis: list[dict], con_explicacion: bool = False) -> None:
    st.markdown('<div class="rejilla">' + "".join(
        tarjeta(p, p.get("explicacion") if con_explicacion else None) for p in pelis)
        + "</div>", unsafe_allow_html=True)


def mostrar_respuesta(r: dict) -> None:
    """Pinta un mensaje del asistente guardado en el historial."""
    if r.get("aviso"):
        st.warning(r["aviso"], icon="⚠️")
    st.markdown(r["content"])
    if r.get("recomendaciones"):
        rejilla(r["recomendaciones"], con_explicacion=True)


def mostrar(ctx: Contexto) -> None:
    user_id = ctx.user_id
    st.caption("Pide algo como «algo corto y liviano para hoy» o «drama policial con un "
               "twist final». Solo se recomiendan películas que entregan las herramientas, "
               "explicadas con tus datos.")

    clave_chat = f"chat_{user_id}"
    historial = st.session_state.setdefault(clave_chat, [])
    caja = st.container()
    pregunta = st.chat_input("¿Qué quieres ver hoy?", key="chat_entrada")
    with caja:
        for m in historial:
            with st.chat_message(m["role"]):
                if m["role"] == "assistant":
                    mostrar_respuesta(m)
                else:
                    st.markdown(m["content"])
        if not pregunta:
            return
        if not ctx.limitador.permitir("chat"):
            # Acción 21 dentro del minuto: aviso y NO se registra en el historial.
            st.warning(ctx.limitador.aviso(), icon="⏳")
            return
        historial.append({"role": "user", "content": pregunta})
        with st.chat_message("user"):
            st.markdown(pregunta)
        with st.chat_message("assistant"):
            with st.spinner("Consultando tus datos…"):
                r = llm.responder(pregunta, user_id, ctx.herr, historial=historial[:-1])
            for rec in r.recomendaciones:
                if not rec.get("poster_url"):
                    rec["poster_url"] = ctx.tmdb.poster_src(rec)
            guardado = {"role": "assistant", "content": r.texto, "aviso": r.aviso,
                        "recomendaciones": r.recomendaciones, "proveedor": r.proveedor}
            mostrar_respuesta(guardado)
            st.caption(f"Proveedor: {r.proveedor} · herramientas: "
                       f"{', '.join(dict.fromkeys(r.herramientas_usadas)) or 'ninguna'}")
        historial.append(guardado)
