"""Productor Kafka: reproduce los ratings de MovieLens en orden temporal.

Lee los ratings de una carpeta de MovieLens (ml-25m o muestras/ml-mini) y los
publica uno a uno en un topic Kafka como JSON::

    {"userId": 1, "movieId": 296, "rating": 4.5, "timestamp": 1451606400}

La clave del mensaje es el userId (así los ratings de un mismo usuario caen en la
misma partición y conservan su orden).

Fuente de datos (en este orden de preferencia):
  1. <datos>/ratings_ordenados.parquet (lo genera scripts/ordenar_ratings.py):
     se lee por lotes, sin cargar 25M filas en memoria.
  2. <datos>/ratings.csv: se lee completo, se filtra por fechas y se ordena en Python
     (bien para la muestra o rangos acotados; para 25M completos conviene el parquet).

Ejemplos (PowerShell, desde la raíz del proyecto):

    # Ver 5 eventos sin Kafka
    .venv/Scripts/python scripts/productor_kafka.py --datos muestras/ml-mini --dry-run --limite 5
    # Demo: lo posterior al corte, 20 ratings por segundo
    .venv/Scripts/python scripts/productor_kafka.py --desde 2018-01-01 --velocidad 20
    # Tiempo real acelerado 3600x (1 hora de historia por segundo)
    .venv/Scripts/python scripts/productor_kafka.py --desde 2018-01-01 --acelerar 3600

Uso como biblioteca (lo reutiliza la prueba de integración del streaming)::

    from scripts.productor_kafka import leer_eventos, publicar
    n = publicar(leer_eventos("muestras/ml-mini", desde="2016-01-01"), "ratings_test_x",
                 "localhost:9092")
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from datetime import date
from pathlib import Path
from typing import Callable, Iterable, Iterator

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402
from recomendador.datos import fecha_a_epoch  # noqa: E402

UN_DIA = 86_400
PARQUET_ORDENADO = "ratings_ordenados.parquet"
CAMPOS = ("userId", "movieId", "rating", "timestamp")


class KafkaNoDisponible(RuntimeError):
    """El broker no respondió o no confirmó la entrega de los mensajes."""


# --------------------------------------------------------------------------- #
# Lectura de eventos
# --------------------------------------------------------------------------- #

def _rango_epoch(desde: str | None, hasta: str | None) -> tuple[int | None, int | None]:
    """Convierte las fechas a epoch: desde inclusivo y hasta inclusivo (todo ese día)."""
    ini = fecha_a_epoch(desde) if desde else None
    fin = fecha_a_epoch(hasta) + UN_DIA if hasta else None  # exclusivo
    if ini is not None and fin is not None and ini >= fin:
        raise ValueError(f"--desde {desde} debe ser anterior o igual a --hasta {hasta}")
    return ini, fin


def _evento(user, movie, rating, ts) -> dict:
    """Normaliza los tipos del evento (int, int, float, int)."""
    return {"userId": int(user), "movieId": int(movie),
            "rating": float(rating), "timestamp": int(ts)}


def _eventos_csv(archivo: Path, ini: int | None, fin: int | None) -> list[dict]:
    """Lee ratings.csv, filtra por fechas y ordena por (timestamp, userId, movieId)."""
    filas = []
    with open(archivo, newline="", encoding="utf-8-sig") as f:
        lector = csv.reader(f)
        encabezado = next(lector, None)
        if encabezado is None:
            return []
        idx = [encabezado.index(c) for c in CAMPOS]
        for fila in lector:
            if not fila:
                continue
            ts = int(fila[idx[3]])
            if (ini is not None and ts < ini) or (fin is not None and ts >= fin):
                continue
            filas.append((ts, int(fila[idx[0]]), int(fila[idx[1]]), float(fila[idx[2]])))
    filas.sort()
    return [_evento(u, m, r, ts) for ts, u, m, r in filas]


def _archivos_parquet(ruta: Path) -> list[Path]:
    """Archivos de datos de un parquet (archivo único o carpeta escrita por Spark).

    Spark escribe un orderBy global en part-00000, part-00001... en orden de rango,
    así que leerlos por nombre conserva el orden temporal.
    """
    if ruta.is_file():
        return [ruta]
    return sorted(p for p in ruta.glob("part-*.parquet"))


def _eventos_parquet(ruta: Path, ini: int | None, fin: int | None) -> Iterator[dict]:
    """Recorre el parquet ordenado por lotes (memoria acotada)."""
    import pyarrow.parquet as pq

    for archivo in _archivos_parquet(ruta):
        pf = pq.ParquetFile(archivo)
        for lote in pf.iter_batches(batch_size=50_000, columns=list(CAMPOS)):
            cols = lote.to_pydict()
            for u, m, r, ts in zip(cols["userId"], cols["movieId"],
                                   cols["rating"], cols["timestamp"]):
                if ini is not None and ts < ini:
                    continue
                if fin is not None and ts >= fin:
                    return  # ordenado: lo que sigue también queda fuera
                yield _evento(u, m, r, ts)


def _es_parquet(ruta: Path) -> bool:
    """True si la ruta es un parquet (no una carpeta de MovieLens con CSV).

    Spark escribe el parquet como CARPETA con nombre *.parquet y archivos part-*;
    sin esta detección se buscaría <carpeta>/ratings_ordenados.parquet dentro de él.
    """
    if ruta.suffix.lower() == ".parquet":
        return True
    return ruta.is_dir() and not (ruta / "ratings.csv").exists() \
        and not (ruta / PARQUET_ORDENADO).exists() \
        and any(ruta.glob("part-*.parquet"))


def _parquet_usable(ruta: Path) -> bool:
    if not ruta.exists() or not _archivos_parquet(ruta):
        return False
    try:
        import pyarrow.parquet  # noqa: F401
    except ImportError:
        return False
    return True


def leer_eventos(datos, desde: str | None = None, hasta: str | None = None,
                 limite: int | None = None, fuente: str = "auto") -> Iterator[dict]:
    """Eventos de rating ordenados por timestamp (generador de dicts).

    datos:  carpeta de MovieLens, o directamente un ratings.csv / un .parquet.
    desde:  'AAAA-MM-DD' inclusivo. hasta: 'AAAA-MM-DD' inclusivo (todo ese día).
    limite: máximo de eventos (None = todos).
    fuente: 'auto' (parquet ordenado si existe, si no CSV), 'csv' o 'parquet'.
    """
    ruta = Path(datos)
    if not ruta.exists() and not ruta.is_absolute() and (RAIZ / ruta).exists():
        ruta = RAIZ / ruta  # rutas relativas a la raíz del proyecto
    ini, fin = _rango_epoch(desde, hasta)
    if _es_parquet(ruta):
        # La ruta ES el parquet: archivo suelto o carpeta escrita por Spark
        # (p. ej. .../ratings_ordenados.parquet/part-00000-....parquet).
        parquet, csv_ = ruta, None
    elif ruta.is_dir():
        parquet, csv_ = ruta / PARQUET_ORDENADO, ruta / "ratings.csv"
    elif ruta.suffix.lower() == ".csv":
        parquet, csv_ = None, ruta
    else:
        parquet, csv_ = ruta, None

    if fuente == "parquet" or (fuente == "auto" and parquet is not None
                               and _parquet_usable(parquet)):
        if parquet is None or not parquet.exists():
            raise FileNotFoundError(f"No existe el parquet ordenado: {parquet}")
        fuente_iter: Iterable[dict] = _eventos_parquet(parquet, ini, fin)
    else:
        if csv_ is None or not csv_.exists():
            raise FileNotFoundError(
                f"No encuentro ratings en {ruta} (ni {PARQUET_ORDENADO} ni ratings.csv). "
                "Descarga los datos con scripts/descargar_movielens.py")
        fuente_iter = _eventos_csv(csv_, ini, fin)

    for i, evento in enumerate(fuente_iter):
        if limite is not None and i >= limite:
            return
        yield evento


def a_json(evento: dict) -> str:
    """Serializa un evento con las 4 claves en orden fijo."""
    return json.dumps({c: evento[c] for c in CAMPOS}, separators=(",", ":"))


# --------------------------------------------------------------------------- #
# Ritmo de reproducción
# --------------------------------------------------------------------------- #

def pausa(anterior: dict | None, actual: dict, velocidad: float = 0,
          acelerar: float | None = None) -> float:
    """Segundos de espera antes de emitir `actual`.

    - acelerar X: respeta el tiempo real entre ratings dividido por X.
    - velocidad V: V ratings por segundo constante (0 = sin pausa).
    acelerar tiene prioridad si se indica.
    """
    if acelerar:
        if anterior is None:
            return 0.0
        return max(0, actual["timestamp"] - anterior["timestamp"]) / acelerar
    return 1.0 / velocidad if velocidad and velocidad > 0 else 0.0


def con_ritmo(eventos: Iterable[dict], velocidad: float = 0,
              acelerar: float | None = None, pausa_maxima: float = 5.0) -> Iterator[dict]:
    """Envuelve un iterable de eventos y duerme entre ellos según el ritmo pedido.

    pausa_maxima evita esperas eternas con --acelerar cuando hay huecos de días.
    """
    anterior = None
    proximo = time.perf_counter()
    for evento in eventos:
        espera = min(pausa(anterior, evento, velocidad, acelerar), pausa_maxima)
        if espera > 0:
            proximo += espera
            restante = proximo - time.perf_counter()
            if restante > 0:
                time.sleep(restante)
        else:
            proximo = time.perf_counter()
        yield evento
        anterior = evento


# --------------------------------------------------------------------------- #
# Publicación en Kafka
# --------------------------------------------------------------------------- #

def publicar(eventos: Iterable[dict], topic: str | None = None, bootstrap: str | None = None,
             timeout: float = 30, al_enviar: Callable[[int, dict], None] | None = None) -> int:
    """Publica eventos en Kafka y devuelve cuántos confirmó el broker.

    eventos:   iterable de dicts {userId, movieId, rating, timestamp}.
    topic:     por defecto config.TOPIC ('ratings').
    bootstrap: por defecto config.KAFKA_BOOTSTRAP ('localhost:9092').
    al_enviar: callback opcional (n_enviados, evento) para mostrar progreso.

    Lanza KafkaNoDisponible si el broker no confirma todos los mensajes en `timeout`.
    """
    from confluent_kafka import Producer

    topic = topic or config.TOPIC
    bootstrap = bootstrap or config.KAFKA_BOOTSTRAP
    productor = Producer({
        "bootstrap.servers": bootstrap,
        "acks": "all",
        "linger.ms": 5,
        "message.timeout.ms": int(timeout * 1000),
        "log_level": 0,  # sin ruido de librdkafka si el broker cae
    })
    estado = {"ok": 0, "errores": 0, "ultimo_error": None}

    def _entrega(err, _msg):
        if err is None:
            estado["ok"] += 1
        else:
            estado["errores"] += 1
            estado["ultimo_error"] = err

    enviados = 0
    for evento in eventos:
        valor = a_json(evento).encode("utf-8")
        clave = str(evento["userId"]).encode("utf-8")
        while True:
            try:
                productor.produce(topic, value=valor, key=clave, on_delivery=_entrega)
                break
            except BufferError:  # cola local llena: dejar que salgan mensajes
                productor.poll(0.5)
        enviados += 1
        productor.poll(0)
        if al_enviar:
            al_enviar(enviados, evento)

    pendientes = productor.flush(timeout)
    if pendientes or estado["errores"]:
        raise KafkaNoDisponible(
            f"Kafka confirmó {estado['ok']} de {enviados} mensajes en {bootstrap} "
            f"(pendientes: {pendientes}, último error: {estado['ultimo_error']})")
    return estado["ok"]


# --------------------------------------------------------------------------- #
# Línea de comandos
# --------------------------------------------------------------------------- #

def _fecha_valida(texto: str) -> str:
    try:
        return date.fromisoformat(texto).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f"fecha no válida {texto!r}; usa AAAA-MM-DD")


def _no_negativo(texto: str) -> float:
    valor = float(texto)
    if valor < 0:
        raise argparse.ArgumentTypeError("debe ser 0 o más")
    return valor


def _positivo(texto: str) -> float:
    valor = float(texto)
    if valor <= 0:
        raise argparse.ArgumentTypeError("debe ser mayor que 0")
    return valor


def _entero_positivo(texto: str) -> int:
    valor = int(texto)
    if valor < 1:
        raise argparse.ArgumentTypeError("debe ser 1 o más")
    return valor


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Reproduce los ratings de MovieLens en un topic Kafka en orden temporal.")
    p.add_argument("--datos", default=str(config.DATOS),
                   help="carpeta de MovieLens (por defecto %(default)s)")
    p.add_argument("--topic", default=config.TOPIC, help="topic destino (%(default)s)")
    p.add_argument("--bootstrap", default=config.KAFKA_BOOTSTRAP,
                   help="broker Kafka (%(default)s)")
    p.add_argument("--velocidad", type=_no_negativo, default=10.0,
                   help="ratings por segundo; 0 = sin pausa (%(default)s)")
    p.add_argument("--acelerar", type=_positivo, default=None, metavar="X",
                   help="respeta el tiempo real entre ratings dividido por X "
                        "(tiene prioridad sobre --velocidad)")
    # Por defecto se reproduce solo lo posterior al corte: lo anterior ya está en el modelo.
    p.add_argument("--desde", type=_fecha_valida, default=config.FECHA_CORTE,
                   help="AAAA-MM-DD inclusivo (por defecto FECHA_CORTE = %(default)s)")
    p.add_argument("--hasta", type=_fecha_valida, help="AAAA-MM-DD inclusivo (todo el día)")
    p.add_argument("--limite", type=_entero_positivo, help="máximo de ratings a enviar")
    p.add_argument("--fuente", choices=("auto", "csv", "parquet"), default="auto",
                   help="de dónde leer: auto = parquet ordenado si existe, si no ratings.csv")
    p.add_argument("--dry-run", action="store_true",
                   help="no se conecta a Kafka: imprime los JSON por pantalla sin pausas")
    return p


def main(argv: list[str] | None = None) -> int:
    args = construir_parser().parse_args(argv)
    try:
        eventos = leer_eventos(args.datos, args.desde, args.hasta, args.limite, args.fuente)

        if args.dry_run:
            n = 0
            for evento in eventos:
                print(a_json(evento))
                n += 1
            print(f"[dry-run] {n} evento(s) ordenados por timestamp; no se envió nada a Kafka.",
                  file=sys.stderr)
            return 0

        from recomendador.kafka_utils import kafka_disponible

        if not kafka_disponible(timeout=3, bootstrap=args.bootstrap):
            print(f"ERROR: Kafka no responde en {args.bootstrap}. Levanta el broker "
                  "(por ejemplo con Docker) o indica otro con --bootstrap / KAFKA_BOOTSTRAP.",
                  file=sys.stderr)
            return 2

        ritmo = (f"tiempo real x{args.acelerar:g}" if args.acelerar
                 else f"{args.velocidad:g} ratings/seg" if args.velocidad else "sin pausa")
        print(f"Publicando en '{args.topic}' ({args.bootstrap}) a {ritmo}. Ctrl+C para detener.")

        def progreso(n: int, ev: dict) -> None:
            if n == 1 or n % 100 == 0:
                print(f"  {n:>8} enviados - último: usuario {ev['userId']} "
                      f"película {ev['movieId']} ({ev['rating']})", flush=True)

        n = publicar(con_ritmo(eventos, args.velocidad, args.acelerar),
                     args.topic, args.bootstrap, al_enviar=progreso)
        print(f"Listo: {n} rating(s) confirmados por Kafka en '{args.topic}'.")
        return 0
    except KeyboardInterrupt:
        print("\nDetenido por el usuario.", file=sys.stderr)
        return 130
    except KafkaNoDisponible as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except ImportError as exc:
        print(f"ERROR: falta una dependencia ({exc}). Ejecuta setup_entorno.ps1.",
              file=sys.stderr)
        return 1
    except (FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    # Salida en UTF-8 aunque la capture otro proceso (si no, Windows usa cp1252: 'envi�').
    for _flujo in (sys.stdout, sys.stderr):
        try:
            _flujo.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
