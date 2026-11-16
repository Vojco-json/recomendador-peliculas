"""Cuentas locales de CineRuta (registro y login) y límite de acciones por sesión.

Cuentas
-------
Se guardan en SQLite (por defecto ``<artefactos activos>/cuentas.db``), tabla
``usuarios(userId, nombre, hash, creado_en)``. La contraseña NUNCA se guarda en claro:
solo su hash bcrypt (que incluye la sal). Los userId nuevos parten en 162542, justo
después del último usuario de MovieLens 25M (162541), para no chocar con ellos.

Limitador
---------
``Limitador`` cuenta las acciones de una sesión (calificar, reseñar, mensaje de chat,
importar CSV) en una ventana deslizante de 60 s: acepta hasta 20 y rechaza la 21.ª con
un aviso, sin registrarla. El reloj se puede inyectar para probarlo sin esperar.
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections import deque
from pathlib import Path
from typing import Callable

import bcrypt

from . import config

ULTIMO_USERID_MOVIELENS = 162541          # máximo userId de ml-25m
PRIMER_USERID = ULTIMO_USERID_MOVIELENS + 1
MIN_CLAVE = 6
MAX_CLAVE = 72                            # bcrypt solo usa los primeros 72 bytes
PATRON_NOMBRE = re.compile(r"^[A-Za-z0-9_.\-]{3,30}$")
RONDAS_BCRYPT = 12

ESQUEMA = """
CREATE TABLE IF NOT EXISTS usuarios (
    userId    INTEGER PRIMARY KEY,
    nombre    TEXT    NOT NULL UNIQUE COLLATE NOCASE,
    hash      BLOB    NOT NULL,
    creado_en REAL    NOT NULL
);
"""


class CuentaError(ValueError):
    """Registro rechazado (nombre inválido o repetido, contraseña inválida)."""


def ruta_defecto() -> Path:
    """cuentas.db junto a los artefactos activos (25m o demo)."""
    return config.artefactos_activos() / "cuentas.db"


def conectar(ruta=None) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base de cuentas."""
    ruta = Path(ruta) if ruta else ruta_defecto()
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ruta), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript(ESQUEMA)
    con.commit()
    return con


def _validar(nombre: str, clave: str) -> str:
    nombre = (nombre or "").strip()
    if not PATRON_NOMBRE.match(nombre):
        raise CuentaError("El nombre de usuario debe tener entre 3 y 30 caracteres: letras "
                          "sin tildes, números, punto, guion o guion bajo.")
    clave = clave or ""
    if len(clave) < MIN_CLAVE:
        raise CuentaError(f"La contraseña debe tener al menos {MIN_CLAVE} caracteres.")
    if len(clave.encode("utf-8")) > MAX_CLAVE:
        raise CuentaError(f"La contraseña no puede pasar de {MAX_CLAVE} bytes.")
    return nombre


def registrar(nombre: str, clave: str, ruta=None, rondas: int = RONDAS_BCRYPT) -> int:
    """Crea la cuenta y devuelve su userId (>= 162542). Lanza CuentaError si no se puede."""
    nombre = _validar(nombre, clave)
    hash_ = bcrypt.hashpw(clave.encode("utf-8"), bcrypt.gensalt(rounds=rondas))
    con = conectar(ruta)
    try:
        # BEGIN IMMEDIATE toma el candado de escritura: dos registros simultáneos no
        # pueden leer el mismo MAX(userId).
        con.execute("BEGIN IMMEDIATE")
        try:
            if con.execute("SELECT 1 FROM usuarios WHERE nombre=?", (nombre,)).fetchone():
                raise CuentaError(f"El nombre «{nombre}» ya está en uso.")
            maximo = con.execute("SELECT MAX(userId) FROM usuarios").fetchone()[0]
            userId = max(int(maximo or 0), ULTIMO_USERID_MOVIELENS) + 1
            con.execute("INSERT INTO usuarios(userId, nombre, hash, creado_en) "
                        "VALUES (?, ?, ?, ?)", (userId, nombre, hash_, time.time()))
            con.commit()
        except BaseException:
            con.rollback()
            raise
        return userId
    finally:
        con.close()


def login(nombre: str, clave: str, ruta=None) -> int | None:
    """userId si el nombre y la contraseña son correctos; None si no."""
    con = conectar(ruta)
    try:
        fila = con.execute("SELECT userId, hash FROM usuarios WHERE nombre=?",
                           ((nombre or "").strip(),)).fetchone()
    finally:
        con.close()
    if fila is None or not clave:
        return None
    try:
        ok = bcrypt.checkpw(clave.encode("utf-8"), bytes(fila["hash"]))
    except ValueError:
        return None
    return int(fila["userId"]) if ok else None


def nombre_de(userId: int, ruta=None) -> str | None:
    """Nombre visible de una cuenta (para firmar reseñas), o None."""
    con = conectar(ruta)
    try:
        fila = con.execute("SELECT nombre FROM usuarios WHERE userId=?",
                           (int(userId),)).fetchone()
    finally:
        con.close()
    return None if fila is None else str(fila[0])


# --------------------------------------------------------------------------- #
# Límite de acciones por minuto
# --------------------------------------------------------------------------- #

MAX_ACCIONES = 20
VENTANA_S = 60.0


class Limitador:
    """Ventana deslizante: como máximo ``maximo`` acciones en ``ventana_s`` segundos.

    Uso: ``if limitador.permitir("calificar"): ... else: st.warning(limitador.aviso())``.
    Una acción rechazada no se cuenta (no alarga el bloqueo).
    """

    def __init__(self, maximo: int = MAX_ACCIONES, ventana_s: float = VENTANA_S,
                 reloj: Callable[[], float] = time.monotonic):
        self.maximo = int(maximo)
        self.ventana_s = float(ventana_s)
        self.reloj = reloj
        self._marcas: deque[float] = deque()

    def _limpiar(self, ahora: float) -> None:
        while self._marcas and ahora - self._marcas[0] >= self.ventana_s:
            self._marcas.popleft()

    def permitir(self, accion: str = "") -> bool:
        """Registra la acción y devuelve True si cabe en la ventana; si no, False."""
        ahora = self.reloj()
        self._limpiar(ahora)
        if len(self._marcas) >= self.maximo:
            return False
        self._marcas.append(ahora)
        return True

    def restantes(self) -> int:
        self._limpiar(self.reloj())
        return self.maximo - len(self._marcas)

    def espera_s(self) -> float:
        """Segundos hasta que se libere un cupo (0 si hay cupo)."""
        ahora = self.reloj()
        self._limpiar(ahora)
        if len(self._marcas) < self.maximo:
            return 0.0
        return max(0.0, self.ventana_s - (ahora - self._marcas[0]))

    def aviso(self) -> str:
        espera = int(round(self.espera_s())) or 1
        return (f"Llegaste al límite de {self.maximo} acciones por minuto. "
                f"Espera unos {espera} s e inténtalo de nuevo; esta acción no se registró.")
