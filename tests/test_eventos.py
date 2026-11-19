"""Paquete 7: eventos a Kafka (ratings y reseñas), respaldo directo, filtro y moderación.

Todo con un productor simulado: no necesita broker ni Spark.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from recomendador import estado, eventos, moderacion
from scripts import moderar_resenas
from scripts.productor_kafka import a_json

RAIZ = Path(__file__).resolve().parent.parent


class ProductorFalso:
    """Imita confluent_kafka.Producer: guarda los mensajes y confirma la entrega."""

    def __init__(self, caido: bool = False, explota: bool = False):
        self.caido = caido
        self.explota = explota
        self.mensajes: list[tuple[str, bytes, bytes]] = []
        self._pendientes = []

    def produce(self, topic, value=None, key=None, on_delivery=None):
        if self.explota:
            raise RuntimeError("broker caído")
        self.mensajes.append((topic, value, key))
        self._pendientes.append(on_delivery)

    def flush(self, timeout=None):
        pendientes = self._pendientes
        self._pendientes = []
        if self.caido:                      # nada confirmado dentro del plazo
            return len(pendientes)
        for cb in pendientes:
            cb(None, None)
        return 0


@pytest.fixture
def db(tmp_path):
    return tmp_path / "estado.db"


# --------------------------------------------------------------------------- #
# Kafka: topics, formato y key
# --------------------------------------------------------------------------- #

def test_rating_a_topic_ratings_con_el_formato_de_siempre(db):
    prod = ProductorFalso()
    r = eventos.publicar_rating(162542, 1, 4.5, timestamp=1700000000, productor=prod,
                                estado_db=db)
    assert r.ok and r.via == "kafka" and r.aviso == ""
    assert len(prod.mensajes) == 1
    topic, valor, clave = prod.mensajes[0]
    assert topic == "ratings"
    assert clave == b"162542"
    esperado = {"userId": 162542, "movieId": 1, "rating": 4.5, "timestamp": 1700000000}
    assert valor.decode() == a_json(esperado)          # idéntico al productor verificado
    assert valor == b'{"userId":162542,"movieId":1,"rating":4.5,"timestamp":1700000000}'
    con = estado.conectar(db)
    assert estado.contar(con, "eventos") == 0          # con Kafka arriba no hay respaldo
    con.close()


def test_resena_a_topic_resenas_con_key_userid(db):
    prod = ProductorFalso()
    r = eventos.publicar_resena(162543, 2571, "  Una joya del cine de acción.  ",
                                timestamp=1700000100, productor=prod, estado_db=db)
    assert r.ok and r.via == "kafka" and r.aviso == ""
    topic, valor, clave = prod.mensajes[0]
    assert topic == "resenas" and clave == b"162543"
    cuerpo = json.loads(valor.decode())
    assert list(cuerpo) == ["uid", "userId", "movieId", "texto", "timestamp"]
    assert cuerpo["userId"] == 162543 and cuerpo["movieId"] == 2571
    assert cuerpo["texto"] == "Una joya del cine de acción."
    assert len(cuerpo["uid"]) == 32


def test_rating_fuera_de_escala_no_se_publica(db):
    prod = ProductorFalso()
    with pytest.raises(ValueError):
        eventos.publicar_rating(162542, 1, 5.5, productor=prod, estado_db=db)
    with pytest.raises(ValueError):
        eventos.publicar_rating(162542, 1, 3.3, productor=prod, estado_db=db)
    assert prod.mensajes == []


# --------------------------------------------------------------------------- #
# Respaldo directo
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("prod", [ProductorFalso(caido=True), ProductorFalso(explota=True)],
                         ids=["sin_confirmacion", "excepcion"])
def test_kafka_caido_guarda_en_estado_db_con_aviso(db, prod):
    r1 = eventos.publicar_rating(162544, 6, 3.0, timestamp=1700000200, productor=prod,
                                 estado_db=db)
    r2 = eventos.publicar_resena(162544, 6, "Buenísima, la volvería a ver.",
                                 timestamp=1700000300, productor=prod, estado_db=db)
    for r in (r1, r2):
        assert r.ok and r.via == "respaldo"
        assert r.aviso.strip() and "Kafka" in r.aviso
    con = estado.conectar(db)
    assert estado.eventos_usuario(con, 162544) == [(6, 3.0)]
    publicas = estado.resenas_pelicula(con, 6)
    assert [x["texto"] for x in publicas] == ["Buenísima, la volvería a ver."]
    assert publicas[0]["uid"] == r2.evento["uid"] and publicas[0]["oculta"] == 0
    # La misma reseña llegada además por el streaming no se duplica (mismo uid).
    assert estado.guardar_resenas(con, [r2.evento]) == []
    con.close()


# --------------------------------------------------------------------------- #
# Filtro de reseñas
# --------------------------------------------------------------------------- #

def test_resena_de_501_caracteres_se_rechaza(db):
    prod = ProductorFalso()
    r = eventos.publicar_resena(1, 1, "a" * 501, productor=prod, estado_db=db)
    assert not r.ok and r.via == "rechazada" and "500" in r.aviso
    assert prod.mensajes == []
    ok = eventos.publicar_resena(1, 1, "a" * 500, productor=prod, estado_db=db)
    assert ok.ok and len(prod.mensajes) == 1


@pytest.mark.parametrize("texto", ["Qué película más idiota", "QUÉ PELÍCULA MÁS IDIOTA",
                                   "qué película más idióta", "Es una MIERDA.",
                                   "puro weón actuando", "hijo   de PUTA el villano",
                                   "what a piece of SHIT"])
def test_resena_ofensiva_se_rechaza_sin_tildes_ni_mayusculas(db, texto):
    prod = ProductorFalso()
    r = eventos.publicar_resena(1, 1, texto, productor=prod, estado_db=db)
    assert not r.ok and r.via == "rechazada"
    assert r.aviso == moderacion.MENSAJE_OFENSIVA
    assert prod.mensajes == []
    con = estado.conectar(db)
    assert estado.contar(con, "resenas") == 0          # tampoco va al respaldo
    con.close()


@pytest.mark.parametrize("texto", ["La vi en el computador y me encantó",  # «puta» dentro
                                   "Dos horas y pico, pero vale la pena",
                                   "Adaptación de Philip K. Dick muy fiel",
                                   "Una disputa familiar contada con cariño"])
def test_resena_valida_se_acepta_palabra_completa(db, texto):
    assert moderacion.revisar_resena(texto).ok
    r = eventos.publicar_resena(1, 1, texto, productor=ProductorFalso(), estado_db=db)
    assert r.ok and r.via == "kafka"


def test_resena_vacia_se_rechaza():
    assert not moderacion.revisar_resena("   ").ok


def test_lista_de_palabras_normalizada():
    lista = moderacion.cargar_lista()
    assert ("idiota",) in lista and ("hijo", "de", "puta") in lista
    assert all(p == moderacion.normalizar(p) for expr in lista for p in expr)


# --------------------------------------------------------------------------- #
# Moderación local
# --------------------------------------------------------------------------- #

def _sembrar(db):
    con = estado.conectar(db)
    estado.guardar_resenas(con, [
        {"uid": "a1", "userId": 162542, "movieId": 1, "texto": "Linda", "timestamp": 10},
        {"uid": "b2", "userId": 162543, "movieId": 1, "texto": "Spam spam", "timestamp": 20},
    ])
    ids = {r["uid"]: r["id"] for r in estado.listar_resenas(con)}
    con.close()
    return ids


def test_moderar_resenas_ocultar_y_mostrar(db, capsys):
    ids = _sembrar(db)
    assert moderar_resenas.main(["--estado", str(db), "--listar"]) == 0
    salida = capsys.readouterr().out
    assert "Spam spam" in salida and "visible" in salida

    assert moderar_resenas.main(["--estado", str(db), "--ocultar", str(ids["b2"])]) == 0
    con = estado.conectar(db)
    assert [r["texto"] for r in estado.resenas_pelicula(con, 1)] == ["Linda"]
    assert estado.resenas_usuario(con, 162543) == []
    fila = con.execute("SELECT oculta FROM resenas WHERE id=?", (ids["b2"],)).fetchone()
    assert fila[0] == 1                                 # se oculta, no se borra
    con.close()

    assert moderar_resenas.main(["--estado", str(db), "--mostrar", str(ids["b2"])]) == 0
    con = estado.conectar(db)
    assert [r["texto"] for r in estado.resenas_pelicula(con, 1)] == ["Spam spam", "Linda"]
    con.close()
    assert moderar_resenas.main(["--estado", str(db), "--ocultar", "9999"]) == 1


def test_moderar_por_subproceso(db):
    """El script también funciona como comando (como lo usa el administrador)."""
    import subprocess
    import sys

    ids = _sembrar(db)
    r = subprocess.run([sys.executable, str(RAIZ / "scripts" / "moderar_resenas.py"),
                        "--estado", str(db), "--ocultar", str(ids["a1"])],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    con = estado.conectar(db)
    assert [x["texto"] for x in estado.resenas_pelicula(con, 1)] == ["Spam spam"]
    con.close()


def test_app_no_importa_moderacion_local():
    """Ningún módulo de app/ importa moderar_resenas ni toca el flag oculta."""
    archivos = sorted((RAIZ / "app").rglob("*.py"))
    assert archivos, "no se encontraron módulos en app/"
    for archivo in archivos:
        codigo = archivo.read_text(encoding="utf-8")
        arbol = ast.parse(codigo)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                nombres = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom):
                nombres = [nodo.module or ""] + [a.name for a in nodo.names]
            else:
                continue
            assert not any("moderar_resenas" in n for n in nombres), archivo
        assert "marcar_oculta" not in codigo, archivo
