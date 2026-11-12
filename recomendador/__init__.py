"""Paquete «recomendador»: recomendación de películas sobre MovieLens con PySpark y Kafka.

Módulos principales (se agregan por paquetes de trabajo):
- config:       configuración central sobrescribible con variables de entorno.
- spark_utils:  fábrica de SparkSession (local[N], 6g, conector Kafka cacheado en .ivy2).
- datos:        carga de MovieLens (ml-25m o la muestra ml-mini) con esquemas explícitos.
- kafka_utils:  detección del broker y creación idempotente de topics.
"""

__version__ = "0.1.0"
