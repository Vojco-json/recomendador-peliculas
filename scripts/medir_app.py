"""Mide los tiempos de la app MoviePredict con AppTest y los artefactos REALES de 25M.

Lo corre el usuario (antes y después de los cambios de rendimiento):

    .venv/Scripts/python -m scripts.medir_app --etiqueta antes
    .venv/Scripts/python -m scripts.medir_app --etiqueta despues

Qué mide (cada paso es una recarga de AppTest, como un clic en el navegador):
  1. Primera carga en frío (página de login, arranca el calentamiento).
  2. Registro de un usuario de prueba e importación de muestras/letterboxd_mini.csv
     (10 calificaciones).
  3. Primera carga después del calentamiento: una sesión NUEVA entra con ese usuario.
  4. Interacciones con la caché caliente: cambiar a Buscar, buscar, abrir una ficha,
     calificar, Explorar, mensaje de chat (modo reglas) y volver a Inicio.

Cada paso se perfila con cProfile. El informe (tabla de tiempos + las funciones más
costosas de cada paso) se ANEXA a artefactos/medicion_app.txt con la etiqueta dada,
para comparar antes/después. La cuenta y los eventos van a una carpeta temporal: no
se toca estado.db ni cuentas.db reales, y Kafka se reemplaza por el respaldo directo.

Metas (requisito 97): cada interacción < 1 s con la caché caliente y la primera carga
< 5 s después del calentamiento.
"""

from __future__ import annotations

import argparse
import cProfile
import io
import os
import pstats
import sys
import tempfile
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

META_INTERACCION_S = 1.0
META_PRIMERA_CARGA_S = 5.0


def _boton(at, etiqueta: str, prefijo_clave: str | None = None):
    for b in at.button:
        if b.label == etiqueta and (prefijo_clave is None or str(b.key).startswith(prefijo_clave)):
            return b
    raise RuntimeError(f"No se encontró el botón «{etiqueta}»")


def _ir_a(at, seccion: str):
    radio = at.radio(key="seccion")
    if radio.value != seccion:
        radio.set_value(seccion)
    return at


class Medidor:
    """Ejecuta pasos de AppTest con cProfile y guarda (nombre, segundos, perfil, tipo)."""

    def __init__(self, top: int):
        self.top = top
        self.filas: list[tuple[str, float, str, str]] = []

    def paso(self, nombre: str, tipo: str, accion) -> None:
        perfil = cProfile.Profile()
        t0 = time.perf_counter()
        perfil.enable()
        try:
            at = accion()
        finally:
            perfil.disable()
        dt = time.perf_counter() - t0
        if at is not None and at.exception:
            raise RuntimeError(f"{nombre}: la app lanzó una excepción: {at.exception}")
        buf = io.StringIO()
        pstats.Stats(perfil, stream=buf).sort_stats("cumulative").print_stats(self.top)
        self.filas.append((nombre, dt, buf.getvalue(), tipo))
        print(f"  {nombre:<45} {dt:7.2f} s")

    def informe(self, etiqueta: str, entorno: str) -> str:
        lineas = [f"===== Medición «{etiqueta}» {time.strftime('%Y-%m-%d %H:%M:%S')} =====",
                  entorno, "", f"{'Paso':<45} {'Tiempo (s)':>10}  Meta", "-" * 70]
        for nombre, dt, _, tipo in self.filas:
            meta = {"interaccion": META_INTERACCION_S, "primera": META_PRIMERA_CARGA_S}.get(tipo)
            marca = "" if meta is None else ("OK" if dt < meta else f"SOBRE {meta:g} s")
            lineas.append(f"{nombre:<45} {dt:10.2f}  {marca}")
        inter = [dt for _, dt, _, t in self.filas if t == "interaccion"]
        if inter:
            lineas.append(f"\nInteracciones con caché caliente: máx {max(inter):.2f} s, "
                          f"media {sum(inter) / len(inter):.2f} s")
        lineas.append("\n--- Detalle por función (cProfile, ordenado por tiempo acumulado) ---")
        for nombre, dt, perfil, _ in self.filas:
            lineas += ["", f">>> {nombre} ({dt:.2f} s)", perfil.strip()]
        return "\n".join(lineas) + "\n\n"


def _esperar_calentamiento(limite_s: float) -> float:
    t0 = time.perf_counter()
    for hilo in threading.enumerate():
        if hilo.name == "calentamiento":
            hilo.join(limite_s)
    return time.perf_counter() - t0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--etiqueta", default="medicion", help="p. ej. antes / despues")
    ap.add_argument("--salida", default=str(RAIZ / "artefactos" / "medicion_app.txt"))
    ap.add_argument("--busqueda", default="nolan")
    ap.add_argument("--mensaje", default="drama policial con un twist final")
    ap.add_argument("--top", type=int, default=25, help="funciones por paso en el detalle")
    args = ap.parse_args(argv)

    tmp = Path(tempfile.mkdtemp(prefix="medir_app_"))
    # Entorno de la medición: artefactos reales (los de config), estado y cuentas aparte.
    os.environ.setdefault("MODO", "completo")
    os.environ["ESTADO_DB"] = str(tmp / "estado.db")
    os.environ["LLM_PROVIDER"] = "reglas"            # el chat se mide en modo reglas

    from streamlit.testing.v1 import AppTest

    from recomendador import config, cuentas, eventos

    config.ESTADO_DB = tmp / "estado.db"
    config.LLM_PROVIDER = "reglas"
    cuentas.ruta_defecto = lambda: tmp / "cuentas.db"
    eventos._productor_defecto = lambda *a, **k: None   # sin Kafka: respaldo directo

    app = str(RAIZ / "app" / "app.py")
    entorno = (f"DATOS={config.DATOS}  ARTEFACTOS_BASE={config.ARTEFACTOS_BASE}  "
               f"MODO={config.MODO}  TMDB_API_KEY={'sí' if os.environ.get('TMDB_API_KEY') else 'no'}")
    print(entorno)
    if not (Path(config.ARTEFACTOS_BASE) / "app" / "peliculas.parquet").exists():
        print("Faltan los artefactos de la app (scripts/precalcular_app.py).")
        return 1

    m = Medidor(args.top)
    nombre, clave = f"medir_{int(time.time())}", "secreta123"
    at = AppTest.from_file(app, default_timeout=600)
    m.paso("1. Primera carga en frío (login)", "info", lambda: at.run())

    def registrar():
        at.text_input(key="reg_nombre").input(nombre)
        at.text_input(key="reg_clave").input(clave)
        at.text_input(key="reg_clave2").input(clave)
        _boton(at, "Crear cuenta").click().run()
        return _boton(at, "Probar con un CSV de ejemplo").click().run()

    m.paso("2. Registro + importar 10 calificaciones", "info", registrar)
    print(f"  (esperando el calentamiento: {_esperar_calentamiento(600):.1f} s)")

    # 3. Sesión nueva con el mismo usuario: primera carga tras el calentamiento.
    at2 = AppTest.from_file(app, default_timeout=600)

    def primera_carga():
        at2.run()
        at2.text_input(key="login_nombre").input(nombre)
        at2.text_input(key="login_clave").input(clave)
        return _boton(at2, "Entrar").click().run()

    m.paso("3. Primera carga tras calentamiento (login+Inicio)", "primera", primera_carga)
    m.paso("4. Recargar Inicio (caché caliente)", "interaccion", lambda: at2.run())
    m.paso("5. Cambiar a Buscar", "interaccion", lambda: _ir_a(at2, "Buscar").run())
    m.paso(f"6. Buscar «{args.busqueda}»", "interaccion",
           lambda: at2.text_input(key="buscar_q").input(args.busqueda).run())

    ver = [b for b in at2.button if b.key and str(b.key).startswith("ver_")]
    if ver:
        movie_id = int(str(ver[0].key).split("_")[1])
        m.paso("7. Abrir una ficha", "interaccion", lambda: ver[0].click().run())

        def calificar():
            at2.selectbox(key=f"bus_nota_{movie_id}").set_value(4.0)
            return _boton(at2, "Guardar calificación").click().run()

        m.paso("8. Calificar (invalida la caché del usuario)", "interaccion", calificar)
    else:
        print(f"  La búsqueda «{args.busqueda}» no trajo resultados: se omite ficha/calificar.")
    m.paso("9. Cambiar a Explorar", "interaccion", lambda: _ir_a(at2, "Explorar").run())
    siguiente = [b for b in at2.button if "iguiente" in str(b.label)]
    if siguiente:
        m.paso("10. Explorar: siguiente", "interaccion", lambda: siguiente[0].click().run())
    m.paso("11. Cambiar a Chat", "interaccion", lambda: _ir_a(at2, "Chat").run())
    m.paso("12. Mensaje de chat (modo reglas)", "interaccion",
           lambda: at2.chat_input[0].set_value(args.mensaje).run())
    m.paso("13. Volver a Inicio", "interaccion", lambda: _ir_a(at2, "Inicio").run())

    texto = m.informe(args.etiqueta, entorno)
    salida = Path(args.salida)
    salida.parent.mkdir(parents=True, exist_ok=True)
    with open(salida, "a", encoding="utf-8") as f:
        f.write(texto)
    print("\n" + texto.split("--- Detalle")[0])
    print(f"Informe anexado a {salida}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
