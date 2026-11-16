"""Configuración central del proyecto.

Todos los valores se pueden sobrescribir con variables de entorno del mismo nombre
(por ejemplo, en PowerShell: ``$env:MODO = "demo"``). Los valores se leen al importar
el módulo; si cambias una variable dentro de un proceso ya iniciado, usa
``importlib.reload(config)``.

Variables reconocidas (con su valor por defecto):

=================  =========================================  ==============================
Variable           Por defecto                                Uso
=================  =========================================  ==============================
MODO               completo                                   'completo' o 'demo'
DEMO_DESDE         2015-01-01                                 inicio del subconjunto demo
FECHA_CORTE        2018-01-01                                 corte temporal train/test (25M)
NUCLEOS            6                                          núcleos de Spark local[N]
SPARK_MEMORIA      6g                                         spark.driver.memory (máx. 6g)
KAFKA_BOOTSTRAP    localhost:9092                             broker Kafka
TOPIC              ratings                                    topic de calificaciones
DATOS              <proyecto>/data/ml-25m                     carpeta de MovieLens
ARTEFACTOS         <proyecto>/artefactos                      raíz de artefactos
ARTEFACTOS_BASE    <ARTEFACTOS>/25m                           modelo y perfiles de 25M
ARTEFACTOS_DEMO    <ARTEFACTOS>/demo                          artefactos del modo demo
ESTADO_DB          <ARTEFACTOS>/estado.db                     estado en vivo (SQLite WAL)
LLM_PROVIDER       ollama                                     'ollama' o 'reglas'
LLM_MODEL          qwen3.5:9b                                 modelo de Ollama
OLLAMA_HOST        http://localhost:11434                     servidor Ollama
LLM_THINK          false                                      razonamiento del LLM
TMDB_API_KEY       (vacío)                                    clave de TMDb (opcional)
=================  =========================================  ==============================
"""

from __future__ import annotations

import os
import re
import warnings
from datetime import date
from pathlib import Path

# --------------------------------------------------------------------------- #
# Utilidades de lectura de variables de entorno
# --------------------------------------------------------------------------- #

_VERDADEROS = {"1", "true", "t", "si", "sí", "s", "yes", "y", "on"}
_FALSOS = {"0", "false", "f", "no", "n", "off", ""}


def _texto(nombre: str, defecto: str) -> str:
    """Lee una variable de entorno como texto (sin espacios a los lados)."""
    valor = os.environ.get(nombre)
    return defecto if valor is None or valor.strip() == "" else valor.strip()


def _booleano(nombre: str, defecto: bool) -> bool:
    """Lee una variable booleana aceptando true/false, 1/0, si/no."""
    valor = os.environ.get(nombre)
    if valor is None:
        return defecto
    v = valor.strip().lower()
    if v in _VERDADEROS:
        return True
    if v in _FALSOS:
        return False
    raise ValueError(f"{nombre}={valor!r} no es un booleano válido (usa true o false)")


def _entero(nombre: str, defecto: int) -> int:
    """Lee una variable entera."""
    valor = os.environ.get(nombre)
    if valor is None or valor.strip() == "":
        return defecto
    try:
        return int(valor.strip())
    except ValueError as exc:
        raise ValueError(f"{nombre}={valor!r} no es un entero válido") from exc


def _fecha(nombre: str, defecto: str) -> str:
    """Lee una fecha AAAA-MM-DD y la valida; devuelve el texto normalizado."""
    valor = _texto(nombre, defecto)
    try:
        return date.fromisoformat(valor).isoformat()
    except ValueError as exc:
        raise ValueError(f"{nombre}={valor!r} no es una fecha AAAA-MM-DD válida") from exc


def _ruta(nombre: str, defecto: Path) -> Path:
    """Lee una ruta; las relativas se interpretan respecto de la raíz del proyecto."""
    valor = os.environ.get(nombre)
    if valor is None or valor.strip() == "":
        return defecto
    p = Path(valor.strip()).expanduser()
    return p if p.is_absolute() else (RAIZ / p)


# Límite de memoria exigido por el usuario: Spark nunca usa más de 6 GB.
MEMORIA_MAXIMA_MB = 6 * 1024


def memoria_a_mb(texto: str) -> int:
    """Convierte '6g', '4096m' o '2G' a megabytes."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([gGmM])[bB]?\s*", texto)
    if not m:
        raise ValueError(f"Memoria {texto!r} no válida (usa por ejemplo 6g o 4096m)")
    cantidad, unidad = float(m.group(1)), m.group(2).lower()
    return int(cantidad * 1024) if unidad == "g" else int(cantidad)


def _memoria_spark() -> str:
    """Lee SPARK_MEMORIA y la limita a 6g como máximo."""
    valor = _texto("SPARK_MEMORIA", "6g")
    if memoria_a_mb(valor) > MEMORIA_MAXIMA_MB:
        warnings.warn(f"SPARK_MEMORIA={valor} supera el máximo de 6g; se usa 6g", stacklevel=2)
        return "6g"
    return valor.lower()


# --------------------------------------------------------------------------- #
# Rutas del proyecto
# --------------------------------------------------------------------------- #

RAIZ: Path = Path(__file__).resolve().parent.parent
MUESTRA_MINI: Path = RAIZ / "muestras" / "ml-mini"
IVY_DIR: Path = RAIZ / ".ivy2"  # caché local del conector Kafka (funciona sin internet)

DATOS: Path = _ruta("DATOS", RAIZ / "data" / "ml-25m")
DATOS_IMDB: Path = _ruta("DATOS_IMDB", RAIZ / "data" / "imdb")
ARTEFACTOS: Path = _ruta("ARTEFACTOS", RAIZ / "artefactos")
ARTEFACTOS_BASE: Path = _ruta("ARTEFACTOS_BASE", ARTEFACTOS / "25m")
ARTEFACTOS_DEMO: Path = _ruta("ARTEFACTOS_DEMO", ARTEFACTOS / "demo")
ESTADO_DB: Path = _ruta("ESTADO_DB", ARTEFACTOS / "estado.db")
FIGURAS: Path = RAIZ / "poster" / "figuras"

# --------------------------------------------------------------------------- #
# Modo de ejecución y fechas
# --------------------------------------------------------------------------- #

MODOS_VALIDOS = ("completo", "demo")
MODO: str = _texto("MODO", "completo").lower()
if MODO not in MODOS_VALIDOS:
    raise ValueError(f"MODO={MODO!r} no es válido; usa 'completo' o 'demo'")

# Inicio del subconjunto que se reentrena en vivo en el modo demo.
DEMO_DESDE: str = _fecha("DEMO_DESDE", "2015-01-01")
# Fecha de corte temporal para 25M (entrenar antes, evaluar después).
FECHA_CORTE: str = _fecha("FECHA_CORTE", "2018-01-01")
# Fecha de corte de la muestra mini (valores conocidos en muestras/ml-mini/LEEME.md).
FECHA_CORTE_MINI: str = "2016-01-01"
# Una calificación es «relevante» para las métricas de ranking si es >= 4.
RELEVANTE_MIN: float = 4.0

# --------------------------------------------------------------------------- #
# Spark
# --------------------------------------------------------------------------- #

NUCLEOS: int = _entero("NUCLEOS", 6)
if NUCLEOS < 1:
    raise ValueError(f"NUCLEOS={NUCLEOS} debe ser 1 o más")
SPARK_MEMORIA: str = _memoria_spark()

# --------------------------------------------------------------------------- #
# Kafka
# --------------------------------------------------------------------------- #

KAFKA_BOOTSTRAP: str = _texto("KAFKA_BOOTSTRAP", "localhost:9092")
TOPIC: str = _texto("TOPIC", "ratings")
TOPIC_PARTICIONES: int = _entero("TOPIC_PARTICIONES", 6)

# --------------------------------------------------------------------------- #
# LLM (Ollama local por defecto; razonamiento desactivado por defecto)
# --------------------------------------------------------------------------- #

LLM_PROVIDER: str = _texto("LLM_PROVIDER", "ollama").lower()
LLM_MODEL: str = _texto("LLM_MODEL", "qwen3.5:9b")
OLLAMA_HOST: str = _texto("OLLAMA_HOST", "http://localhost:11434")
LLM_THINK: bool = _booleano("LLM_THINK", False)

# --------------------------------------------------------------------------- #
# Enriquecimiento (opcional)
# --------------------------------------------------------------------------- #

TMDB_API_KEY: str | None = os.environ.get("TMDB_API_KEY") or None

# --------------------------------------------------------------------------- #
# Modo público (expo por Cloudflare Tunnel)
# --------------------------------------------------------------------------- #

# MODO_PUBLICO=1: login obligatorio, límite de acciones por minuto, LLM de a una
# solicitud y sin ningún control de administración en la app.
MODO_PUBLICO: bool = _booleano("MODO_PUBLICO", False)
# URL pública (p. ej. la de cloudflared) a la que apunta el QR del póster.
URL_PUBLICA: str = _texto("URL_PUBLICA", "").strip()


def artefactos_activos() -> Path:
    """Carpeta de artefactos que usan la app y el streaming según el MODO."""
    return ARTEFACTOS_DEMO if MODO == "demo" else ARTEFACTOS_BASE


def resumen() -> dict:
    """Diccionario legible con la configuración efectiva (sin secretos)."""
    return {
        "MODO": MODO,
        "DEMO_DESDE": DEMO_DESDE,
        "FECHA_CORTE": FECHA_CORTE,
        "NUCLEOS": NUCLEOS,
        "SPARK_MEMORIA": SPARK_MEMORIA,
        "KAFKA_BOOTSTRAP": KAFKA_BOOTSTRAP,
        "TOPIC": TOPIC,
        "DATOS": str(DATOS),
        "ARTEFACTOS": str(ARTEFACTOS),
        "ARTEFACTOS_BASE": str(ARTEFACTOS_BASE),
        "ARTEFACTOS_DEMO": str(ARTEFACTOS_DEMO),
        "ESTADO_DB": str(ESTADO_DB),
        "LLM_PROVIDER": LLM_PROVIDER,
        "LLM_MODEL": LLM_MODEL,
        "OLLAMA_HOST": OLLAMA_HOST,
        "LLM_THINK": LLM_THINK,
        "TMDB_API_KEY": "definida" if TMDB_API_KEY else "no definida",
    }
