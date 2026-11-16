"""Paquete 7: cuentas locales (bcrypt + SQLite, userId > 162541) y límite de 20 acciones/min."""

from __future__ import annotations

import sqlite3

import pytest

from recomendador import cuentas


@pytest.fixture
def db(tmp_path):
    return tmp_path / "cuentas.db"


def test_clave_solo_como_hash_bcrypt_y_login(db):
    uid = cuentas.registrar("ana_cine", "secreta123", db, rondas=4)
    con = sqlite3.connect(db)
    columnas = [c[1] for c in con.execute("PRAGMA table_info(usuarios)")]
    filas = con.execute("SELECT * FROM usuarios").fetchall()
    crudo = open(db, "rb").read()
    con.close()
    assert columnas == ["userId", "nombre", "hash", "creado_en"]
    hash_ = bytes(filas[0][2])
    assert hash_.startswith(b"$2b$04$") and len(hash_) == 60        # formato bcrypt
    assert b"secreta123" not in hash_ and b"secreta123" not in crudo  # nunca en claro
    assert cuentas.login("ana_cine", "secreta123", db) == uid
    assert cuentas.login("ANA_CINE", "secreta123", db) == uid          # nombre sin mayúsc.
    assert cuentas.login("ana_cine", "Secreta123", db) is None
    assert cuentas.login("ana_cine", "", db) is None
    assert cuentas.login("nadie", "secreta123", db) is None


def test_userids_despues_de_movielens_y_nombre_duplicado(db):
    primero = cuentas.registrar("ana", "clave-uno", db, rondas=4)
    segundo = cuentas.registrar("beto", "clave-dos", db, rondas=4)
    assert primero >= 162542 and primero > cuentas.ULTIMO_USERID_MOVIELENS
    assert segundo > primero
    with pytest.raises(cuentas.CuentaError, match="ya está en uso"):
        cuentas.registrar("ana", "otra-clave", db, rondas=4)
    with pytest.raises(cuentas.CuentaError):
        cuentas.registrar("Ana", "otra-clave", db, rondas=4)        # mismo nombre
    tercero = cuentas.registrar("carla", "clave-tres", db, rondas=4)
    assert tercero == segundo + 1                                  # el rechazo no gasta id
    assert cuentas.nombre_de(segundo, db) == "beto"


def test_validaciones_de_registro(db):
    with pytest.raises(cuentas.CuentaError):
        cuentas.registrar("ab", "clave-larga", db, rondas=4)       # nombre corto
    with pytest.raises(cuentas.CuentaError):
        cuentas.registrar("con espacio", "clave-larga", db, rondas=4)
    with pytest.raises(cuentas.CuentaError):
        cuentas.registrar("valido", "corta", db, rondas=4)


def test_rondas_por_defecto_son_12():
    assert cuentas.RONDAS_BCRYPT >= 12


class Reloj:
    """Reloj simulado: avanza solo cuando la prueba lo pide."""

    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_limitador_20_por_minuto_con_reloj_simulado():
    reloj = Reloj()
    lim = cuentas.Limitador(reloj=reloj)
    assert lim.maximo == 20 and lim.ventana_s == 60
    for i in range(20):
        assert lim.permitir("calificar"), f"la acción {i + 1} debía aceptarse"
        reloj.t += 1                       # 20 acciones en 20 s
    assert lim.permitir("reseñar") is False          # la 21.ª dentro del minuto
    assert lim.restantes() == 0
    aviso = lim.aviso()
    assert "20 acciones por minuto" in aviso and "no se registró" in aviso
    reloj.t = 1000.0 + 59.9                           # aún dentro del minuto de la 1.ª
    assert lim.permitir("chat") is False
    reloj.t = 1000.0 + 60                             # la 1.ª cumple 60 s
    assert lim.permitir("chat") is True
    assert lim.permitir("chat") is False              # la 2.ª aún no cumple 60 s


def test_limitador_rechazo_no_se_cuenta():
    reloj = Reloj()
    lim = cuentas.Limitador(reloj=reloj)
    for _ in range(20):
        assert lim.permitir()
    for _ in range(50):                    # insistir no alarga el bloqueo
        assert not lim.permitir()
    reloj.t += 60
    assert lim.restantes() == 20
    assert all(lim.permitir() for _ in range(20))
    assert not lim.permitir()
