"""Piezas compartidas por las vistas de MoviePredict: contexto, carátulas y acciones.

La lógica que escribe (calificar, importar) vive aquí como funciones normales para
poder probarla sin Streamlit. Ninguna de estas funciones importa pyspark.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from pathlib import Path

from recomendador import estado, eventos
from recomendador.catalogo_app import Catalogo
from recomendador.cuentas import Limitador
from recomendador.herramientas import Herramientas
from recomendador.tmdb import TMDb

CLAVE_USUARIO = "usuario"          # st.session_state: {'userId', 'nombre'}
CLAVE_LIMITADOR = "limitador"      # st.session_state: Limitador de la sesión


@dataclass
class Contexto:
    """Todo lo que necesitan las vistas (se arma una vez por ejecución en app.py)."""
    cat: Catalogo
    herr: Herramientas
    tmdb: TMDb
    limitador: Limitador
    datos: Path
    estado_db: Path
    cuentas_db: Path
    usuario: dict | None = None
    avisos: list[str] = field(default_factory=list)

    @property
    def user_id(self) -> int | None:
        return int(self.usuario["userId"]) if self.usuario else None


def esc(texto) -> str:
    return html.escape("" if texto is None else str(texto))


# --------------------------------------------------------------------------- #
# Carátulas
# --------------------------------------------------------------------------- #

def precargar_posters(ctx: Contexto, pelis) -> None:
    """Pide en paralelo a TMDb los pósters que faltan antes de dibujar una grilla."""
    precargar = getattr(ctx.tmdb, "precargar", None)
    if precargar is not None:
        precargar([p for p in pelis if p])


def caratula_html(ctx: Contexto, peli: dict) -> str:
    """Una carátula 2:3 (póster de TMDb o SVG generado) con título y año."""
    src = ctx.tmdb.poster_src(peli)
    anio = f'<div class="a">{esc(peli.get("anio"))}</div>' if peli.get("anio") else ""
    return (f'<div class="cr-caratula" data-movie="{int(peli["movieId"])}">'
            f'<img src="{esc(src)}" alt="Carátula de {esc(peli["titulo"])}" loading="lazy">'
            f'<div class="t">{esc(peli["titulo"])}</div>{anio}</div>')


def fila_html(ctx: Contexto, titulo: str, pelis: list[dict], clave: str = "") -> str:
    """Fila con título y carril horizontal desplazable de carátulas."""
    precargar_posters(ctx, pelis)
    caratulas = "".join(caratula_html(ctx, p) for p in pelis if p)
    return (f'<section class="cr-fila" data-fila="{esc(clave)}"><h3>{esc(titulo)}</h3>'
            f'<div class="cr-carril">{caratulas}</div></section>')


# --------------------------------------------------------------------------- #
# Ficha global y carátulas clicables
# --------------------------------------------------------------------------- #

CLAVE_FICHA = "ficha_abierta"      # st.session_state: {'movieId', 'origen'} o ausente
CARPETA_COMPONENTE = Path(__file__).resolve().parent / "componente_caratulas"
_componente = None                 # se declara una sola vez por proceso (perezoso)


def _sesion(sesion=None):
    """El diccionario de sesión: el que se pasa (pruebas) o st.session_state."""
    if sesion is not None:
        return sesion
    import streamlit as st
    return st.session_state


def abrir_ficha(movieId: int, origen: str, sesion=None) -> dict:
    """Abre la ficha de una película desde la sección «origen» (Inicio, Buscar, Chat…).

    Solo escribe la clave única «ficha_abierta»: el usuario, la sección, el chat y la
    cola de Explorar quedan intactos. Devuelve el valor guardado.
    """
    valor = {"movieId": int(movieId), "origen": str(origen)}
    _sesion(sesion)[CLAVE_FICHA] = valor
    return valor


def cerrar_ficha(sesion=None) -> None:
    """«← Volver»: cierra la ficha y la sección de origen vuelve a mostrarse."""
    _sesion(sesion).pop(CLAVE_FICHA, None)


def ficha_abierta(sesion=None) -> dict | None:
    """La ficha abierta ({'movieId', 'origen'}) o None."""
    valor = _sesion(sesion).get(CLAVE_FICHA)
    return valor if isinstance(valor, dict) and "movieId" in valor else None


def procesar_clic(valor, clave: str, origen: str, sesion=None) -> int | None:
    """Convierte el valor del componente en una ficha abierta (si es un clic nuevo).

    El componente devuelve {'movieId', 'n'} y Streamlit lo repite en cada recarga;
    «n» (marca del clic) evita reabrir la ficha tras «Volver». Devuelve el movieId
    abierto o None si no hubo clic nuevo.
    """
    if not isinstance(valor, dict) or "movieId" not in valor:
        return None
    s = _sesion(sesion)
    marca = f"_clic_{clave}"
    if s.get(marca) == valor.get("n"):
        return None
    s[marca] = valor.get("n")
    abrir_ficha(int(valor["movieId"]), origen, s)
    return int(valor["movieId"])


def caratulas_clicables(html_interno: str, clave: str, origen: str) -> None:
    """Dibuja HTML de carátulas/tarjetas en el que todo elemento con data-movie es clicable.

    Usa un componente propio (app/vistas/componente_caratulas/index.html, sin
    dependencias): al hacer clic devuelve el movieId sin enlaces ni recarga de página,
    se abre la ficha global y se vuelve a ejecutar el script con la sesión intacta.
    """
    global _componente
    import streamlit as st
    import streamlit.components.v1 as components

    import estilo
    if _componente is None:
        _componente = components.declare_component("caratulas_moviepredict",
                                                   path=str(CARPETA_COMPONENTE))
    valor = _componente(html=html_interno, css=estilo.CSS, key=f"clic_{clave}", default=None)
    if procesar_clic(valor, clave, origen) is not None:
        st.rerun()


def estrellas(nota: float | None) -> str:
    """5.0 -> '★★★★★', 3.5 -> '★★★½', None -> 'No la vi'."""
    if nota is None:
        return "No la vi"
    return "★" * int(nota) + ("½" if nota % 1 else "")


# --------------------------------------------------------------------------- #
# Acciones que escriben (todas pasan por el Limitador desde las vistas)
# --------------------------------------------------------------------------- #

def publicar_calificacion(userId: int, movieId: int, rating: float, estado_db,
                          timestamp: int | None = None) -> eventos.Resultado:
    """Publica en Kafka (o respaldo directo) y deja la nota visible al instante.

    Si Kafka confirma, el evento llega a estado.db recién cuando el streaming lo
    procesa. Para que las recomendaciones sean inmediatas se guarda además una copia
    en estado.db con el timestamp un segundo antes: el streaming la verá como evento
    distinto (así igual actualiza el perfil y dispara el reentrenamiento) y, como
    la nota es la misma, el resultado del fold-in no cambia.
    """
    r = eventos.publicar_rating(userId, movieId, rating, timestamp, estado_db=estado_db)
    if r.ok and r.via == "kafka":
        copia = dict(r.evento, timestamp=int(r.evento["timestamp"]) - 1)
        con = estado.conectar(estado_db)
        try:
            estado.guardar_eventos(con, [copia])
        finally:
            con.close()
    return r


def publicar_resena(userId: int, movieId: int, texto: str, estado_db) -> eventos.Resultado:
    """Revisa y publica una reseña (Kafka o respaldo) y la deja visible al instante.

    Si Kafka confirma, se guarda además la misma reseña (mismo ``uid``) en estado.db:
    cuando el streaming la procese, ``INSERT OR IGNORE`` por uid evita el duplicado.
    Una reseña rechazada por el filtro no se publica ni se guarda.
    """
    r = eventos.publicar_resena(userId, movieId, texto, estado_db=estado_db)
    if r.ok and r.via == "kafka":
        con = estado.conectar(estado_db)
        try:
            estado.guardar_resenas(con, [r.evento])
        finally:
            con.close()
    return r


def calificar_varias(userId: int, pares: list[tuple[int, float]], estado_db,
                     limitador: Limitador) -> dict:
    """Califica en orden; cada nota es UNA acción del Limitador.

    Devuelve {'registradas': [movieId], 'limite': bool, 'avisos': [str]}. Al llegar
    al límite se detiene: esa nota y las siguientes no se registran.
    """
    registradas, avisos, limite = [], [], False
    for movieId, rating in pares:
        if not limitador.permitir("calificar"):
            limite = True
            avisos.append(limitador.aviso())
            break
        r = publicar_calificacion(userId, movieId, rating, estado_db)
        registradas.append(int(movieId))
        if r.aviso and r.aviso not in avisos:
            avisos.append(r.aviso)
    return {"registradas": registradas, "limite": limite, "avisos": avisos}


def importar_letterboxd(ruta_csv, userId: int, datos, estado_db,
                        limitador: Limitador) -> dict:
    """Importa un CSV de exportación de Letterboxd como calificaciones del usuario.

    Toda la importación cuenta como UNA acción del Limitador. Empareja por título y
    año con movies.csv (enriquecimiento.emparejar) y publica cada nota con la fecha
    del CSV. Devuelve {'ok', 'importadas', 'sueltas', 'avisos', 'error'}.
    """
    import pandas as pd

    from recomendador.enriquecimiento import emparejar, fecha_a_timestamp, leer_letterboxd

    salida = {"ok": False, "importadas": [], "sueltas": [], "avisos": [], "error": ""}
    if not limitador.permitir("importar"):
        salida["error"] = limitador.aviso()
        return salida
    try:
        filas = leer_letterboxd(ruta_csv)
    except (ValueError, KeyError, UnicodeDecodeError) as exc:
        salida["error"] = str(exc) or "El archivo no tiene el formato de Letterboxd."
        return salida
    movies = pd.read_csv(Path(datos) / "movies.csv", usecols=["movieId", "title"])
    emparejadas, sueltas = emparejar(filas, movies)
    salida["sueltas"] = [f"{f['titulo']} ({f['anio']})" if f.get("anio") else f["titulo"]
                         for f in sueltas]
    if not emparejadas:
        salida["error"] = ("Ninguna película del CSV coincide con el catálogo. Revisa que "
                           "sea la exportación de Letterboxd (ratings.csv).")
        return salida
    for fila in sorted(emparejadas, key=lambda f: f["fecha"]):
        r = publicar_calificacion(userId, fila["movieId"], fila["rating"], estado_db,
                                  timestamp=fecha_a_timestamp(fila["fecha"]))
        salida["importadas"].append(int(fila["movieId"]))
        if r.aviso and r.aviso not in salida["avisos"]:
            salida["avisos"].append(r.aviso)
    salida["ok"] = True
    return salida
