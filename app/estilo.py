"""Estilo visual de MoviePredict: paleta oscura propia, tipografía y CSS de la app.

La paleta se inspira en el ambiente oscuro de las apps de cine, pero NO usa los colores
de marca de Letterboxd (eso lo revisa tests/test_app_letterboxd.py con una prueba de
texto sobre este archivo). El CSS se inyecta una sola vez por ejecución con ``inyectar``.
"""

from __future__ import annotations

NOMBRE_APP = "MoviePredict"
LEMA = "Tu ruta por el cine, guiada por tus gustos"

# --------------------------------------------------------------------------- #
# Paleta MoviePredict
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

/* ---- buscar ---- */
.cr-sub {{ font-size:1.1rem !important; margin:1.1rem 0 .5rem !important; padding-bottom:.3rem;
  border-bottom:1px solid var(--cr-borde); }}
.cr-resultado .cr-caratula {{ flex:none; }}
.cr-resultado .cr-caratula img {{ width:100%; }}
.cr-resultado .como {{ color:var(--cr-turquesa); font-size:.72rem; margin:.15rem 0 .3rem; }}

/* ---- ficha ---- */
.cr-poster {{ width:100%; max-width:300px; aspect-ratio:2/3; object-fit:cover; border-radius:6px;
  border:1px solid var(--cr-borde); display:block; margin-bottom:1rem; }}
.cr-ficha-titulo {{ font-size:2rem !important; margin:0 0 .3rem !important; line-height:1.15; }}
.cr-ficha-meta {{ color:var(--cr-texto); font-size:.95rem; margin-bottom:.7rem; }}
.cr-dato {{ font-size:.9rem; color:var(--cr-titulo); margin:.25rem 0; }}
.cr-dato span {{ display:inline-block; min-width:7.5rem; color:var(--cr-texto);
  text-transform:uppercase; font-size:.7rem; letter-spacing:1px; }}
.cr-dato b {{ color:var(--cr-ambar); margin-right:.3rem; }}
.cr-sinopsis {{ color:var(--cr-titulo) !important; line-height:1.55; margin:.8rem 0; opacity:.92; }}
.chip.favor {{ border-color:var(--cr-turquesa); color:var(--cr-turquesa); }}
.chip.contra {{ border-color:var(--cr-coral); color:var(--cr-coral); }}
.cr-histo {{ background:var(--cr-superficie); border:1px solid var(--cr-borde); border-radius:8px;
  padding:.7rem .8rem .5rem; max-width:300px; }}
.cr-histo-cab {{ display:flex; justify-content:space-between; gap:.5rem; font-size:.72rem;
  color:var(--cr-texto); margin-bottom:.4rem; flex-wrap:wrap; }}
.cr-histo-cab b {{ color:var(--cr-ambar); font-size:.95rem; }}
.cr-barras {{ display:flex; align-items:flex-end; gap:3px; height:70px; }}
.cr-barras .b {{ flex:1; height:100%; display:flex; flex-direction:column; justify-content:flex-end;
  align-items:center; }}
.cr-barras i {{ display:block; width:100%; background:var(--cr-ambar); border-radius:2px 2px 0 0;
  opacity:.85; }}
.cr-barras span {{ font-size:.58rem; color:var(--cr-texto); margin-top:2px; }}
.cr-exp {{ background:var(--cr-superficie); border:1px solid var(--cr-borde);
  border-left:4px solid var(--cr-turquesa); border-radius:8px; padding:.8rem 1rem; margin:1rem 0; }}
.cr-exp.no {{ border-left-color:var(--cr-coral); }}
.cr-veredicto {{ font-weight:700; text-transform:uppercase; letter-spacing:1px; font-size:.75rem;
  color:var(--cr-turquesa); }}
.cr-exp.no .cr-veredicto {{ color:var(--cr-coral); }}
.cr-exp-peli {{ color:var(--cr-titulo); font-family:{FUENTE_TITULOS}; font-size:1.05rem; }}
.cr-exp p {{ color:var(--cr-titulo) !important; font-size:.9rem; margin:.35rem 0; opacity:.9; }}
.cr-exp-fila {{ margin-top:.25rem; }}
.cr-exp-fila > span {{ font-size:.68rem; text-transform:uppercase; letter-spacing:1px;
  color:var(--cr-texto); margin-right:.4rem; }}
.cr-para-ti h3 {{ font-size:1.15rem; margin:0 0 .5rem; padding-bottom:.3rem;
  border-bottom:1px solid var(--cr-borde); }}
.cr-par {{ display:grid; grid-template-columns:1fr 1fr; gap:14px; }}
.cr-par .cr-exp {{ margin:0; }}
.cr-resenas {{ display:flex; flex-direction:column; gap:10px; }}
.cr-resenas.vacio {{ color:var(--cr-texto); font-size:.9rem; }}
.cr-resena {{ border-bottom:1px solid var(--cr-borde); padding:.3rem 0 .6rem; }}
.cr-resena .cab {{ display:flex; gap:.6rem; align-items:baseline; font-size:.8rem; }}
.cr-resena .cab b {{ color:var(--cr-titulo); }}
.cr-resena p {{ margin:.25rem 0 0; white-space:pre-wrap; overflow-wrap:anywhere; }}
.cr-atrib {{ margin-top:1.2rem; font-size:.72rem; color:var(--cr-texto); opacity:.8; }}
@media (max-width:640px) {{
  .cr-par {{ grid-template-columns:1fr; }}
  .cr-ficha-titulo {{ font-size:1.5rem !important; }}
  .cr-poster, .cr-histo {{ max-width:100%; }}
}}
</style>
"""


CSS = _css()


def inyectar(st) -> None:
    """Inserta el CSS de MoviePredict (una vez por ejecución del script)."""
    st.markdown(CSS, unsafe_allow_html=True)


def _logo_html(nombre: str) -> str:
    """Parte el nombre en su segunda mayúscula: 'MoviePredict' -> 'Movie<b>Predict</b>'."""
    corte = next((i for i, c in enumerate(nombre) if i > 0 and c.isupper()), None)
    if corte is None:
        return f"<b>{nombre}</b>"
    return f"{nombre[:corte]}<b>{nombre[corte:]}</b>"


def cabecera_html() -> str:
    """Logotipo y lema (la cabecera de todas las pantallas).

    El logo se arma desde NOMBRE_APP: la primera palabra en peso normal y el resto
    en negrita ("Movie<b>Predict</b>"), así un cambio de nombre no deja restos.
    """
    return (f'<div class="cr-marca"><span class="cr-logo">{_logo_html(NOMBRE_APP)}</span>'
            f'<span class="cr-lema">{LEMA}</span></div>')


def pie_html(atribucion: str = "") -> str:
    """Pie con el nombre de la app, las fuentes de datos y la atribución a TMDb."""
    extra = f"<br>{atribucion}" if atribucion else ""
    return (f'<div class="cr-pie"><b>{NOMBRE_APP}</b> · recomendaciones con MovieLens 25M, '
            f'Spark y Kafka{extra}</div>')
