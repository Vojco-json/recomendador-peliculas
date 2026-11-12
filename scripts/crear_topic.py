"""Crea el topic Kafka de calificaciones (por defecto 'ratings', 6 particiones, RF 1).

Es idempotente: si el topic ya existe, lo avisa y termina bien (código 0).
Si Kafka no responde, muestra un mensaje claro y termina con código 2 (sin traceback).

    .venv/Scripts/python scripts/crear_topic.py
    .venv/Scripts/python scripts/crear_topic.py --topic ratings_prueba --particiones 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

from recomendador import config  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Crea el topic Kafka de ratings si no existe.")
    p.add_argument("--topic", default=config.TOPIC, help="nombre del topic (%(default)s)")
    p.add_argument("--particiones", type=int, default=config.TOPIC_PARTICIONES,
                   help="número de particiones (%(default)s)")
    p.add_argument("--replicacion", type=int, default=1,
                   help="factor de replicación (%(default)s; un solo broker local)")
    p.add_argument("--bootstrap", default=config.KAFKA_BOOTSTRAP,
                   help="broker Kafka (%(default)s)")
    args = p.parse_args(argv)

    try:
        from recomendador import kafka_utils

        if not kafka_utils.kafka_disponible(timeout=3, bootstrap=args.bootstrap):
            print(f"ERROR: Kafka no responde en {args.bootstrap}. Levanta el broker "
                  "o indica otro con --bootstrap / KAFKA_BOOTSTRAP.", file=sys.stderr)
            return 2
        creado = kafka_utils.crear_topic(args.topic, particiones=args.particiones,
                                         replicacion=args.replicacion,
                                         bootstrap=args.bootstrap)
    except ImportError as exc:
        print(f"ERROR: falta una dependencia ({exc}). Ejecuta setup_entorno.ps1.",
              file=sys.stderr)
        return 1
    except Exception as exc:  # errores del broker (permisos, parámetros inválidos...)
        print(f"ERROR: no se pudo crear el topic '{args.topic}': {exc}", file=sys.stderr)
        return 1

    if creado:
        print(f"Topic '{args.topic}' creado ({args.particiones} particiones, "
              f"RF {args.replicacion}) en {args.bootstrap}.")
    else:
        print(f"AVISO: el topic '{args.topic}' ya existía en {args.bootstrap}; no se modificó.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
