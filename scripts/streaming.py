"""Streaming en vivo: Kafka (o archivos) -> Spark Structured Streaming -> estado.db.

Ejemplos (desde la raíz del proyecto, con el broker corriendo):

    # Modo completo, micro-lotes cada 5 s, reentrenamiento cada 30 min
    .venv/Scripts/python scripts/streaming.py --fuente kafka --topic ratings
    # Modo demo (antes: scripts/preparar_demo.py), reentrena cada 5 min
    .venv/Scripts/python scripts/streaming.py --modo demo --reentrenar-cada-min 5
    # Procesar lo que ya está en el topic y terminar
    .venv/Scripts/python scripts/streaming.py --availableNow --starting-offsets earliest
    # Sin Kafka: carpeta con archivos JSON (una línea por evento)
    .venv/Scripts/python scripts/streaming.py --fuente archivo --ruta entrada/ --datos muestras/ml-mini \
        --artefactos artefactos/mini --corte 2016-01-01

Ctrl+C detiene el streaming y el hilo de reentrenamiento. El checkpoint queda en
<artefactos>/checkpoints/<topic>: al relanzar, continúa sin duplicar lotes.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402


def construir_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--fuente", choices=["kafka", "archivo"], default="kafka")
    p.add_argument("--topic", default=config.TOPIC)
    p.add_argument("--bootstrap", default=config.KAFKA_BOOTSTRAP)
    p.add_argument("--ruta", help="carpeta de entrada para --fuente archivo")
    p.add_argument("--formato", choices=["json", "csv"], default="json",
                   help="formato de los archivos de --fuente archivo")
    p.add_argument("--modo", choices=list(config.MODOS_VALIDOS), default=config.MODO)
    p.add_argument("--artefactos", help="por defecto ARTEFACTOS_BASE (completo) o "
                                        "ARTEFACTOS_DEMO (demo)")
    p.add_argument("--estado", default=str(config.ESTADO_DB), help="ruta de estado.db")
    p.add_argument("--datos", default=str(config.DATOS),
                   help="MovieLens: historia de los usuarios y géneros de las películas")
    p.add_argument("--corte", default=config.FECHA_CORTE,
                   help="la historia son los ratings anteriores a esta fecha")
    p.add_argument("--trigger", type=float, default=5, help="segundos entre micro-lotes")
    p.add_argument("--reentrenar-cada-min", type=float, default=30,
                   help="minutos entre reentrenamientos de ALS (0 = nunca)")
    p.add_argument("--availableNow", action="store_true",
                   help="procesa lo disponible y termina (Trigger.availableNow)")
    p.add_argument("--starting-offsets", choices=["latest", "earliest"], default="latest")
    p.add_argument("--nucleos", type=int, default=config.NUCLEOS)
    return p


def main(argv: list[str] | None = None) -> int:
    a = construir_parser().parse_args(argv)
    from recomendador import kafka_utils, spark_utils, streaming

    if a.fuente == "kafka" and not kafka_utils.kafka_disponible(bootstrap=a.bootstrap):
        print(f"Kafka no disponible en {a.bootstrap}", file=sys.stderr)
        return 2
    if a.fuente == "archivo" and not a.ruta:
        print("Error: --fuente archivo necesita --ruta", file=sys.stderr)
        return 2
    artefactos = Path(a.artefactos) if a.artefactos else (
        config.ARTEFACTOS_DEMO if a.modo == "demo" else config.ARTEFACTOS_BASE)

    spark = spark_utils.crear_spark(nucleos=a.nucleos, kafka=a.fuente == "kafka",
                                    app=f"streaming-{a.modo}")
    nombre = a.topic if a.fuente == "kafka" else "archivo"
    ctx = streaming.crear_contexto(spark, artefactos, a.estado, a.modo, consulta=nombre,
                                   datos=a.datos, corte=a.corte)
    hilo = None
    if a.reentrenar_cada_min > 0 and not a.availableNow:
        hilo = streaming.Reentrenador(ctx, a.reentrenar_cada_min * 60)
        hilo.start()
    print(f"Streaming {a.fuente} ({nombre}) modo {a.modo} -> {a.estado}; "
          f"checkpoint {streaming.ruta_checkpoint(artefactos, nombre)}")
    consulta = streaming.iniciar_streaming(
        ctx, a.fuente, a.topic, a.bootstrap, a.ruta, a.trigger, a.availableNow,
        a.starting_offsets, formato=a.formato)
    try:
        consulta.awaitTermination()
    except KeyboardInterrupt:
        print("Deteniendo...")
        consulta.stop()
    finally:
        if hilo:
            hilo.detener()
        ctx.cerrar()
        from recomendador.spark_utils import detener_spark
        detener_spark(spark)   # con plazo: no se cuelga al apagar la JVM
    return 0


if __name__ == "__main__":
    sys.exit(main())
