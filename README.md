# Recomendador de películas distribuido (MovieLens 25M)

> README provisional: la guía completa (instalación, corrida de 25M, demo de 5 minutos,
> licencias y solución de problemas en Windows) se entrega en el paquete 9.

## App Streamlit con chat

```powershell
.venv\Scripts\python -m streamlit run app/app.py
```

- **Barra lateral:** `userId`, tipo de espectador, géneros con más peso, modo
  (completo/demo), «Actualizado hace X s» (leído de `artefactos/estado.db`) y la
  configuración del LLM.
- **Columna principal:** tarjetas con las recomendaciones y un chat
  (ej.: «algo corto y liviano para hoy»). El chat solo recomienda películas que
  entrega la herramienta `recomendar` y explica cada una con datos del usuario y de la
  película; las películas inventadas se descartan.

### Variables del LLM

| Variable | Por defecto | Uso |
|---|---|---|
| `LLM_PROVIDER` | `ollama` | `ollama` o `reglas` (determinista, sin red) |
| `LLM_MODEL` | `qwen3.5:9b` | modelo de Ollama |
| `OLLAMA_HOST` | `http://localhost:11434` | servidor de Ollama |
| `LLM_THINK` | `false` | razonamiento del modelo; su contenido nunca se muestra |

Si Ollama no responde, la app muestra «Ollama no responde: usando modo reglas» y sigue
funcionando con el proveedor `reglas`.

Pruebas de este módulo:

```powershell
.venv\Scripts\python -m pytest -q tests/test_herramientas.py tests/test_app.py
```
