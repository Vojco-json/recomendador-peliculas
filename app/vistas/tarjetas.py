"""Piezas HTML de CineRuta: tarjeta «Para ti», histograma de notas y datos de la ficha.

La explicación SIEMPRE sale de ``Catalogo.explicar`` (títulos, tags y personas
concretas). El LLM, si está configurado y responde, solo REDACTA esos mismos datos
en una frase más natural; si no responde, devuelve algo raro o el proveedor es
'reglas', se usa la plantilla de ``explicar`` tal cual.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from recomendador import config, llm
from recomendador.catalogo_app import PROBABLE

from .comun import esc

log = logging.getLogger(__name__)

LIMITE_REDACCION_S = 8.0      # tope de espera al LLM para redactar una explicación
MAX_REDACCION = 420           # caracteres máximos aceptados de la redacción
ETIQUETAS_HISTO = ["½", "1", "1½", "2", "2½", "3", "3½", "4", "4½", "5"]

SISTEMA_REDACCION = (
    "Eres el redactor de CineRuta. Recibes en JSON por qué a una persona podría gustarle "
    "(o no) una película. Escribe en español de Chile, en 1 o 2 frases y en segunda persona, "
    "una explicación natural usando SOLO esos datos: no inventes títulos, personas ni tags, "
    "no uses números, notas ni estrellas. Responde solo con el texto.")


# --------------------------------------------------------------------------- #
# Redacción (LLM opcional con la plantilla como respaldo)
# --------------------------------------------------------------------------- #

def _redaccion_valida(texto: str, e: dict) -> bool:
    """Acepta el texto del LLM solo si es breve, sin cifras y sin comillas a títulos ajenos."""
    if not texto or len(texto) > MAX_REDACCION or re.search(r"\d", texto):
        return False
    permitidos = {e["titulo"]}
    for clave in ("parecida_alta", "parecida_baja"):
        if e.get(clave):
            permitidos.add(e[clave]["titulo"])
    if e.get("persona_comun"):
        permitidos.update(e["persona_comun"]["peliculas"])
    citados = re.findall(r"[«\"“]([^»\"”]+)[»\"”]", texto)
    return all(c.strip() in permitidos for c in citados)


def redactar(e: dict, memoria: dict | None = None, cliente=None) -> tuple[str, str]:
    """Texto de la explicación y su origen ('llm' o 'plantilla').

    ``memoria`` (p. ej. un dict en st.session_state) evita volver a llamar al LLM por
    la misma película y veredicto. Nunca lanza excepciones.
    """
    plantilla = e["texto"]
    if str(config.LLM_PROVIDER).lower() == "reglas":
        return plantilla, "plantilla"
    clave = f"{e['movieId']}|{e['veredicto']}|{plantilla}"
    if memoria is not None and clave in memoria:
        return memoria[clave]
    salida = (plantilla, "plantilla")
    # Modo público: el LLM atiende de a una solicitud; si está ocupado, plantilla ya.
    publico = bool(getattr(config, "MODO_PUBLICO", False))
    if publico and not llm.SEMAFORO_LLM.acquire(blocking=False):
        return salida
    try:
        datos = {k: e.get(k) for k in ("titulo", "veredicto", "parecida_alta", "parecida_baja",
                                       "tags_a_favor", "tags_en_contra", "persona_comun")}
        for k in ("parecida_alta", "parecida_baja"):        # sin la nota numérica
            if datos[k]:
                datos[k] = {"titulo": datos[k]["titulo"]}
        if datos["persona_comun"]:
            datos["persona_comun"] = {c: datos["persona_comun"][c]
                                      for c in ("nombre", "rol", "le_gusto", "peliculas")}
        resp = llm._llamar_con_limite(
            cliente or llm.crear_cliente(), LIMITE_REDACCION_S, model=config.LLM_MODEL,
            messages=[{"role": "system", "content": SISTEMA_REDACCION},
                      {"role": "user", "content": json.dumps(datos, ensure_ascii=False)}],
            think=config.LLM_THINK)
        texto = llm._limpiar_thinking(llm._campo(llm._campo(resp, "message", {}),
                                                 "content", "") or "").strip()
        if _redaccion_valida(texto, e):
            salida = (texto, "llm")
    except Exception as exc:  # Ollama caído, lento o sin el modelo: plantilla
        log.info("Redacción con LLM no disponible (%s); uso la plantilla.", exc)
    finally:
        if publico:
            llm.SEMAFORO_LLM.release()
    if memoria is not None:
        memoria[clave] = salida
    return salida


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #

def chips(etiquetas, clase: str = "") -> str:
    return "".join(f'<span class="chip {clase}">{esc(t)}</span>' for t in etiquetas or [])


def explicacion_html(e: dict, texto: str | None = None) -> str:
    """Tarjeta con veredicto, texto y etiquetas a favor / en contra / persona en común."""
    probable = e["veredicto"] == PROBABLE
    clase = "si" if probable else "no"
    filas = []
    if e.get("tags_a_favor"):
        filas.append(f'<div class="cr-exp-fila"><span>A favor</span>'
                     f'{chips(e["tags_a_favor"], "favor")}</div>')
    if e.get("tags_en_contra"):
        filas.append(f'<div class="cr-exp-fila"><span>En contra</span>'
                     f'{chips(e["tags_en_contra"], "contra")}</div>')
    p = e.get("persona_comun")
    if p:
        rol = "Director" if p["rol"] == "director" else "Actor/actriz"
        filas.append(f'<div class="cr-exp-fila"><span>{rol} en común</span>'
                     f'{chips([p["nombre"]], "favor" if p["le_gusto"] else "contra")}</div>')
    return (f'<div class="cr-exp {clase}" data-movie="{int(e["movieId"])}">'
            f'<div class="cr-veredicto">{esc(e["veredicto"])}</div>'
            f'<div class="cr-exp-peli">{esc(e["titulo"])}</div>'
            f'<p>{esc(texto or e["texto"])}</p>{"".join(filas)}</div>')


def para_ti_html(tarjeta: dict, textos: dict | None = None) -> str:
    """Tarjeta 'Para ti': la probable y la improbable lado a lado."""
    textos = textos or {}
    partes = [explicacion_html(tarjeta[k], textos.get(k))
              for k in ("probable", "improbable") if tarjeta.get(k)]
    if not partes:
        return ""
    return (f'<section class="cr-para-ti"><h3>Tu tarjeta «Para ti»</h3>'
            f'<div class="cr-par">{"".join(partes)}</div></section>')


def histograma_html(conteos: list[int], promedio: float | None, votos: int) -> str:
    """Histograma de notas de MovieLens (10 casillas de 0,5 a 5,0) con el promedio."""
    conteos = list(conteos or []) + [0] * (10 - len(conteos or []))
    tope = max(conteos) or 1
    barras = "".join(
        f'<div class="b" title="{esc(et)} estrellas: {c} votos">'
        f'<i style="height:{max(2, round(100 * c / tope))}%"></i><span>{esc(et)}</span></div>'
        for et, c in zip(ETIQUETAS_HISTO, conteos[:10]))
    prom = (f'<b>{promedio:.1f}</b> de 5 · {votos:,} votos'.replace(",", ".")
            if promedio is not None and votos else "Aún sin votos en MovieLens")
    return (f'<div class="cr-histo" aria-label="Histograma de notas"><div class="cr-histo-cab">'
            f'<span>Histograma de notas</span><span>{prom}</span></div>'
            f'<div class="cr-barras">{barras}</div></div>')


def duracion(minutos) -> str | None:
    if not minutos:
        return None
    h, m = divmod(int(minutos), 60)
    return f"{h} h {m:02d} min" if h else f"{m} min"


def datos_html(f: dict) -> str:
    """Bloque de datos: año, géneros, duración, IMDb, director y reparto."""
    meta = [str(f["anio"]) if f.get("anio") else None, duracion(f.get("duracion_min")),
            " · ".join(f.get("generos_es") or []) or None]
    linea = " · ".join(m for m in meta if m)
    imdb = ""
    if f.get("imdb_rating"):
        votos = f" ({int(f['imdb_votos']):,} votos)".replace(",", ".") if f.get("imdb_votos") else ""
        imdb = f'<div class="cr-dato"><span>Nota IMDb</span><b>{f["imdb_rating"]:.1f}</b>{votos}</div>'
    director = esc(", ".join(f.get("directores") or [])) or "Sin datos de IMDb"
    reparto = esc(", ".join((f.get("reparto") or [])[:8])) or "Sin datos de IMDb"
    return (f'<div class="cr-ficha-meta">{esc(linea)}</div>{imdb}'
            f'<div class="cr-dato"><span>Director</span>{director}</div>'
            f'<div class="cr-dato"><span>Reparto</span>{reparto}</div>')


def resenas_html(resenas: list[dict]) -> str:
    """Lista de reseñas públicas (sin controles de moderación: eso vive fuera de la app)."""
    if not resenas:
        return '<div class="cr-resenas vacio">Aún no hay reseñas. ¡Escribe la primera!</div>'
    items = []
    for r in resenas:
        fecha = datetime.fromtimestamp(int(r["timestamp"])).strftime("%d-%m-%Y")
        items.append(f'<article class="cr-resena"><div class="cab"><b>{esc(r["autor"])}</b>'
                     f'<span>{fecha}</span></div><p>{esc(r["texto"])}</p></article>')
    return f'<div class="cr-resenas">{"".join(items)}</div>'
