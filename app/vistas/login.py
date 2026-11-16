"""Pantalla de entrada: iniciar sesión o crear una cuenta (bcrypt + SQLite)."""

from __future__ import annotations

import streamlit as st

from recomendador import cuentas

from .comun import CLAVE_USUARIO, Contexto


def _entrar(userId: int, nombre: str) -> None:
    st.session_state[CLAVE_USUARIO] = {"userId": int(userId), "nombre": nombre}
    st.rerun()


def mostrar(ctx: Contexto) -> None:
    """Formularios de login y registro. Solo esto se ve sin sesión."""
    st.markdown('<div class="cr-bienvenida"><h2>Bienvenido a CineRuta</h2>'
                'Califica lo que has visto y te armamos una ruta de películas a tu medida, '
                'con un modelo entrenado sobre 25 millones de calificaciones.</div>',
                unsafe_allow_html=True)
    col, _ = st.columns([3, 2])
    with col:
        entrar, crear = st.tabs(["Entrar", "Crear cuenta"])
        with entrar:
            with st.form("form_login", border=False):
                nombre = st.text_input("Nombre de usuario", key="login_nombre")
                clave = st.text_input("Contraseña", type="password", key="login_clave")
                if st.form_submit_button("Entrar", type="primary", use_container_width=True):
                    userId = cuentas.login(nombre, clave, ruta=ctx.cuentas_db)
                    if userId is None:
                        st.error("Nombre o contraseña incorrectos.")
                    else:
                        _entrar(userId, nombre.strip())
        with crear:
            with st.form("form_registro", border=False):
                nombre_n = st.text_input("Elige un nombre de usuario", key="reg_nombre",
                                         help="De 3 a 30 letras, números, punto, guion o _.")
                clave_n = st.text_input("Contraseña (mínimo 6 caracteres)", type="password",
                                        key="reg_clave")
                clave_2 = st.text_input("Repite la contraseña", type="password",
                                        key="reg_clave2")
                if st.form_submit_button("Crear cuenta", type="primary",
                                         use_container_width=True):
                    if clave_n != clave_2:
                        st.error("Las contraseñas no coinciden.")
                    else:
                        try:
                            userId = cuentas.registrar(nombre_n, clave_n, ruta=ctx.cuentas_db)
                        except cuentas.CuentaError as exc:
                            st.error(str(exc))
                        else:
                            _entrar(userId, nombre_n.strip())
        st.caption("Tu cuenta y tus calificaciones se guardan solo en este equipo.")
