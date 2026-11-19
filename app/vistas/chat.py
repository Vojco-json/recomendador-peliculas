"""Pestaña Chat: conversación natural con contexto, apoyada en los datos del sistema.

La lógica vive en recomendador/conversacion.py. Aquí solo se pinta:
- el historial y el estado viven SOLO en st.session_state (RAM): se pierden al
  recargar, al cerrar sesión o con «Nueva conversación»;
- chips con las condiciones activas (cada una con × para quitarla y recalcular);
- cada mensaje es una acción del Limitador (la 21 del minuto se rechaza sin guardarse).
"""

from __future__ import annotations

import streamlit as st

from recomendador import config, conversacion, llm
from recomendador.herramientas import _afinidad

from .comun import Contexto, caratulas_clicables, esc

ORIGEN = "Chat"
CLAVE_ULTIMO_PROVEEDOR = "chat_ultimo_proveedor"
AVISO_EXTERNO = ("Los mensajes del chat se procesan con un servicio externo (Groq o "
                 "Gemini). No escribas datos personales.")


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
    cond = (f'<div class="cond">{esc(peli["condiciones_texto"])}</div>'
            if explicacion and peli.get("condiciones_texto") else "")
    # data-movie: la tarjeta es clicable (abre la ficha) dentro del componente de carátulas.
    mid = f' data-movie="{int(peli["movieId"])}"' if peli.get("movieId") is not None else ""
    return (f'<div class="tarjeta"{mid}>{poster}<div class="rank">#{peli.get("rank", "")}</div>'
            f'<div class="titulo">{esc(peli["titulo"])}</div>'
            f'<div class="meta">{esc(" · ".join(meta))}</div><div>{chips}</div>{cond}{expl}'
            f'<div class="fuente">{esc(peli.get("fuente", ""))}{esc(puntaje)}</div></div>')


def rejilla(pelis: list[dict], con_explicacion: bool = False, clave: str = "chat") -> None:
    """Tarjetas recomendadas; un clic en cualquiera abre su ficha (sin recargar)."""
    html_rejilla = '<div class="rejilla">' + "".join(
        tarjeta(p, p.get("explicacion") if con_explicacion else None) for p in pelis) + "</div>"
    caratulas_clicables(html_rejilla, clave, ORIGEN)


def mostrar_respuesta(r: dict, clave: str = "chat") -> None:
    """Pinta un mensaje del asistente guardado en el historial."""
    if r.get("aviso"):
        st.warning(r["aviso"], icon="⚠️")
    st.markdown(r["content"])
    if r.get("recomendaciones"):
        rejilla(r["recomendaciones"], con_explicacion=True, clave=clave)


def _claves(user_id: int) -> tuple[str, str]:
    return f"chat_{user_id}", f"chat_estado_{user_id}"


def _guardar(ctx: Contexto, historial: list, t: conversacion.Turno) -> dict:
    for rec in t.recomendaciones:
        if not rec.get("poster_url"):
            rec["poster_url"] = ctx.tmdb.poster_src(rec)
    guardado = {"role": "assistant", "content": t.texto, "recomendaciones": t.recomendaciones,
                "proveedor": t.proveedor}
    historial.append(guardado)
    st.session_state[CLAVE_ULTIMO_PROVEEDOR] = t.proveedor
    return guardado


def mostrar(ctx: Contexto) -> None:
    user_id = ctx.user_id
    clave_chat, clave_estado = _claves(user_id)
    historial = st.session_state.setdefault(clave_chat, [])
    estado = st.session_state.setdefault(clave_estado, conversacion.estado_nuevo())
    st.caption("Conversa como con una persona: «drama policial con giro final», luego "
               "«pero que sea corta», «otra», «¿quién dirige la segunda?» o «ya la vi».")
    if str(config.LLM_PROVIDER).lower() == "api":
        st.caption("🔒 " + AVISO_EXTERNO)

    # Los chips van arriba de la conversación, pero se dibujan AL FINAL de la recarga
    # (en este contenedor reservado), después de procesar el mensaje nuevo: así reflejan
    # de inmediato las condiciones que ese mensaje acaba de cambiar.
    zona_chips = st.container()
    _conversar(ctx, historial, estado)
    with zona_chips:
        _chips(ctx, historial, estado)


def _chips(ctx: Contexto, historial: list, estado: dict) -> None:
    """Chips de condiciones activas (× quita y recalcula), «Nueva conversación» y «Entendí»."""
    clave_chat, clave_estado = _claves(ctx.user_id)
    chips = conversacion.etiquetas(estado)
    columnas = st.columns(len(chips) + 1) if chips else [st.container()]
    for col, (clave, visible) in zip(columnas, chips):
        if col.button(f"{visible}  ×", key=f"chip_{clave}", help="Quitar esta condición"):
            if ctx.limitador.permitir("chat"):
                conversacion.quitar_condicion(estado, clave)
                _guardar(ctx, historial, conversacion.recalcular(estado, ctx.user_id, ctx.herr))
                # La conversación ya se dibujó: se repinta con la respuesta recalculada.
                st.rerun()
            else:
                st.warning(ctx.limitador.aviso(), icon="⏳")
    if columnas[-1].button("Nueva conversación", key="chat_nueva"):
        st.session_state[clave_chat] = []
        st.session_state[clave_estado] = conversacion.estado_nuevo()
        st.rerun()                              # repinta sin chips ni historial
    st.caption(conversacion.entendi_estado(estado))


def _conversar(ctx: Contexto, historial: list, estado: dict) -> None:
    """Historial, entrada del chat y respuesta al mensaje nuevo (si lo hay)."""
    user_id = ctx.user_id
    caja = st.container()
    pregunta = st.chat_input("¿Qué quieres ver hoy?", key="chat_entrada")
    with caja:
        for i, m in enumerate(historial):
            with st.chat_message(m["role"]):
                if m["role"] == "assistant":
                    mostrar_respuesta(m, clave=f"chat_{i}")
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
                if str(config.LLM_PROVIDER).lower() == "ollama":
                    # Opción configurable: Ollama local, un turno por mensaje con su aviso.
                    r = llm.responder(pregunta, user_id, ctx.herr, historial=historial[:-1])
                    t = conversacion.Turno(texto=r.texto, intencion="nuevo_pedido",
                                           recomendaciones=r.recomendaciones,
                                           proveedor=r.proveedor)
                else:
                    r = None
                    t = conversacion.turno(pregunta, estado, user_id, ctx.herr, ctx.cat)
            guardado = _guardar(ctx, historial, t)
            guardado["aviso"] = r.aviso if r is not None else None
            # Misma clave que tendrá en el historial: el clic se conserva tras recargar.
            mostrar_respuesta(guardado, clave=f"chat_{len(historial) - 1}")
            st.caption(f"Proveedor: {t.proveedor}")
