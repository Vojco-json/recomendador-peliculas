"""Pedido final (PEDIDO_FINAL.md §1, §6, §7, §8 y §10) con la muestra mini.

- Integración con AppTest: la ficha muestra «Similares» (hasta 6, no calificadas, con
  veredicto y razón), la reseña muestra las estrellas de su autor y ninguna sección
  (carátulas, ficha, Explorar, chat) muestra años con «.0».
- Explicaciones con el catálogo real de la muestra: «Puede que no» nunca cita una
  película calificada alto, «se parece» solo con coseno ALS >= 0,3 y la improbable de
  «Para ti» trae al menos una razón concreta en contra.
"""

from __future__ import annotations

import re
import sys

import pytest
from streamlit.testing.v1 import AppTest

from base_mini import CORTE, clic_caratula, crear_artefactos_moviepredict, html_pagina, ir_a
from recomendador import catalogo_app, config, cuentas, estado, eventos

APP = config.RAIZ / "app" / "app.py"
sys.path.insert(0, str(config.RAIZ / "app"))

# Un año con decimal («1990.0») en cualquier parte del HTML visible.
ANIO_DECIMAL = re.compile(r"\b(?:18|19|20)\d\d\.0\b")


@pytest.fixture(scope="module")
def artefactos(spark, ruta_mini, tmp_path_factory):
    return crear_artefactos_moviepredict(spark, ruta_mini, tmp_path_factory.mktemp("pedido_final"))


@pytest.fixture
def entorno(monkeypatch, artefactos, ruta_mini, tmp_path):
    monkeypatch.setattr(config, "MODO", "completo")
    monkeypatch.setattr(config, "DATOS", ruta_mini)
    monkeypatch.setattr(config, "ARTEFACTOS_BASE", artefactos)
    monkeypatch.setattr(config, "ESTADO_DB", tmp_path / "estado.db")
    monkeypatch.setattr(config, "LLM_PROVIDER", "reglas")
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
    monkeypatch.setattr(eventos, "_productor_defecto", lambda *a, **k: None)
    monkeypatch.setattr(cuentas, "ruta_defecto", lambda: tmp_path / "cuentas.db")
    return tmp_path


def _sesion(nombre: str) -> AppTest:
    at = AppTest.from_file(str(APP), default_timeout=120)
    at.run()
    at.text_input(key="reg_nombre").input(nombre)
    at.text_input(key="reg_clave").input("secreta123")
    at.text_input(key="reg_clave2").input("secreta123")
    next(b for b in at.button if b.label == "Crear cuenta").click().run()
    next(b for b in at.button if b.label == "Probar con un CSV de ejemplo").click().run()
    assert not at.exception, at.exception
    return at


def _calificadas(userId: int) -> dict[int, float]:
    con = estado.lector(config.ESTADO_DB)
    try:
        filas = con.execute("SELECT movieId, rating FROM eventos WHERE userId = ? "
                            "ORDER BY timestamp", (userId,)).fetchall()
    finally:
        con.close()
    return {int(m): float(r) for m, r in filas}


VISTA_PREVIA = config.RAIZ / "vista_previa"


def _guardar_vista(at, nombre: str, titulo: str) -> None:
    """Deja una página estática (HTML de la app + estilos) para revisar a ojo con un
    navegador: vista_previa/<nombre>.html. Se regenera en cada corrida."""
    import estilo  # app/estilo.py
    VISTA_PREVIA.mkdir(exist_ok=True)
    cuerpo = html_pagina(at)
    (VISTA_PREVIA / f"{nombre}.html").write_text(
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{titulo}</title>{estilo.CSS}<style>body{{background:#0e1117;color:#e6edf3;"
        "font-family:system-ui,sans-serif;max-width:1100px;margin:0 auto;padding:24px}"
        "</style></head><body>"
        f"<p style='opacity:.6'>Vista previa generada por tests/test_pedido_final_app.py "
        f"(AppTest, muestra mini): {titulo}</p>{cuerpo}</body></html>", encoding="utf-8")


def _sin_anio_decimal(at, donde: str) -> None:
    html = html_pagina(at)
    assert not ANIO_DECIMAL.findall(html), f"{donde}: {ANIO_DECIMAL.findall(html)[:3]}"


def test_similares_resena_con_estrellas_y_anios_enteros(entorno):
    at = _sesion("similares")
    uid = at.session_state["usuario"]["userId"]
    vistas = _calificadas(uid)
    assert vistas, "el CSV de ejemplo no dejó calificaciones"
    _sin_anio_decimal(at, "Inicio (carátulas)")

    # Similares: se busca una ficha que tenga vecinos (la mini tiene pocas películas).
    ids = [int(x) for x in re.findall(r'class="cr-caratula" data-movie="(\d+)"',
                                      html_pagina(at))]
    con_similares = None
    for m in list(dict.fromkeys(ids + list(vistas)))[:15]:
        clic_caratula(at, m, "Inicio")
        assert not at.exception, at.exception
        if "cr-similares" in html_pagina(at):
            con_similares = m
            break
    assert con_similares is not None, "ninguna ficha mostró la sección Similares"
    html = html_pagina(at)
    assert "Similares" in html
    _sin_anio_decimal(at, "Ficha")
    _guardar_vista(at, "ficha_similares", "Ficha con Similares")
    bloque = html[html.index('<div class="cr-similares">'):]
    sims = [int(x) for x in re.findall(r'<div class="cr-sim">.*?data-movie="(\d+)"',
                                       bloque, flags=re.S)]
    assert 1 <= len(sims) <= 6
    assert con_similares not in sims
    assert not set(sims) & set(vistas), "Similares mostró películas ya calificadas"
    razones = re.findall(r'<div class="cr-sim-razon (si|no)"><b>([^<]+)</b>\s*([^<]+)</div>',
                         bloque)
    assert len(razones) == len(sims)
    for clase, veredicto, razon in razones:
        assert veredicto in (catalogo_app.PROBABLE, catalogo_app.IMPROBABLE)
        assert (clase == "si") == (veredicto == catalogo_app.PROBABLE)
        assert razon.strip(), "veredicto sin razón"

    # Reseña con las estrellas de la última calificación del autor (§8).
    calificada, nota = next(iter(vistas.items()))
    con = estado.conectar(config.ESTADO_DB)
    estado.guardar_resenas(con, [{"uid": "r1", "userId": uid, "movieId": calificada,
                                  "texto": "Me encantó la fotografía", "timestamp": 1}])
    con.close()
    clic_caratula(at, calificada, "Inicio")
    assert not at.exception, at.exception
    html = html_pagina(at)
    esperado = "★" * int(nota) + ("½" if nota % 1 else "")
    resena = re.search(r'<article class="cr-resena">.*?</article>', html, flags=re.S)
    assert resena and "Me encantó la fotografía" in resena.group(0)
    assert f'title="Su calificación">{esperado}</span>' in resena.group(0)
    _sin_anio_decimal(at, "Ficha con reseña")
    _guardar_vista(at, "ficha_resena", "Reseña con las estrellas del autor")

    # Explorar y Chat tampoco muestran años con decimal.
    ir_a(at, "Explorar")
    assert not at.exception, at.exception
    _sin_anio_decimal(at, "Explorar")
    ir_a(at, "Chat").chat_input[0].set_value("una comedia de los 90").run()
    assert not at.exception, at.exception
    assert "años 90" in html_pagina(at)                  # época en «Entendí:» / chips
    _sin_anio_decimal(at, "Chat")
    _guardar_vista(at, "chat_tema_epoca", "Chat: chips de tema y época")


# --------------------------------------------------------------------------- #
# Explicaciones coherentes con el veredicto (§7) con el catálogo de la muestra
# --------------------------------------------------------------------------- #

USUARIO = 162700
# Gustos marcados: acción/ciencia ficción alto, romance/familia bajo.
CALIFICA = [(6, 5.0), (1036, 5.0), (2571, 4.5), (589, 5.0), (47, 4.5),
            (1721, 1.5), (539, 1.0), (597, 1.5), (1307, 2.0), (356, 2.0)]


@pytest.fixture
def cat(artefactos, ruta_mini, tmp_path):
    db = tmp_path / "estado.db"
    con = estado.conectar(db)
    estado.guardar_eventos(con, [{"userId": USUARIO, "movieId": m, "rating": r,
                                  "timestamp": 1_700_000_000 + i}
                                 for i, (m, r) in enumerate(CALIFICA)])
    con.close()
    return catalogo_app.Catalogo(artefactos=artefactos, datos=ruta_mini, estado_db=db,
                                 corte=CORTE)


def test_explicaciones_alineadas_con_el_veredicto(cat):
    fichas = cat._peliculas()["fichas"]
    vistas = dict(CALIFICA)
    altas = {fichas[m]["titulo"] for m, r in CALIFICA if r >= 4 and m in fichas}
    n_no = n_parecida = 0
    for m in fichas:
        if m in vistas:
            continue
        e = cat.explicar(USUARIO, m)
        if e is None:
            continue
        for clave in ("parecida_alta", "parecida_baja"):
            if e[clave]:
                n_parecida += 1
                assert e[clave]["similitud"] >= catalogo_app.SIM_MINIMA == 0.3
        if not e["probable"]:
            n_no += 1
            assert e["veredicto"] == catalogo_app.IMPROBABLE
            assert "calificaste alto" not in e["texto"].replace("sueles calificar alto", "")
            assert not any(t in e["texto"] for t in altas), e["texto"]
            assert e["parecida_alta"] is None and not e["tags_a_favor"]
        else:
            assert e["parecida_baja"] is None and not e["tags_en_contra"]
    assert n_no > 0, "ninguna película quedó como «Puede que no»"

    tarjeta = cat.tarjeta_para_ti(USUARIO)
    imp = tarjeta["improbable"]
    assert imp and not imp["probable"]
    assert imp["razon_concreta"] and imp["razones"], imp["texto"]
