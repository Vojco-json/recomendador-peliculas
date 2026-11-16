"""Estilo visual de CineRuta: paleta oscura propia, tipografía y CSS de la app.

La paleta se inspira en el ambiente oscuro de las apps de cine, pero NO usa los colores
de marca de Letterboxd (eso lo revisa tests/test_app_letterboxd.py con una prueba de
texto sobre este archivo). El CSS se inyecta una sola vez por ejecución con ``inyectar``.
"""

from __future__ import annotations

NOMBRE_APP = "CineRuta"
LEMA = "Tu ruta por el cine, guiada por tus gustos"

# --------------------------------------------------------------------------- #
# Paleta CineRuta
# --------------------------------------------------------------------------- #
PALETA = {
    "fondo": "#0e1216",          # fondo general
    "superficie": "#1f2730",     # tarjetas, barra lateral, campos
    "borde": "#34404c",          # bordes y separadores
    "texto": "#aab4be",          # texto corriente
    "titulo": "#f3f5f7",         # títulos y texto destacado
    "ambar": "#f0b23a",          # acento principal: botones, estrellas, barras
    "turquesa": "#3cb4a8",       # 'probablemente te guste', enlaces, estado en vivo
    "coral": "#e5675c",          # 'puede que no', avisos
}

FUENTE_TEXTO = 'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif'
FUENTE_TITULOS = 'Georgia, "Times New Roman", serif'


def _css() -> str:
    p = PALETA
    return f"""
<style>
:root {{
  --cr-fondo:{p['fondo']}; --cr-superficie:{p['superficie']}; --cr-borde:{p['borde']};
  --cr-texto:{p['texto']}; --cr-titulo:{p['titulo']}; --cr-ambar:{p['ambar']};
  --cr-turquesa:{p['turquesa']}; --cr-coral:{p['coral']};
}}
/* ---- base ---- */
.stApp, [data-testid="stAppViewContainer"] {{ background:var(--cr-fondo); color:var(--cr-texto);
  font-family:{FUENTE_TEXTO}; }}
[data-testid="stHeader"] {{ background:transparent; }}
[data-testid="stSidebar"] {{ background:var(--cr-superficie);
  border-right:1px solid var(--cr-borde); }}
.stApp h1, .stApp h2, .stApp h3 {{ font-family:{FUENTE_TITULOS}; font-weight:400;
  color:var(--cr-titulo); letter-spacing:.2px; }}
.stApp p, .stApp li, .stApp label {{ color:var(--cr-texto); }}
.stApp a {{ color:var(--cr-turquesa); }}
.block-container {{ padding-top:1.6rem; max-width:1200px; }}

/* ---- botones y campos ---- */
.stButton > button, .stFormSubmitButton > button, .stDownloadButton > button {{
  background:var(--cr-superficie); color:var(--cr-titulo); border:1px solid var(--cr-borde);
  border-radius:6px; }}
.stButton > button:hover, .stFormSubmitButton > button:hover {{ border-color:var(--cr-ambar);
  color:var(--cr-ambar); }}
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {{
  background:var(--cr-ambar); color:var(--cr-fondo); border-color:var(--cr-ambar);
  font-weight:700; }}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {{
  filter:brightness(1.08); color:var(--cr-fondo); }}
[data-baseweb="input"], [data-baseweb="select"] > div, [data-baseweb="textarea"] {{
  background:var(--cr-fondo) !important; border-color:var(--cr-borde) !important; }}
.stTabs [data-baseweb="tab-list"] {{ gap:.4rem; border-bottom:1px solid var(--cr-borde); }}
.stTabs [data-baseweb="tab"] {{ color:var(--cr-texto); padding:.4rem .9rem; }}
.stTabs [aria-selected="true"] {{ color:var(--cr-titulo); }}
.stTabs [data-baseweb="tab-highlight"] {{ background:var(--cr-ambar); }}

/* ---- marca ---- */
.cr-marca {{ display:flex; align-items:baseline; gap:.7rem; flex-wrap:wrap;
  margin:0 0 .8rem; }}
.cr-logo {{ font-family:{FUENTE_TITULOS}; font-size:2.1rem; color:var(--cr-titulo);
  line-height:1; }}
.cr-logo b {{ color:var(--cr-ambar); font-weight:400; }}
.cr-lema {{ color:var(--cr-texto); font-size:.9rem; }}
.cr-pie {{ margin-top:2.5rem; padding-top:1rem; border-top:1px solid var(--cr-borde);
  color:var(--cr-texto); font-size:.75rem; text-align:center; opacity:.85; }}
.cr-pie b {{ color:var(--cr-titulo); font-weight:600; }}

/* ---- login ---- */
.cr-bienvenida {{ background:var(--cr-superficie); border:1px solid var(--cr-borde);
  border-radius:10px; padding:1.1rem 1.3rem; margin-bottom:1rem; }}
.cr-bienvenida h2 {{ margin:0 0 .3rem; font-size:1.5rem; }}

/* ---- tipo de espectador ---- */
.cr-tipo {{ display:inline-block; padding:.3rem .8rem; border-radius:999px;
  background:var(--cr-ambar); color:var(--cr-fondo); font-weight:700; font-size:.9rem; }}
.cr-tipo.vacio {{ background:var(--cr-borde); color:var(--cr-titulo); }}
.cr-tipo-linea {{ margin:.2rem 0 1rem; color:var(--cr-texto); font-size:.9rem; }}
.cr-vivo {{ color:var(--cr-turquesa); font-size:.82rem; }}
.cr-vivo.inactivo {{ color:var(--cr-texto); }}

/* ---- filas de carátulas (desplazables en horizontal) ---- */
.cr-fila {{ margin:0 0 1.4rem; }}
.cr-fila h3 {{ font-size:1.15rem; margin:0 0 .5rem; padding-bottom:.3rem;
  border-bottom:1px solid var(--cr-borde); }}
.cr-carril {{ display:flex; gap:12px; overflow-x:auto; padding:2px 2px 10px;
  scroll-snap-type:x proximity; scrollbar-color:var(--cr-borde) transparent; }}
.cr-caratula {{ flex:0 0 132px; scroll-snap-align:start; }}
.cr-caratula img {{ width:132px; aspect-ratio:2/3; object-fit:cover; border-radius:4px;
  border:1px solid var(--cr-borde); display:block; background:var(--cr-superficie);
  transition:border-color .15s, transform .15s; }}
.cr-caratula:hover img {{ border-color:var(--cr-ambar); transform:translateY(-2px); }}
.cr-caratula .t {{ color:var(--cr-titulo); font-size:.8rem; line-height:1.2; margin-top:.35rem;
  display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }}
.cr-caratula .a {{ color:var(--cr-texto); font-size:.72rem; }}
@media (max-width:640px) {{
  .cr-caratula {{ flex-basis:108px; }} .cr-caratula img {{ width:108px; }}
  .cr-logo {{ font-size:1.7rem; }}
}}

/* ---- onboarding ---- */
.cr-paso {{ color:var(--cr-ambar); font-size:.78rem; text-transform:uppercase;
  letter-spacing:1.2px; font-weight:700; }}
.cr-onb img {{ width:100%; aspect-ratio:2/3; object-fit:cover; border-radius:4px;
  border:1px solid var(--cr-borde); }}
.cr-onb .t {{ color:var(--cr-titulo); font-size:.85rem; margin:.3rem 0 .1rem;
  min-height:2.1em; line-height:1.2; }}

/* ---- chat: tarjetas de recomendaciones ---- */
.rejilla {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(210px,1fr)); gap:14px; }}
.tarjeta {{ background:var(--cr-superficie); border:1px solid var(--cr-borde); border-radius:8px;
  padding:12px 14px; display:flex; flex-direction:column; gap:6px; min-height:150px; }}
.tarjeta img {{ width:100%; border-radius:4px; aspect-ratio:2/3; object-fit:cover; }}
.tarjeta .rank {{ color:var(--cr-ambar); font-weight:700; font-size:.8rem; }}
.tarjeta .titulo {{ color:var(--cr-titulo); font-weight:700; font-size:1rem; line-height:1.25; }}
.tarjeta .meta {{ color:var(--cr-texto); font-size:.8rem; }}
.chip {{ display:inline-block; background:var(--cr-fondo); color:var(--cr-texto);
  border:1px solid var(--cr-borde); border-radius:999px; padding:1px 8px; margin:2px 3px 0 0;
  font-size:.72rem; }}
.tarjeta .expl {{ color:var(--cr-titulo); font-size:.85rem; line-height:1.35; opacity:.9; }}
.tarjeta .cond {{ color:var(--cr-turquesa); font-size:.78rem; line-height:1.3;
  border-left:2px solid var(--cr-turquesa); padding-left:6px; }}
.tarjeta .fuente {{ color:var(--cr-texto); font-size:.7rem; margin-top:auto; opacity:.8; }}
@media (max-width:640px) {{ .rejilla {{ grid-template-columns:1fr; }} }}
</style>
"""


CSS = _css()


def inyectar(st) -> None:
    """Inserta el CSS de CineRuta (una vez por ejecución del script)."""
    st.markdown(CSS, unsafe_allow_html=True)


def cabecera_html() -> str:
    """Logotipo y lema (la cabecera de todas las pantallas)."""
    return (f'<div class="cr-marca"><span class="cr-logo">Cine<b>Ruta</b></span>'
            f'<span class="cr-lema">{LEMA}</span></div>')


def pie_html(atribucion: str = "") -> str:
    """Pie con el nombre de la app, las fuentes de datos y la atribución a TMDb."""
    extra = f"<br>{atribucion}" if atribucion else ""
    return (f'<div class="cr-pie"><b>{NOMBRE_APP}</b> · recomendaciones con MovieLens 25M, '
            f'Spark y Kafka{extra}</div>')
