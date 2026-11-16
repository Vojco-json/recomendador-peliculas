"""App CineRuta (AppTest): Buscar, Ficha, calificar/reseñar con filtro y respaldo, Explorar.

Usa los artefactos de la muestra mini (ALS + perfiles + app/*.parquet con IMDb mini)
en una carpeta temporal. Kafka se simula caído (respaldo directo a estado.db), no hay
TMDB_API_KEY (carátulas generadas) y el LLM usa el proveedor 'reglas' (plantilla).
"""

from __future__ import annotations

import re
import sys

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import crear_artefactos_cineruta
from recomendador import config, estado, eventos
from recomendador.catalogo_app import IMPROBABLE, PROBABLE
from recomendador.enriquecimiento import ATRIBUCION_TMDB

APP = config.RAIZ / "app" / "app.py"
CARPETA_APP = config.RAIZ / "app"
AVISO_KAFKA = "No pudimos conectar con el servidor de eventos (Kafka)"


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_cineruta(spark, ruta_mini, tmp_path_factory.mktemp("ficha"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    """Configuración apuntando a la mini, estado.db propio, sin TMDb y Kafka 'caído'."""
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    return tmp_path / "estado.db"


# --------------------------------------------------------------------------- #
# Ayudas
# --------------------------------------------------------------------------- #

def _html(at) -> str:
    return "\n".join(str(m.value) for m in at.markdown)


def _textos(at) -> str:
    """Todo lo visible: markdown, captions, avisos y errores."""
    partes = [_html(at)]
    for grupo in (at.caption, at.info, at.warning, at.error, at.success, at.exception):
        partes += [str(x.value) for x in grupo]
    return "\n".join(partes)


def _boton(at, etiqueta: str, clave: str | None = None):
    return next(b for b in at.button if b.label == etiqueta and (clave is None or b.key == clave))


def _sesion_con_notas(nombre: str) -> tuple[AppTest, int]:
    """Registra una cuenta e importa el CSV de ejemplo (salta el onboarding)."""
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input(nombre)
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    _boton(at, "Crear cuenta").click().run()
    _boton(at, "Probar con un CSV de ejemplo").click().run()
    assert not at.exception, at.exception
    return at, int(at.session_state["usuario"]["userId"])


def _abrir_ficha_nolan(at) -> int:
    """Busca 'nolan' y abre la ficha del primer resultado; devuelve su movieId."""
    at.text_input(key="buscar_q").input("nolan").run()
    assert not at.exception, at.exception
    html = _html(at)
    assert "resultados para «nolan»" in html
    assert "Director: Christopher Nolan" in html
    ver = [b for b in at.button if b.key and b.key.startswith("ver_")]
    assert ver, "buscar 'nolan' no devolvió resultados"
    movieId = int(ver[0].key.split("_")[1])
    ver[0].click().run()
    assert not at.exception, at.exception
    return movieId


def _eventos(db, userId) -> dict[int, float]:
    con = estado.conectar(db)
    try:
        return dict(estado.eventos_usuario(con, userId))
    finally:
        con.close()


def _resenas(db, movieId) -> list[dict]:
    if not db.exists():
        return []
    con = estado.conectar(db)
    try:
        return estado.resenas_pelicula(con, movieId)
    finally:
        con.close()


# --------------------------------------------------------------------------- #
# C1 y C2: buscar y ficha sin clave de TMDb
# --------------------------------------------------------------------------- #

def test_buscar_nolan_y_ficha_sin_tmdb(entorno):
    at, _ = _sesion_con_notas("fer_ficha")
    movieId = _abrir_ficha_nolan(at)
    html = _html(at)
    # Datos de la ficha: Director y Reparto vía links.csv + IMDb, histograma y atribución.
    assert "<span>Director</span>" in html and "Christopher Nolan" in html
    assert "<span>Reparto</span>" in html
    assert 'class="cr-histo"' in html and "Histograma de notas" in html
    assert "cr-barras" in html
    assert ATRIBUCION_TMDB in html
    # Sin TMDB_API_KEY: carátula SVG generada, sin sinopsis ni tráiler y sin errores.
    assert 'class="cr-poster" src="data:image/svg+xml' in html
    assert not at.get("video") and 'class="cr-sub">Tráiler<' not in html
    textos = _textos(at)
    assert "Error" not in textos and "Traceback" not in textos
    assert not at.exception and not at.error
    # Botones para calificar y reseñar (con el contador de 500 caracteres).
    assert _boton(at, "Guardar calificación")
    assert at.text_area(key=f"bus_resena_{movieId}_0").max_chars == 500
    assert "Reseñas (0)" in html
    # Volver a los resultados.
    _boton(at, "← Volver a los resultados").click().run()
    assert "resultados para «nolan»" in _html(at)


def test_buscar_ignora_tildes_y_mayusculas(entorno):
    at, _ = _sesion_con_notas("gabi_tildes")
    at.text_input(key="buscar_q").input("NÓLÁN").run()
    assert not at.exception, at.exception
    assert "Director: Christopher Nolan" in _html(at)


# --------------------------------------------------------------------------- #
# C3: calificar con Kafka caído
# --------------------------------------------------------------------------- #

def test_calificar_con_kafka_caido(entorno):
    at, uid = _sesion_con_notas("hugo_nota")
    movieId = _abrir_ficha_nolan(at)
    assert movieId not in _eventos(entorno, uid)
    at.selectbox(key=f"bus_nota_{movieId}").set_value(4.5)
    _boton(at, "Guardar calificación").click().run()
    assert not at.exception, at.exception
    avisos = [str(w.value) for w in at.warning]
    assert any(AVISO_KAFKA in a for a in avisos), avisos
    assert _eventos(entorno, uid)[movieId] == 4.5
    # El tipo de espectador se recalcula con la nueva nota.
    assert any("Tu tipo de espectador ahora:" in str(i.value) for i in at.info)
    assert "Ya la calificaste con ★★★★½" in _textos(at)


# --------------------------------------------------------------------------- #
# C4: reseñas con filtro y respaldo
# --------------------------------------------------------------------------- #

def test_resena_valida_se_publica_y_aparece(entorno):
    at, uid = _sesion_con_notas("ines_resena")
    movieId = _abrir_ficha_nolan(at)
    texto = "Un rompecabezas precioso, la música te acompaña todo el rato."
    at.text_area(key=f"bus_resena_{movieId}_0").input(texto)
    _boton(at, "Publicar reseña").click().run()
    assert not at.exception, at.exception
    assert "Publicamos tu reseña." in [str(s.value) for s in at.success]
    assert any(AVISO_KAFKA in str(w.value) for w in at.warning)
    guardadas = _resenas(entorno, movieId)
    assert [(r["userId"], r["texto"]) for r in guardadas] == [(uid, texto)]
    html = _html(at)
    assert "Reseñas (1)" in html and texto in html and "ines_resena" in html
    # La caja quedó vacía para escribir otra.
    assert at.text_area(key=f"bus_resena_{movieId}_1").value in ("", None)


def test_resena_ofensiva_se_rechaza(entorno):
    at, _ = _sesion_con_notas("juan_ofensa")
    movieId = _abrir_ficha_nolan(at)
    at.text_area(key=f"bus_resena_{movieId}_0").input("Qué película más mala, WEÓN.")
    _boton(at, "Publicar reseña").click().run()
    assert not at.exception, at.exception
    assert at.error, "la reseña ofensiva no mostró mensaje"
    assert not _resenas(entorno, movieId)
    assert "Reseñas (0)" in _html(at)


def test_resena_larga_se_rechaza(entorno):
    """Más de 500 caracteres: la caja lo impide (max_chars) y la función lo rechaza."""
    sys.path.insert(0, str(CARPETA_APP))
    from vistas.comun import publicar_resena

    r = publicar_resena(170050, 58559, "a" * 501, entorno)
    assert not r.ok and r.via == "rechazada" and "500" in r.aviso
    assert not _resenas(entorno, 58559)
    r = publicar_resena(170050, 58559, "b" * 500, entorno)
    assert r.ok and len(_resenas(entorno, 58559)) == 1


# --------------------------------------------------------------------------- #
# C5: ningún control para ocultar reseñas
# --------------------------------------------------------------------------- #

def test_sin_controles_para_ocultar(entorno):
    at, _ = _sesion_con_notas("kati_mod")
    movieId = _abrir_ficha_nolan(at)
    at.text_area(key=f"bus_resena_{movieId}_0").input("Muy buena, la volvería a ver.")
    _boton(at, "Publicar reseña").click().run()
    assert "Reseñas (1)" in _html(at)
    etiquetas = [str(w.label) for grupo in (at.button, at.checkbox, at.toggle, at.selectbox,
                                            at.radio) for w in grupo]
    assert not [e for e in etiquetas if re.search(r"ocult|moder|borrar|eliminar", e, re.I)]
    # Tampoco en el código de la app: ninguna vista llama a estado.ocultar_resena.
    for archivo in CARPETA_APP.rglob("*.py"):
        fuente = archivo.read_text(encoding="utf-8")
        assert "ocultar_resena" not in fuente and "oculta=1" not in fuente, archivo


# --------------------------------------------------------------------------- #
# C6: Explorar
# --------------------------------------------------------------------------- #

def _paso(at) -> tuple[str, str]:
    """(texto 'Película i de n', movieId de la explicación mostrada)."""
    # Todas las pestañas se dibujan a la vez: se mira solo lo que viene después del
    # indicador de Explorar (la tarjeta 'Para ti' de Inicio va antes).
    html = _html(at)
    paso = re.search(r'class="cr-paso">(Película \d+ de \d+)<', html)
    assert paso, "Explorar no mostró el indicador de película"
    resto = html[paso.end():]
    exp = re.search(r'class="cr-exp (?:si|no)" data-movie="(\d+)"', resto)
    assert exp, "Explorar no mostró la explicación personalizada"
    veredicto = re.search(r'class="cr-veredicto">([^<]+)<', resto[exp.start():])
    assert veredicto and veredicto.group(1).lower() in ("probablemente te guste", "puede que no")
    return paso.group(1), exp.group(1)


def test_explorar_siguiente_cambia_de_pelicula(entorno):
    at, _ = _sesion_con_notas("lalo_explora")
    paso1, peli1 = _paso(at)
    assert paso1.startswith("Película 1 de")
    html = _html(at)
    assert PROBABLE in html or IMPROBABLE in html
    assert "probablemente te guste" in html.lower() or "puede que no" in html.lower()
    _boton(at, "Siguiente →").click().run()
    assert not at.exception, at.exception
    paso2, peli2 = _paso(at)
    assert paso2.startswith("Película 2 de") and peli2 != peli1
    html = _html(at)
    assert f'data-movie="{peli2}"' in html
    assert PROBABLE in html or IMPROBABLE in html
    textos = _textos(at)
    assert "Error" not in textos and "Traceback" not in textos


def test_tarjeta_para_ti_en_inicio(entorno):
    """La tarjeta 'Para ti' de Inicio muestra una probable y una improbable con etiquetas."""
    at, _ = _sesion_con_notas("mili_tarjeta")
    html = _html(at)
    bloque = re.search(r'<section class="cr-para-ti">(.*?)</section>', html, re.S)
    assert bloque, "falta la tarjeta 'Para ti'"
    assert PROBABLE in bloque.group(1) and IMPROBABLE in bloque.group(1)
    assert 'class="cr-exp si"' in bloque.group(1) and 'class="cr-exp no"' in bloque.group(1)
