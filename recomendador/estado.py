"""Estado en vivo compartido en SQLite (artefactos/estado.db), en modo WAL.

Lo escribe el streaming (scripts/streaming.py) y lo lee la app. WAL permite que un
proceso escriba mientras otro lee sin bloquearse.

Tablas
------
- eventos(userId, movieId, rating, timestamp, recibido_en)
      clave primaria (userId, movieId, timestamp): un evento repetido no se duplica.
- perfiles(userId, tipo, top_generos, perfil_json, actualizado_en)
      perfil de géneros actualizado de forma incremental (perfil_json = dict completo).
- recomendaciones(userId, movieId, rank, score, actualizado_en)
      top-N vigente por usuario (se reemplaza completo en cada actualización).
- meta(clave, valor, actualizado_en)
      claves: ultimo_reentrenamiento, version_modelo, modo e item_factors (puntero a la
      carpeta de factores vigente; se cambia junto con version_modelo en una transacción).
- lotes(consulta, batch_id, procesado_en)
      micro-lotes ya aplicados: si Spark reintenta un lote tras un reinicio, no se aplica
      dos veces (los perfiles son incrementales y se inflarían).
- resenas(id, uid, userId, movieId, texto, timestamp, oculta, recibido_en)
      reseñas públicas. ``uid`` lo genera quien publica (eventos.publicar_resena): la
      misma reseña llegada por Kafka y por el respaldo directo no se duplica. ``id`` es el
      número corto que usa scripts/moderar_resenas.py; ``oculta=1`` la saca de la vista.

Las marcas de tiempo son segundos epoch (float, UTC).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Iterable

ESQUEMA = """
CREATE TABLE IF NOT EXISTS eventos (
    userId      INTEGER NOT NULL,
    movieId     INTEGER NOT NULL,
    rating      REAL    NOT NULL,
    timestamp   INTEGER NOT NULL,
    recibido_en REAL    NOT NULL,
    PRIMARY KEY (userId, movieId, timestamp)
);
CREATE TABLE IF NOT EXISTS perfiles (
    userId         INTEGER PRIMARY KEY,
    tipo           TEXT,
    top_generos    TEXT,
    perfil_json    TEXT NOT NULL,
    actualizado_en REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recomendaciones (
    userId         INTEGER NOT NULL,
    movieId        INTEGER NOT NULL,
    rank           INTEGER NOT NULL,
    score          REAL    NOT NULL,
    actualizado_en REAL    NOT NULL,
    PRIMARY KEY (userId, rank)
);
CREATE TABLE IF NOT EXISTS meta (
    clave          TEXT PRIMARY KEY,
    valor          TEXT,
    actualizado_en REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lotes (
    consulta     TEXT    NOT NULL,
    batch_id     INTEGER NOT NULL,
    procesado_en REAL    NOT NULL,
    PRIMARY KEY (consulta, batch_id)
);
CREATE TABLE IF NOT EXISTS resenas (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    uid         TEXT    NOT NULL UNIQUE,
    userId      INTEGER NOT NULL,
    movieId     INTEGER NOT NULL,
    texto       TEXT    NOT NULL,
    timestamp   INTEGER NOT NULL,
    oculta      INTEGER NOT NULL DEFAULT 0,
    recibido_en REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_eventos_usuario ON eventos(userId);
CREATE INDEX IF NOT EXISTS ix_resenas_pelicula ON resenas(movieId);
"""


def conectar(ruta) -> sqlite3.Connection:
    """Abre (y crea si hace falta) la base en modo WAL con el esquema completo.

    ``check_same_thread=False`` permite usar la conexión desde el hilo de
    reentrenamiento; cada escritura va en su propia transacción.
    """
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(ruta), timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(ESQUEMA)
    con.commit()
    return con


def modo_journal(con: sqlite3.Connection) -> str:
    return con.execute("PRAGMA journal_mode").fetchone()[0].lower()


# --------------------------------------------------------------------------- #
# Meta
# --------------------------------------------------------------------------- #

def set_meta(con: sqlite3.Connection, **valores) -> None:
    """Escribe varias claves de meta en UNA transacción (cambio atómico)."""
    ahora = time.time()
    with con:
        con.executemany(
            "INSERT INTO meta(clave, valor, actualizado_en) VALUES (?, ?, ?) "
            "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor, "
            "actualizado_en=excluded.actualizado_en",
            [(k, None if v is None else str(v), ahora) for k, v in valores.items()])


def get_meta(con: sqlite3.Connection, clave: str, defecto=None):
    fila = con.execute("SELECT valor FROM meta WHERE clave=?", (clave,)).fetchone()
    return defecto if fila is None else fila[0]


def meta(con: sqlite3.Connection) -> dict:
    return {f["clave"]: f["valor"] for f in con.execute("SELECT clave, valor FROM meta")}


# --------------------------------------------------------------------------- #
# Lotes (idempotencia ante reintentos de Spark)
# --------------------------------------------------------------------------- #

def lote_procesado(con: sqlite3.Connection, consulta: str, batch_id: int) -> bool:
    return con.execute("SELECT 1 FROM lotes WHERE consulta=? AND batch_id=?",
                       (consulta, int(batch_id))).fetchone() is not None


# --------------------------------------------------------------------------- #
# Eventos
# --------------------------------------------------------------------------- #

def guardar_eventos(con: sqlite3.Connection, eventos: Iterable[dict],
                    commit: bool = True) -> list[dict]:
    """Inserta eventos ignorando los repetidos; devuelve solo los realmente nuevos."""
    ahora = time.time()
    nuevos = []
    for e in eventos:
        cur = con.execute(
            "INSERT OR IGNORE INTO eventos(userId, movieId, rating, timestamp, recibido_en) "
            "VALUES (?, ?, ?, ?, ?)",
            (int(e["userId"]), int(e["movieId"]), float(e["rating"]), int(e["timestamp"]),
             ahora))
        if cur.rowcount == 1:
            nuevos.append(e)
    if commit:
        con.commit()
    return nuevos


def eventos_usuario(con: sqlite3.Connection, userId: int) -> list[tuple[int, float]]:
    """[(movieId, rating)] del usuario en orden temporal (si repite película, vale el último)."""
    filas = con.execute("SELECT movieId, rating FROM eventos WHERE userId=? "
                        "ORDER BY timestamp", (int(userId),)).fetchall()
    return [(int(m), float(r)) for m, r in filas]


def todos_los_eventos(con: sqlite3.Connection):
    """Todos los eventos como DataFrame de pandas (para el reentrenamiento)."""
    import pandas as pd

    return pd.read_sql_query("SELECT userId, movieId, rating, timestamp FROM eventos", con)


def contar(con: sqlite3.Connection, tabla: str) -> int:
    if tabla not in {"eventos", "perfiles", "recomendaciones", "meta", "lotes", "resenas"}:
        raise ValueError(tabla)
    return int(con.execute(f"SELECT COUNT(*) FROM {tabla}").fetchone()[0])


# --------------------------------------------------------------------------- #
# Perfiles y recomendaciones
# --------------------------------------------------------------------------- #

def guardar_perfil(con: sqlite3.Connection, perfil: dict, commit: bool = True) -> None:
    con.execute(
        "INSERT INTO perfiles(userId, tipo, top_generos, perfil_json, actualizado_en) "
        "VALUES (?, ?, ?, ?, ?) ON CONFLICT(userId) DO UPDATE SET tipo=excluded.tipo, "
        "top_generos=excluded.top_generos, perfil_json=excluded.perfil_json, "
        "actualizado_en=excluded.actualizado_en",
        (int(perfil["userId"]), perfil.get("tipo"),
         json.dumps(perfil.get("top_generos", []), ensure_ascii=False),
         json.dumps(perfil, ensure_ascii=False), time.time()))
    if commit:
        con.commit()


def leer_perfil(con: sqlite3.Connection, userId: int) -> dict | None:
    fila = con.execute("SELECT perfil_json FROM perfiles WHERE userId=?",
                       (int(userId),)).fetchone()
    return None if fila is None else json.loads(fila[0])


def guardar_recomendaciones(con: sqlite3.Connection, userId: int,
                            recs: list[tuple[int, float]], commit: bool = True) -> None:
    """Reemplaza el top-N del usuario (rank empieza en 1)."""
    ahora = time.time()
    con.execute("DELETE FROM recomendaciones WHERE userId=?", (int(userId),))
    con.executemany(
        "INSERT INTO recomendaciones(userId, movieId, rank, score, actualizado_en) "
        "VALUES (?, ?, ?, ?, ?)",
        [(int(userId), int(m), i, float(s), ahora) for i, (m, s) in enumerate(recs, 1)])
    if commit:
        con.commit()


def leer_recomendaciones(con: sqlite3.Connection, userId: int) -> list[dict]:
    filas = con.execute("SELECT movieId, rank, score, actualizado_en FROM recomendaciones "
                        "WHERE userId=? ORDER BY rank", (int(userId),)).fetchall()
    return [dict(f) for f in filas]


# --------------------------------------------------------------------------- #
# Reseñas
# --------------------------------------------------------------------------- #

COLUMNAS_RESENA = "id, uid, userId, movieId, texto, timestamp, oculta, recibido_en"


def guardar_resenas(con: sqlite3.Connection, resenas: Iterable[dict],
                    commit: bool = True) -> list[dict]:
    """Inserta reseñas ignorando las repetidas (mismo ``uid``); devuelve las nuevas."""
    ahora = time.time()
    nuevas = []
    for r in resenas:
        cur = con.execute(
            "INSERT OR IGNORE INTO resenas(uid, userId, movieId, texto, timestamp, oculta, "
            "recibido_en) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (str(r["uid"]), int(r["userId"]), int(r["movieId"]), str(r["texto"]),
             int(r["timestamp"]), ahora))
        if cur.rowcount == 1:
            nuevas.append(r)
    if commit:
        con.commit()
    return nuevas


def resenas_pelicula(con: sqlite3.Connection, movieId: int, limite: int = 50) -> list[dict]:
    """Reseñas PÚBLICAS de una película (sin las ocultas), de la más nueva a la más vieja."""
    filas = con.execute(
        f"SELECT {COLUMNAS_RESENA} FROM resenas WHERE movieId=? AND oculta=0 "
        "ORDER BY timestamp DESC, id DESC LIMIT ?", (int(movieId), int(limite))).fetchall()
    return [dict(f) for f in filas]


def resenas_usuario(con: sqlite3.Connection, userId: int) -> list[dict]:
    """Reseñas públicas de un usuario (sin las ocultas), de la más nueva a la más vieja."""
    filas = con.execute(
        f"SELECT {COLUMNAS_RESENA} FROM resenas WHERE userId=? AND oculta=0 "
        "ORDER BY timestamp DESC, id DESC", (int(userId),)).fetchall()
    return [dict(f) for f in filas]


def listar_resenas(con: sqlite3.Connection, limite: int = 100,
                   incluir_ocultas: bool = True) -> list[dict]:
    """Todas las reseñas (para el script de moderación), de la más nueva a la más vieja."""
    filtro = "" if incluir_ocultas else "WHERE oculta=0"
    filas = con.execute(f"SELECT {COLUMNAS_RESENA} FROM resenas {filtro} "
                        "ORDER BY id DESC LIMIT ?", (int(limite),)).fetchall()
    return [dict(f) for f in filas]


def marcar_oculta(con: sqlite3.Connection, id_resena: int, oculta: bool = True) -> bool:
    """Oculta (o vuelve a mostrar) una reseña por su id. False si el id no existe."""
    with con:
        cur = con.execute("UPDATE resenas SET oculta=? WHERE id=?",
                          (1 if oculta else 0, int(id_resena)))
    return cur.rowcount == 1


def ultima_actualizacion(con: sqlite3.Connection, userId: int) -> float | None:
    """Marca de tiempo (epoch) de la última actualización del usuario, o None."""
    fila = con.execute(
        "SELECT MAX(t) FROM (SELECT MAX(actualizado_en) t FROM recomendaciones WHERE userId=? "
        "UNION ALL SELECT actualizado_en FROM perfiles WHERE userId=?)",
        (int(userId), int(userId))).fetchone()
    return None if fila is None or fila[0] is None else float(fila[0])
