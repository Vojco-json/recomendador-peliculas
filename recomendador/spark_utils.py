"""Fábrica de SparkSession para el proyecto.

- Spark local con ``local[nucleos]`` y ``spark.driver.memory`` (6g por defecto, máximo 6g).
- Particiones de shuffle proporcionales a los núcleos (2 por núcleo).
- Caché Ivy dentro del proyecto (``.ivy2``): tras la primera descarga del conector
  Kafka todo funciona sin internet (importante el día de la expo).
- Con ``kafka=True`` agrega ``org.apache.spark:spark-sql-kafka-0-10_2.12`` con la
  MISMA versión que ``pyspark.__version__``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from . import config


def version_pyspark() -> str:
    """Versión de PySpark instalada (por ejemplo '3.5.5')."""
    import pyspark

    return pyspark.__version__


def conector_kafka(version: str | None = None) -> str:
    """Coordenada Maven del conector Kafka para la versión de PySpark instalada."""
    return f"org.apache.spark:spark-sql-kafka-0-10_2.12:{version or version_pyspark()}"


def conector_en_cache(version: str | None = None) -> Path | None:
    """Ruta del jar del conector en la caché Ivy del proyecto, o None si no está."""
    version = version or version_pyspark()
    nombre = f"org.apache.spark_spark-sql-kafka-0-10_2.12-{version}.jar"
    candidato = config.IVY_DIR / "jars" / nombre
    return candidato if candidato.exists() else None


def jars_conector_en_cache(version: str | None = None) -> list[Path] | None:
    """Jars del conector Kafka y sus dependencias ya descargados en ``.ivy2/jars``.

    Devuelve None si falta el conector o su ``spark-token-provider`` de la misma
    versión. Se excluyen los jars de otras versiones del conector (si alguna vez
    se cambió la versión de PySpark) para no mezclar clases.
    """
    version = version or version_pyspark()
    carpeta = config.IVY_DIR / "jars"
    propios = (f"org.apache.spark_spark-sql-kafka-0-10_2.12-{version}.jar",
               f"org.apache.spark_spark-token-provider-kafka-0-10_2.12-{version}.jar")
    if not carpeta.is_dir() or not all((carpeta / n).exists() for n in propios):
        return None
    jars = []
    for jar in sorted(carpeta.glob("*.jar")):
        es_conector = jar.name.startswith(("org.apache.spark_spark-sql-kafka-0-10",
                                           "org.apache.spark_spark-token-provider-kafka-0-10"))
        if es_conector and jar.name not in propios:
            continue
        jars.append(jar)
    return jars


def _preparar_entorno_windows() -> None:
    """Ajustes del proceso para que Spark funcione bien en Windows.

    - PYSPARK_PYTHON apunta al Python del .venv (evita 'Python worker failed to connect back').
    - Si existe HADOOP_HOME, agrega %HADOOP_HOME%\\bin al PATH para que se cargue hadoop.dll.
    """
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    os.environ.setdefault("PYSPARK_DRIVER_PYTHON", sys.executable)
    hadoop = os.environ.get("HADOOP_HOME")
    if hadoop:
        bin_dir = str(Path(hadoop) / "bin")
        if bin_dir.lower() not in os.environ.get("PATH", "").lower():
            os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")


def opciones_spark(nucleos: int | None = None, kafka: bool = False,
                   memoria: str | None = None, usar_cache: bool = True) -> dict[str, str]:
    """Configuración que usa crear_spark (separada para poder probarla sin JVM).

    Con ``kafka=True``:
    - si el conector y sus dependencias ya están en ``.ivy2/jars`` (y ``usar_cache``),
      se pasan como ``spark.jars`` (rutas locales). Así el arranque NO ejecuta la
      resolución Ivy, que consulta Maven Central y podía quedarse esperando la red
      varios minutos de forma intermitente (causa de las pruebas colgadas);
    - si no están, se usa ``spark.jars.packages`` con la coordenada Maven de la misma
      versión que PySpark, que los descarga a ``.ivy2`` (solo la primera vez).
    """
    n = int(nucleos or config.NUCLEOS)
    if n < 1:
        raise ValueError("nucleos debe ser 1 o más")
    memoria = (memoria or config.SPARK_MEMORIA).lower()
    if config.memoria_a_mb(memoria) > config.MEMORIA_MAXIMA_MB:
        raise ValueError(f"memoria {memoria} supera el máximo permitido de 6g")
    particiones = str(max(2, 2 * n))
    opciones = {
        "spark.master": f"local[{n}]",
        "spark.driver.memory": memoria,
        "spark.sql.shuffle.partitions": particiones,
        "spark.default.parallelism": particiones,
        "spark.jars.ivy": config.IVY_DIR.as_posix(),
        "spark.sql.session.timeZone": "UTC",
        "spark.driver.host": "127.0.0.1",
        "spark.driver.bindAddress": "127.0.0.1",
        "spark.ui.showConsoleProgress": "false",
        "spark.sql.adaptive.enabled": "true",
    }
    if kafka:
        locales = jars_conector_en_cache() if usar_cache else None
        if locales:
            opciones["spark.jars"] = ",".join(j.resolve().as_uri() for j in locales)
        else:
            opciones["spark.jars.packages"] = conector_kafka()
    return opciones


def crear_spark(nucleos: int | None = None, kafka: bool = False, memoria: str | None = None,
                app: str = "recomendador-peliculas", extra: dict | None = None):
    """Crea (o reutiliza) la SparkSession del proceso.

    Parámetros
    ----------
    nucleos: núcleos de ``local[N]``; por defecto ``config.NUCLEOS``.
    kafka:   si es True agrega el conector spark-sql-kafka de la misma versión que PySpark.
             La primera vez lo descarga de Maven a ``.ivy2``; después funciona sin internet.
    memoria: memoria del driver (por defecto ``config.SPARK_MEMORIA``; máximo 6g).
    extra:   opciones adicionales de Spark.

    Nota: si ya hay una SparkSession activa en el proceso, Spark la reutiliza y las
    opciones de JVM (memoria, paquetes) no cambian. Llama a ``detener_spark()`` antes
    si necesitas otra configuración.
    """
    from pyspark.sql import SparkSession

    _preparar_entorno_windows()
    _silenciar_taskkill_pyspark()
    config.IVY_DIR.mkdir(parents=True, exist_ok=True)

    builder = SparkSession.builder.appName(app)
    for clave, valor in {**opciones_spark(nucleos, kafka, memoria), **(extra or {})}.items():
        builder = builder.config(clave, valor)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


def _es_taskkill(args) -> bool:
    """True si la orden es un ``taskkill`` (directo o vía ``cmd /c``)."""
    if isinstance(args, (list, tuple)):
        return any(str(a).lower() == "taskkill" for a in args[:3])
    return "taskkill" in str(args).lower()


def _clase_popen_silenciosa():
    """Subclase de ``subprocess.Popen`` que descarta la salida de ``taskkill``.

    Al salir del intérprete, pyspark (``java_gateway.killChild``, registrado con atexit)
    ejecuta ``cmd /c taskkill /f /t /pid <pid>`` sobre la JVM. Si la JVM ya terminó
    (p. ej. porque ``detener_spark`` la cerró), taskkill imprime en la consola
    "ERROR: no se encontró el proceso"; es inofensivo pero ensucia los registros.
    Esta clase redirige stdout/stderr de taskkill a DEVNULL; el resto de los procesos
    (la JVM incluida) se lanzan sin cambios.
    """
    import subprocess

    class PopenSilencioso(subprocess.Popen):
        def __init__(self, args, *resto, **kwargs):
            if _es_taskkill(args):
                kwargs["stdout"] = subprocess.DEVNULL
                kwargs["stderr"] = subprocess.DEVNULL
            super().__init__(args, *resto, **kwargs)

    return PopenSilencioso


_POPEN_SILENCIOSO = None


def _silenciar_taskkill_pyspark() -> None:
    """Instala (una vez) la clase silenciosa como ``Popen`` de ``pyspark.java_gateway``."""
    global _POPEN_SILENCIOSO
    if os.name != "nt":
        return
    from pyspark import java_gateway

    if _POPEN_SILENCIOSO is None:
        _POPEN_SILENCIOSO = _clase_popen_silenciosa()
    if not issubclass(java_gateway.Popen, _POPEN_SILENCIOSO):
        java_gateway.Popen = _POPEN_SILENCIOSO


def _popen_vigilado():
    """Subclase de ``subprocess.Popen`` para lanzar la JVM de Spark de forma vigilada.

    Causa raíz de los cuelgues intermitentes al crear la SparkSession en Windows:
    pyspark (``java_gateway.launch_gateway``) espera el puerto de la JVM con
    ``while not proc.poll() and not os.path.isfile(conn_info_file)``. En Windows el
    proceso hijo es ``spark-submit.cmd``; si ese .cmd termina con código 0 sin haber
    escrito el archivo del puerto (``not 0`` es True), el bucle no termina nunca.

    Esta subclase hace que ``poll()`` informe 1 en ese caso (o cuando el vigilante
    abortó el arranque), así pyspark lanza ``JAVA_GATEWAY_EXITED`` en vez de colgarse
    y ``crear_spark_vigilado`` puede reintentar.
    """
    import subprocess

    # Hereda de la clase silenciosa (si está instalada) para no perder ese efecto.
    base = _POPEN_SILENCIOSO or subprocess.Popen

    class PopenVigilado(base):
        lanzados: list = []   # procesos creados durante el arranque en curso

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.abortado = False
            self.arrancando = True
            PopenVigilado.lanzados.append(self)

        def poll(self):
            codigo = super().poll()
            if self.abortado or (self.arrancando and codigo == 0):
                return 1
            return codigo

    return PopenVigilado


def _matar_arbol(proc) -> None:
    """Termina el proceso y sus hijos (en Windows el hijo directo es un .cmd, no la JVM)."""
    import subprocess

    try:
        if proc.stdin:
            proc.stdin.close()   # la JVM de pyspark termina al ver EOF en su stdin
    except OSError:
        pass
    if os.name == "nt":
        subprocess.run(["taskkill", "/f", "/t", "/pid", str(proc.pid)],
                       capture_output=True, check=False)
    try:
        proc.kill()
    except OSError:
        pass


def _reiniciar_estado_pyspark() -> None:
    """Olvida un gateway/contexto a medio crear para poder lanzar otra JVM."""
    from pyspark import SparkContext
    from pyspark.sql import SparkSession

    SparkContext._gateway = None
    SparkContext._jvm = None
    SparkContext._active_spark_context = None
    SparkSession._instantiatedSession = None
    SparkSession._activeSession = None


def crear_spark_vigilado(plazo_s: float = 60, intentos: int = 3, registro=None, **kwargs):
    """Como ``crear_spark``, pero si el arranque no termina a tiempo lo aborta (matando
    la JVM a medio lanzar) y reintenta, hasta ``intentos`` veces.

    El plazo crece con cada intento (``plazo_s``, ``2*plazo_s``, ...): un cuelgue se
    detecta pronto (un arranque normal tarda ~10 s) y, si la máquina está fría y lenta,
    los intentos siguientes tienen más margen.

    ``registro`` es un archivo de texto opcional donde se anota cada intento fallido
    (código del proceso lanzado y pilas de los hilos de Python) como evidencia.
    """
    import faulthandler
    import threading
    import time

    from pyspark import java_gateway

    _silenciar_taskkill_pyspark()   # así la clase restaurada al final también es silenciosa
    popen_original = java_gateway.Popen
    clase = _popen_vigilado()
    ultimo_error: BaseException | None = None
    for intento in range(1, intentos + 1):
        clase.lanzados = []
        resultado: dict = {}

        def _arrancar():
            try:
                resultado["spark"] = crear_spark(**kwargs)
            except BaseException as exc:  # se reporta desde el hilo principal
                resultado["error"] = exc

        java_gateway.Popen = clase
        try:
            inicio = time.monotonic()
            hilo = threading.Thread(target=_arrancar, name=f"arranque-spark-{intento}",
                                    daemon=True)
            hilo.start()
            hilo.join(plazo_s * intento)
            colgado = hilo.is_alive()
            if colgado:
                if registro is not None:
                    codigos = [p.returncode if p.returncode is not None else "vivo"
                               for p in clase.lanzados]
                    registro.write(f"[arranque Spark] intento {intento}: sin terminar tras "
                                   f"{time.monotonic() - inicio:.0f}s; procesos lanzados: "
                                   f"{codigos}. Pilas:\n")
                    faulthandler.dump_traceback(file=registro, all_threads=True)
                    registro.flush()
                for proc in clase.lanzados:
                    proc.abortado = True
                    _matar_arbol(proc)
                hilo.join(20)
                if hilo.is_alive():
                    raise RuntimeError("El arranque de Spark quedó colgado y no se pudo abortar")
                ultimo_error = TimeoutError(f"Spark no arrancó en {plazo_s * intento:.0f}s")
        finally:
            java_gateway.Popen = popen_original
            for proc in clase.lanzados:
                proc.arrancando = False

        if not colgado and "spark" in resultado:
            return resultado["spark"]
        if not colgado:
            ultimo_error = resultado.get("error")
            # Solo se reintenta si la JVM murió al arrancar; otros errores (p. ej. de
            # configuración o del conector) se propagan tal cual.
            if "JAVA_GATEWAY_EXITED" not in repr(ultimo_error):
                raise ultimo_error
            if registro is not None:
                registro.write(f"[arranque Spark] intento {intento}: error {ultimo_error!r}\n")
                registro.flush()
            for proc in clase.lanzados:
                _matar_arbol(proc)
        _reiniciar_estado_pyspark()
    raise RuntimeError(f"Spark no arrancó tras {intentos} intentos") from ultimo_error


PLAZO_DETENER_S = float(os.environ.get("SPARK_PLAZO_DETENER", "30"))


def _pid_jvm(spark) -> int | None:
    """PID del proceso Java de la sesión (se pregunta a la propia JVM)."""
    try:
        return int(spark.sparkContext._jvm.java.lang.ProcessHandle.current().pid())
    except Exception:  # noqa: BLE001 - sin JVM viva no hay PID que obtener
        return None


def _matar_pid(pid: int) -> None:
    """Mata un proceso por PID (con su árbol en Windows)."""
    import signal
    import subprocess

    if os.name == "nt":
        subprocess.run(["taskkill", "/f", "/t", "/pid", str(pid)],
                       capture_output=True, check=False)
    else:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def detener_spark(spark=None, plazo_s: float | None = None) -> bool:
    """Detiene la SparkSession con plazo (por defecto 30 s, env SPARK_PLAZO_DETENER).

    En Windows ``spark.stop()`` a veces se cuelga al apagar la JVM. Aquí el stop corre
    en un hilo; si no termina dentro del plazo se mata el proceso Java de ESA sesión
    (su PID se obtiene antes de detener) y se limpia el estado de pyspark para que el
    script pueda terminar. Devuelve True si se detuvo a tiempo, False si hubo que matar.
    """
    import threading

    from pyspark.sql import SparkSession

    spark = spark or SparkSession.getActiveSession()
    if spark is None:
        return True
    plazo = PLAZO_DETENER_S if plazo_s is None else float(plazo_s)
    pid = _pid_jvm(spark)
    gateway = getattr(spark.sparkContext, "_gateway", None)
    proc = getattr(gateway, "proc", None)

    hilo = threading.Thread(target=spark.stop, name="detener-spark", daemon=True)
    hilo.start()
    hilo.join(plazo)
    if not hilo.is_alive():
        return True
    print(f"Aviso: spark.stop() no terminó en {plazo:.0f} s; se mata la JVM "
          f"(pid {pid}).", file=sys.stderr, flush=True)
    if pid is not None:
        _matar_pid(pid)
    if proc is not None:
        _matar_arbol(proc)
    _reiniciar_estado_pyspark()
    return False


def precalentar_conector() -> Path | None:
    """Descarga el conector Kafka a la caché .ivy2 (requiere internet la primera vez).

    Devuelve la ruta del jar si quedó en caché.
    """
    if conector_en_cache():
        return conector_en_cache()
    spark = crear_spark(nucleos=1, kafka=True, memoria="1g", app="precalentar-conector")
    spark.stop()
    return conector_en_cache()
