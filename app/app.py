"""MoviePredict: app Streamlit del recomendador (login, onboarding, Inicio, Buscar, Explorar, Chat).

Uso (desde la raíz del proyecto, con el .venv activo):
    .venv/Scripts/python -m streamlit run app/app.py

Flujo: sin sesión solo se ve el login/registro. Tras entrar, si la cuenta aún no
tiene calificaciones se muestra el onboarding (calificar ~10 populares o importar un
CSV de Letterboxd). Luego, las secciones Inicio | Buscar | Explorar | Chat (solo se
ejecuta la elegida). Al arrancar, un hilo precarga los datos pesados.

La app solo LEE lo precalculado (artefactos/<perfil>/app/*.parquet, de
scripts/precalcular_app.py) y el estado en vivo (estado.db, que escribe
scripts/streaming.py). Nunca importa pyspark. Configuración en recomendador/config.py
(MODO, DATOS, ARTEFACTOS, ESTADO_DB, LLM_PROVIDER, LLM_MODEL, OLLAMA_HOST, LLM_THINK).

Modo público (MODO_PUBLICO=1, para publicarla por Cloudflare Tunnel): login
obligatorio, límite de 20 acciones por minuto por sesión, LLM de a una solicitud y
ningún control de administración (ni configuración del LLM, ni recarga, ni rutas).
La moderación de reseñas nunca está en la app: solo scripts/moderar_resenas.py.
"""

from __future__ import annotations

import logging
import sys
import threading
import time
from pathlib import Path

import streamlit as st

CARPETA_APP = Path(__file__).resolve().parent
RAIZ = CARPETA_APP.parent
for ruta in (str(RAIZ), str(CARPETA_APP)):
    if ruta not in sys.path:
        sys.path.insert(0, ruta)

import estilo  # noqa: E402
from recomendador import config, cuentas, demo, estado, llm, proveedores  # noqa: E402
from recomendador import genome as genome_npy  # noqa: E402
from recomendador.catalogo_app import Catalogo  # noqa: E402
from recomendador.enriquecimiento import ATRIBUCION_TMDB  # noqa: E402
from recomendador.herramientas import Herramientas  # noqa: E402
from recomendador.tmdb import TMDb  # noqa: E402
from vistas import buscar, chat, explorar, inicio, login, onboarding  # noqa: E402
from vistas.comun import CLAVE_LIMITADOR, CLAVE_USUARIO, Contexto, esc  # noqa: E402

st.set_page_config(page_title=f"{estilo.NOMBRE_APP} · recomendaciones de películas",
                   page_icon="🎬", layout="wide")


# --------------------------------------------------------------------------- #
# Recursos compartidos (uno por proceso y configuración)
# --------------------------------------------------------------------------- #

@st.cache_resource(show_spinner=False)
def obtener_herramientas(datos: str, artefactos: str, estado_db: str,
                         corte: str | None = None) -> Herramientas:
    """Herramientas del chat con el corte temporal siempre aplicado."""
    return Herramientas(datos, artefactos, estado_db, corte=corte)


@st.cache_resource(show_spinner=False)
def obtener_catalogo(artefactos: str, datos: str, estado_db: str, corte: str | None,
                     cuentas_db: str) -> Catalogo:
    """Capa de lectura (parquets + estado.db) compartida por todas las sesiones."""
    return Catalogo(artefactos, datos, estado_db, corte, cuentas_db)


@st.cache_resource(show_spinner=False)
def obtener_tmdb() -> TMDb:
    return TMDb()


def _calentar(cat: Catalogo, herr: Herramientas, tmdb: TMDb) -> None:
    """Precarga en segundo plano todo lo pesado para que el primer visitante no espere."""
    pasos = [("catálogo", cat._peliculas), ("personas", cat._personas),
             ("vecinos", cat._vecinos), ("populares por tipo", cat._populares_tipo),
             ("factores", cat._factores), ("ratings y votos", herr._votos),
             ("catálogo del chat", herr.catalogo), ("genome", lambda: genome_npy.cargar(herr.datos)),
             ("TMDb", tmdb.precalentado)]
    for nombre, paso in pasos:
        t0 = time.perf_counter()
        try:
            paso()
        except Exception as exc:  # un artefacto faltante no debe tumbar la app
            logging.getLogger(__name__).warning("Calentamiento: %s falló (%s)", nombre, exc)
        logging.getLogger(__name__).info("Calentamiento: %s en %.1f s", nombre,
                                         time.perf_counter() - t0)


@st.cache_resource(show_spinner=False)
def iniciar_calentamiento(artefactos: str, datos: str, estado_db: str, corte: str | None,
                          cuentas_db: str) -> threading.Thread:
    """Un solo hilo de calentamiento por proceso y configuración (al arrancar la app).
    Usa los mismos recursos compartidos que las sesiones (st.cache_resource)."""
    cat = obtener_catalogo(artefactos, datos, estado_db, corte, cuentas_db)
    herr = obtener_herramientas(datos, artefactos, estado_db, corte)
    hilo = threading.Thread(target=_calentar, name="calentamiento", daemon=True,
                            args=(cat, herr, obtener_tmdb()))
    hilo.start()
    return hilo


@st.cache_data(ttl=20, show_spinner=False)
def estado_ollama(host: str, modelo: str) -> dict:
    """Chequeo de Ollama al cargar la página (cacheado 20 s por host y modelo)."""
    return llm.estado_ollama()


def hace(segundos: float) -> str:
    segundos = max(0, int(segundos))
    if segundos < 120:
        return f"{segundos} s"
    if segundos < 7200:
        return f"{segundos // 60} min"
    return f"{segundos // 3600} h"


def armar_contexto() -> Contexto:
    datos, corte = Path(config.DATOS), None
    if config.MODO == "demo":
        # demo.json guarda rutas relativas a la raíz; leer_demo_json las resuelve.
        info = demo.leer_demo_json(config.ARTEFACTOS_DEMO) or {}
        if info.get("datos") and Path(info["datos"]).exists():
            datos = Path(info["datos"])
        corte = info.get("corte")
    artefactos = config.artefactos_activos()
    cuentas_db = cuentas.ruta_defecto()
    if CLAVE_LIMITADOR not in st.session_state:
        st.session_state[CLAVE_LIMITADOR] = cuentas.Limitador()
    cat = obtener_catalogo(str(artefactos), str(datos), str(config.ESTADO_DB), corte,
                           str(cuentas_db))
    herr = obtener_herramientas(str(datos), str(artefactos), str(config.ESTADO_DB), corte)
    iniciar_calentamiento(str(artefactos), str(datos), str(config.ESTADO_DB), corte,
                          str(cuentas_db))
    return Contexto(
        cat=cat, herr=herr,
        tmdb=obtener_tmdb(), limitador=st.session_state[CLAVE_LIMITADOR], datos=datos,
        estado_db=Path(config.ESTADO_DB), cuentas_db=cuentas_db,
        usuario=st.session_state.get(CLAVE_USUARIO))


# --------------------------------------------------------------------------- #
# Barra lateral (solo con sesión)
# --------------------------------------------------------------------------- #

def barra_lateral(ctx: Contexto) -> None:
    with st.sidebar:
        st.markdown(estilo.cabecera_html(), unsafe_allow_html=True)
        st.markdown(f"**{esc(ctx.usuario['nombre'])}** · usuario {ctx.user_id}")
        if st.button("Cerrar sesión", key="salir", use_container_width=True):
            for clave in [k for k in st.session_state if k != CLAVE_LIMITADOR]:
                del st.session_state[clave]
            st.rerun()
        st.divider()
        st.markdown(f"**Modo:** {'demo (reentreno en vivo)' if config.MODO == 'demo' else 'completo'}")
        st.caption(f"Historial de MovieLens hasta el {ctx.herr.corte} (exclusivo) + "
                   "eventos en vivo")
        t = ctx.herr.ultima_actualizacion(ctx.user_id)
        if t is None:
            st.markdown('<div class="cr-vivo inactivo">Sin actualizaciones en vivo para este '
                        'usuario</div>', unsafe_allow_html=True)
        else:
            st.markdown(f'<div class="cr-vivo">● Actualizado hace {hace(time.time() - t)}'
                        '</div>', unsafe_allow_html=True)
        st.caption(f"Te quedan {ctx.limitador.restantes()} de {ctx.limitador.maximo} "
                   "acciones en este minuto.")
        if config.MODO_PUBLICO:
            # Público: nada de configuración, estado del servidor ni recarga manual.
            return
        cfg = llm.descripcion_config()
        st.markdown(f"**LLM_PROVIDER:** `{cfg['LLM_PROVIDER']}`  \n**LLM_MODEL:** "
                    f"`{cfg['LLM_MODEL']}`  \n**think:** `{str(cfg['think']).lower()}`")
        # Estado de Ollama visible desde el inicio (no solo al enviar un mensaje).
        if cfg["LLM_PROVIDER"].lower() == "reglas":
            st.caption("Chat en modo reglas (configurado con LLM_PROVIDER=reglas).")
        elif cfg["LLM_PROVIDER"].lower() == "api":
            # Qué proveedor respondió el último mensaje y cupo restante (sin claves).
            e = proveedores.estado()
            st.caption(f"Modelos: chat `{cfg['LLM_MODELO_CHAT']}` · tarjetas "
                       f"`{cfg['LLM_MODELO_TARJETAS']}`  \nCadena: {' → '.join(e['cadena'])}")
            ultimo = st.session_state.get(chat.CLAVE_ULTIMO_PROVEEDOR, "—")
            st.markdown(f"**Último proveedor:** {esc(ultimo)}  \n**Cupo:** "
                        f"{e['cupo_minuto']}/{e['max_minuto']} por minuto · "
                        f"{e['cupo_dia']}/{e['max_dia']} hoy")
        else:
            estado = estado_ollama(cfg["OLLAMA_HOST"], cfg["LLM_MODEL"])
            if estado["ok"]:
                st.markdown(f'<div class="cr-vivo">● {esc(estado["mensaje"])}</div>',
                            unsafe_allow_html=True)
            else:
                st.warning(estado["mensaje"], icon="⚠️")
        if st.button("🔄 Actualizar", use_container_width=True, key="actualizar"):
            st.rerun()


# --------------------------------------------------------------------------- #
# Página
# --------------------------------------------------------------------------- #

SECCIONES = {"Inicio": inicio, "Buscar": buscar, "Explorar": explorar, "Chat": chat}
CLAVE_SECCION = "seccion"

estado.marcar_recarga()       # la firma de eventos de cada usuario se lee una vez por recarga
estilo.inyectar(st)
ctx = armar_contexto()

if ctx.usuario is None:
    st.markdown(estilo.cabecera_html(), unsafe_allow_html=True)
    login.mostrar(ctx)
else:
    barra_lateral(ctx)
    st.markdown(estilo.cabecera_html(), unsafe_allow_html=True)
    sin_notas = not ctx.cat.ratings_usuario(ctx.user_id)
    if sin_notas and not st.session_state.get(onboarding.CLAVE_OMITIDO):
        onboarding.mostrar(ctx)
    else:
        # Navegación que ejecuta SOLO la sección elegida (st.tabs ejecutaba las cuatro en
        # cada recarga). La sección, el chat y la cola de Explorar viven en session_state.
        seccion = st.radio("Sección", list(SECCIONES), key=CLAVE_SECCION, horizontal=True,
                           label_visibility="collapsed")
        SECCIONES[seccion].mostrar(ctx)

st.markdown(estilo.pie_html(ATRIBUCION_TMDB), unsafe_allow_html=True)
