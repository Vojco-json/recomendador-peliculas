"""App Streamlit de la demo: recomendaciones por usuario y chat LLM con herramientas.

Uso (desde la raíz del proyecto, con el .venv activo):
    .venv/Scripts/python -m streamlit run app/app.py

Lee la configuración de recomendador/config.py (variables de entorno MODO, DATOS,
ARTEFACTOS, ESTADO_DB, LLM_PROVIDER, LLM_MODEL, OLLAMA_HOST, LLM_THINK).
El estado en vivo (estado.db) lo escribe scripts/streaming.py mientras la app corre.
"""

from __future__ import annotations

import html
import sys
import time
from pathlib import Path

import streamlit as st

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config, demo, llm  # noqa: E402
from recomendador.herramientas import Herramientas, _afinidad  # noqa: E402

st.set_page_config(page_title="Recomendador de películas · MovieLens 25M",
                   page_icon="🎬", layout="wide")

# Paleta del póster: fondo oscuro, rojo y ámbar.
CSS = """
<style>
.badge-tipo {display:inline-block;padding:.35rem .8rem;border-radius:999px;
  background:#E50914;color:#fff;font-weight:700;font-size:.95rem;margin:.2rem 0 .4rem}
.badge-gris {background:#555}
.desc-tipo {color:#B3B3B3;font-size:.82rem;margin-bottom:.6rem}
.barra {margin:.25rem 0}
.barra .et {display:flex;justify-content:space-between;font-size:.8rem;color:#ddd}
.barra .fondo {background:#2a2a2a;border-radius:4px;height:8px;overflow:hidden}
.barra .relleno {background:linear-gradient(90deg,#E50914,#F5C518);height:8px}
.vivo {font-size:.82rem;color:#7CFC9A}
.vivo.inactivo {color:#B3B3B3}
.rejilla {display:grid;grid-template-columns:repeat(auto-fill,minmax(210px,1fr));gap:14px}
.tarjeta {background:#1c1c1c;border:1px solid #2e2e2e;border-radius:12px;padding:12px 14px;
  display:flex;flex-direction:column;gap:6px;min-height:150px}
.tarjeta img {width:100%;border-radius:8px;aspect-ratio:2/3;object-fit:cover}
.tarjeta .rank {color:#F5C518;font-weight:700;font-size:.8rem}
.tarjeta .titulo {color:#F2F2F2;font-weight:700;font-size:1rem;line-height:1.25}
.tarjeta .meta {color:#B3B3B3;font-size:.8rem}
.chip {display:inline-block;background:#2b2b2b;color:#ddd;border-radius:999px;
  padding:1px 8px;margin:2px 3px 0 0;font-size:.72rem}
.tarjeta .expl {color:#ddd;font-size:.85rem;line-height:1.35}
.tarjeta .cond {color:#F5C518;font-size:.78rem;line-height:1.3;
  border-left:2px solid #F5C518;padding-left:6px}
.tarjeta .fuente {color:#777;font-size:.7rem;margin-top:auto}
@media (max-width: 640px){ .rejilla{grid-template-columns:1fr} }
</style>
"""


@st.cache_resource(show_spinner=False)
def obtener_herramientas(datos: str, artefactos: str, estado_db: str,
                         corte: str | None = None) -> Herramientas:
    """Herramientas con el corte temporal siempre aplicado (por defecto FECHA_CORTE)."""
    return Herramientas(datos, artefactos, estado_db, corte=corte)


@st.cache_data(ttl=20, show_spinner=False)
def estado_ollama(host: str, modelo: str) -> dict:
    """Chequeo de Ollama al cargar la página (cacheado 20 s por host y modelo)."""
    return llm.estado_ollama()


def esc(texto) -> str:
    return html.escape("" if texto is None else str(texto))


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


def hace(segundos: float) -> str:
    segundos = max(0, int(segundos))
    if segundos < 120:
        return f"{segundos} s"
    if segundos < 7200:
        return f"{segundos // 60} min"
    return f"{segundos // 3600} h"


def mostrar_respuesta(r: dict) -> None:
    """Pinta un mensaje del asistente guardado en el historial."""
    if r.get("aviso"):
        st.warning(r["aviso"], icon="⚠️")
    st.markdown(r["content"])
    if r.get("recomendaciones"):
        rejilla(r["recomendaciones"], con_explicacion=True)


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #

st.markdown(CSS, unsafe_allow_html=True)
datos_app, corte_app = config.DATOS, None
if config.MODO == "demo":
    # demo.json guarda rutas relativas a la raíz; leer_demo_json las resuelve.
    info_demo = demo.leer_demo_json(config.ARTEFACTOS_DEMO) or {}
    if info_demo.get("datos") and Path(info_demo["datos"]).exists():
        datos_app = info_demo["datos"]
    corte_app = info_demo.get("corte")
herr = obtener_herramientas(str(datos_app), str(config.artefactos_activos()),
                            str(config.ESTADO_DB), corte_app)

with st.sidebar:
    st.title("🎬 Recomendador")
    user_id = int(st.number_input("userId", min_value=1, value=1, step=1, key="userId"))
    perfil = herr.perfil_gustos(user_id)
    if perfil.get("tipo"):
        st.markdown(f'<span class="badge-tipo">{esc(perfil["tipo"])}</span>',
                    unsafe_allow_html=True)
        if perfil.get("descripcion_tipo"):
            st.markdown(f'<div class="desc-tipo">{esc(perfil["descripcion_tipo"])}</div>',
                        unsafe_allow_html=True)
    else:
        st.markdown('<span class="badge-tipo badge-gris">Sin tipo de espectador</span>',
                    unsafe_allow_html=True)

    pesos = [p for p in perfil.get("pesos_generos", []) if p["peso"] > 0][:6]
    if pesos:
        st.caption("Géneros que más pesan en tu perfil")
        maximo = max(p["peso"] for p in pesos)
        st.markdown("".join(
            f'<div class="barra"><div class="et"><span>{esc(p["genero_es"].capitalize())}'
            f'</span><span>{p["peso"]:.2f}</span></div><div class="fondo"><div class="relleno"'
            f' style="width:{100 * p["peso"] / maximo:.0f}%"></div></div></div>'
            for p in pesos), unsafe_allow_html=True)
    if perfil.get("existe"):
        st.caption(f"{perfil['n_ratings']} calificaciones · media {perfil['media']:.2f} · "
                   f"perfil: {perfil['fuente']}")

    st.divider()
    st.markdown(f"**Modo:** {'demo (reentreno en vivo)' if config.MODO == 'demo' else 'completo'}")
    st.caption(f"Historial de MovieLens hasta el {herr.corte} (exclusivo) + eventos en vivo")
    t = herr.ultima_actualizacion(user_id)
    if t is None:
        st.markdown('<div class="vivo inactivo">Sin actualizaciones en vivo para este '
                    'usuario</div>', unsafe_allow_html=True)
    else:
        st.markdown(f'<div class="vivo">● Actualizado hace {hace(time.time() - t)}</div>',
                    unsafe_allow_html=True)
    cfg = llm.descripcion_config()
    st.markdown(f"**LLM_PROVIDER:** `{cfg['LLM_PROVIDER']}`  \n**LLM_MODEL:** "
                f"`{cfg['LLM_MODEL']}`  \n**think:** `{str(cfg['think']).lower()}`")
    # Estado de Ollama visible desde el inicio (no solo al enviar un mensaje).
    if cfg["LLM_PROVIDER"].lower() == "reglas":
        st.caption("Chat en modo reglas (configurado con LLM_PROVIDER=reglas).")
    else:
        estado = estado_ollama(cfg["OLLAMA_HOST"], cfg["LLM_MODEL"])
        if estado["ok"]:
            st.markdown(f'<div class="vivo">● {esc(estado["mensaje"])}</div>',
                        unsafe_allow_html=True)
        else:
            st.warning(estado["mensaje"], icon="⚠️")
    if st.button("🔄 Actualizar", use_container_width=True):
        st.rerun()

# ---- Columna principal ------------------------------------------------------ #
st.header(f"Recomendaciones para el usuario {user_id}")
if not perfil.get("existe") and not herr.existe_usuario(user_id):
    st.info("Este userId no tiene calificaciones: se muestran las películas más populares.")
recs = herr.recomendar(user_id, n=8)
for aviso in recs.get("avisos", []):
    st.caption(aviso)
if recs["recomendaciones"]:
    for r in recs["recomendaciones"]:
        r.update({k: v for k, v in herr.info_pelicula(r["movieId"]).items()
                  if k in ("poster_url",) and v})
    rejilla(recs["recomendaciones"])

st.subheader("💬 Pregúntale al recomendador")
st.caption("Ej.: «algo corto y liviano para hoy». Solo se recomiendan películas que "
           "entregan las herramientas, explicadas con tus datos.")

clave_chat = f"chat_{user_id}"
historial = st.session_state.setdefault(clave_chat, [])
for m in historial:
    with st.chat_message(m["role"]):
        if m["role"] == "assistant":
            mostrar_respuesta(m)
        else:
            st.markdown(m["content"])

pregunta = st.chat_input("¿Qué quieres ver hoy?")
if pregunta:
    historial.append({"role": "user", "content": pregunta})
    with st.chat_message("user"):
        st.markdown(pregunta)
    with st.chat_message("assistant"):
        with st.spinner("Consultando tus datos…"):
            r = llm.responder(pregunta, user_id, herr, historial=historial[:-1])
        guardado = {"role": "assistant", "content": r.texto, "aviso": r.aviso,
                    "recomendaciones": r.recomendaciones, "proveedor": r.proveedor}
        mostrar_respuesta(guardado)
        st.caption(f"Proveedor: {r.proveedor} · herramientas: "
                   f"{', '.join(dict.fromkeys(r.herramientas_usadas)) or 'ninguna'}")
    historial.append(guardado)
