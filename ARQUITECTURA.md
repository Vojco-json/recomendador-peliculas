# Plan de «recomendador-peliculas-v2» (version 2)
Lo mantiene Delegado; no lo edites.

Vision: Reparar y pulir el recomendador de películas que ya existe (MovieLens, ALS con Spark, perfiles, streaming Kafka, app Streamlit con chat por reglas u Ollama, póster) sin rehacer lo verificado. Hay cinco cambios: (1) en todos los modos, el historial del usuario son los ratings anteriores a FECHA_CORTE más los eventos en vivo; (2) el orden se arma por niveles de coincidencia con el pedido y, dentro de cada nivel, por afinidad ALS; (3) el pedido se interpreta aunque traiga ánimos, errores de tipeo o venga en inglés; (4) buscar_pelicula busca de forma aproximada en MovieLens y, si hay clave, también en los títulos en español de TMDb guardados en caché; (5) el chat nunca termina sin películas. Las pruebas usan solo la muestra mini y las 117 pruebas actuales se mantienen.
Stack: Mantener Python, el .venv, pytest, Streamlit, Spark y el resto del stack actual (El proyecto ya funciona y tiene 117 pruebas verificadas. Hay que reparar, no reescribir.); Aplicar el corte siempre en Herramientas: corte = config.FECHA_CORTE por defecto y FECHA_CORTE_MINI con la muestra mini (Es lo que pidió el usuario: el modelo se entrenó hasta el corte, así que el historial posterior no debe existir en ningún modo. Además, un solo camino es más fácil de probar.); Hacer la coincidencia aproximada con difflib y unicodedata de la biblioteca estándar (No hay que instalar nada y alcanza para tolerar tildes, mayúsculas y errores de tipeo.); Para los títulos en español, reutilizar ClienteTMDb de recomendador/enriquecimiento.py y su caché artefactos/tmdb_cache.json, y agregar una búsqueda /search/movie con language=es-ES que también se guarda en caché (Ya existe un cliente con caché que, sin TMDB_API_KEY, devuelve None sin errores. Así la búsqueda en español es opcional y las pruebas no salen a la red: se les inyecta un cliente falso.); Crear el módulo recomendador/pedido.py con diccionarios ES/EN de géneros, ánimos y ocasiones (Es determinista y se puede probar sin LLM. Lo usan el modo reglas, el modo Ollama y la red de seguridad.); Simular el LLM con un cliente falso que se inyecta en responder_ollama (La batería de 30 pedidos tiene que correr sin Ollama y en pocos minutos.)
Arquitectura: recomendador/: config.py (FECHA_CORTE, FECHA_CORTE_MINI, TMDB_API_KEY), herramientas.py (Herramientas: catálogo, historial con el corte siempre aplicado, recomendar por niveles, buscar_pelicula, ESQUEMAS), pedido.py (NUEVO: texto -> {generos, tags_genome, max_duracion_min, condiciones, titulo_mencionado, pide_recomendacion}), titulos.py (NUEVO: normalizar(), índice de títulos MovieLens + títulos ES de tmdb_cache.json, búsqueda aproximada y consulta opcional a TMDb mediante ClienteTMDb.buscar con caché), llm.py (interpretar delega en pedido.py; responder_reglas y responder_ollama, _validar, red de seguridad), enriquecimiento.py (ClienteTMDb + búsqueda con caché), demo.py (demo.json relativo), poster.py (figuras). Flujo: app/app.py -> llm.responder -> (reglas | ollama + herramientas) -> Herramientas.recomendar(niveles: n.º de condiciones cumplidas DESC, luego afinidad ALS; sin condiciones = orden híbrido actual) -> tarjetas con «Cumple / No cumple / No se pudo evaluar». Scripts que el usuario corre sobre los 25M: correr_25m.ps1 (perfilar_usuarios --hasta FECHA_CORTE), scripts/productor_kafka.py (--desde FECHA_CORTE por defecto). Las pruebas van en tests/test_<punto>.py y usan tests/base_mini.py.
Diseno: La estética de la app Streamlit no cambia. Cada tarjeta suma la línea «Cumple: …» y, cuando corresponde, «No cumple: …» y «No se pudo evaluar: …» (por ejemplo, la duración sin IMDb). Si el título se encontró por TMDb, la tarjeta muestra el título en español y, entre paréntesis, el original. La figura tipos_espectador pasa a barras horizontales ordenadas, con las etiquetas a la izquierda sin superponerse, letra de 11 pt o más y la paleta de las demás figuras. El tono del chat sigue siendo breve y en español.
Supuestos: El plan v1 ya estaba aprobado; solo cambian los puntos 1, 2 y 3 que pidió el usuario. Ningún paquete figura como verificado todavía, así que se mantienen los cinco con sus ajustes.; Con la muestra mini, el corte de las pruebas es FECHA_CORTE_MINI (2016-01-01). Los ratings de 2017 de los usuarios 12 y 15 sirven como dato conocido posterior al corte.; Los eventos en vivo de estado.db pueden tener cualquier fecha y siempre se muestran.; Una condición no evaluable (por ejemplo, la duración sin IMDb) no suma ni resta al nivel: se informa aparte.; «Corto» sigue siendo ≤ 100 min (DURACION_CORTA_MIN actual).; Se reutiliza ClienteTMDb y su caché; se le agrega un método de búsqueda por texto.; El LLM simulado es un cliente falso inyectado; no se prueba contra Ollama real ni contra TMDb real.

## Paquetes de trabajo
### 1. 1. Corte temporal en todos los modos y demo.json relativo [pendiente]
Objetivo: Que en todos los modos (demo o no) Herramientas y la app solo vean ratings y tags anteriores a FECHA_CORTE, más los eventos en vivo de estado.db. Que los scripts de los 25M respeten el corte por defecto y que demo.json use rutas relativas.
Requisitos: 1, 2, 9 · Archivos: recomendador/herramientas.py, recomendador/config.py, recomendador/demo.py, app/app.py, scripts/productor_kafka.py, correr_25m.ps1, tests/test_corte.py · Depende de: -
- Criterio: Herramientas aplica siempre un corte: por defecto config.FECHA_CORTE; con la muestra mini, FECHA_CORTE_MINI; se puede pasar explícito con el parámetro 'corte'. No hay ningún camino sin corte.
- Criterio: _historial, peliculas_calificadas, tags_usuario y _popularidad descartan los timestamps >= corte, en modo demo y fuera de él
- Criterio: Los eventos de estado.db se incluyen siempre, aunque su fecha sea posterior al corte
- Criterio: correr_25m.ps1 pasa --hasta con FECHA_CORTE a perfilar_usuarios.py
- Criterio: productor_kafka.py usa por defecto --desde config.FECHA_CORTE
- Criterio: demo.json no contiene rutas absolutas (ni 'C:' ni barras iniciales) y demo.py/app.py las resuelven contra la raíz del proyecto
- Criterio: tests/test_corte.py prueba el corte con y sin modo demo sobre la muestra mini (por ejemplo, los ratings de 2017 de los usuarios 12 y 15 no aparecen)
- Criterio: Las 117 pruebas existentes siguen pasando; si alguna esperaba ver datos posteriores al corte, se ajusta y el cambio se documenta
- Prueba de aceptacion: Corte en todos los modos, eventos en vivo, demo.json relativo y valores por defecto de los scripts: el comando «.venv/Scripts/python -m pytest -q tests/test_corte.py» debe terminar sin error
- Prueba de aceptacion: Regresión completa: el comando «.venv/Scripts/python -m pytest -q» debe terminar sin error
### 2. 2. Interpretación del pedido y búsqueda de títulos (MovieLens + TMDb opcional) [pendiente]
Objetivo: Crear recomendador/pedido.py, que convierte un mensaje libre en condiciones, y recomendador/titulos.py, que busca títulos de forma aproximada contra MovieLens y, si hay TMDB_API_KEY, también contra los títulos en español de TMDb con caché.
Requisitos: 3, 5, 10 · Archivos: recomendador/pedido.py, recomendador/titulos.py, recomendador/enriquecimiento.py, recomendador/llm.py, tests/test_pedido.py, tests/test_titulos.py · Depende de: -
- Criterio: Ánimos y ocasiones mapeados a géneros y tags: 'para llorar' -> Drama + sad/tearjerker; 'en familia' -> Children/Animation + family; 'liviano' -> Comedy; 'que me haga pensar' -> thought-provoking; 'con
- Criterio: Sinónimos en inglés (comedy, scary, feel good, short, family movie) y tolerancia a errores de tipeo (comdia, terorr, ciensia ficcion)
- Criterio: Detecta 'algo como X' o 'parecida a X' y devuelve titulo_mencionado. pide_recomendacion es False para saludos y preguntas generales
- Criterio: Las condiciones salen como textos en español; solo se usan tags que existen en genome-tags.csv y los demás se descartan sin error
- Criterio: llm.interpretar delega en pedido.py y mantiene su forma de salida
- Criterio: titulos.buscar ignora tildes y mayúsculas, quita el año '(1999)' y el artículo final ', The' de MovieLens, y usa un umbral de coincidencia aproximada (ratio de al menos 0.8) para evitar falsos positiv
- Criterio: Sin TMDB_API_KEY no hace llamadas de red y funciona solo con MovieLens
- Criterio: Con clave, usa los títulos en español de tmdb_cache.json y ClienteTMDb.buscar (/search/movie con language=es-ES), cuyo resultado queda en caché; tmdbId se mapea a movieId con links.csv
- Prueba de aceptacion: Interpretación ES/EN, errores de tipeo, ánimos y ocasiones: el comando «.venv/Scripts/python -m pytest -q tests/test_pedido.py» debe terminar sin error
- Prueba de aceptacion: Búsqueda de títulos con y sin TMDb (cliente falso, caché): el comando «.venv/Scripts/python -m pytest -q tests/test_titulos.py» debe terminar sin error
### 3. 3. Recomendar por niveles de coincidencia y herramienta buscar_pelicula [pendiente]
Objetivo: Reemplazar los filtros duros de Herramientas.recomendar por grupos según el número de condiciones cumplidas, ordenar por afinidad ALS dentro de cada grupo y registrar buscar_pelicula en ESQUEMAS.
Requisitos: 4, 5, 11 · Archivos: recomendador/herramientas.py, tests/test_puntaje.py, tests/test_buscar.py · Depende de: 1, 2
- Criterio: recomendar acepta generos, tags y max_duracion_min y nunca devuelve una lista vacía si el catálogo tiene películas
- Criterio: Orden: primero el nivel (número de condiciones cumplidas, de mayor a menor) y dentro de cada nivel la afinidad ALS de mayor a menor. Una película de nivel 2 nunca aparece después de una de nivel 1
- Criterio: Sin condiciones, el orden es idéntico al del híbrido actual (la prueba compara con la salida previa)
- Criterio: Cada recomendación trae 'nivel', 'cumple', 'no_cumple' y 'no_evaluables'. La duración sin dato de IMDb va a no_evaluables, la respuesta incluye un aviso y esa condición no baja el nivel
- Criterio: buscar_pelicula(titulo) usa recomendador/titulos.py y devuelve {encontrada, movieId, titulo, titulo_es?} o encontrada=false con un mensaje de una frase
- Criterio: ESQUEMAS incluye buscar_pelicula con su descripción y parámetros
- Criterio: Las pruebas usan la muestra mini y un cliente TMDb falso o ninguno
- Prueba de aceptacion: Niveles de coincidencia, ALS dentro del nivel, sin condiciones = orden híbrido, lista nunca vacía: el comando «.venv/Scripts/python -m pytest -q tests/test_puntaje.py» debe terminar sin error
- Prueba de aceptacion: buscar_pelicula como herramienta y caso inexistente: el comando «.venv/Scripts/python -m pytest -q tests/test_buscar.py» debe terminar sin error
### 4. 4. Chat robusto: red de seguridad, regla de sistema y batería de 30 pedidos [pendiente]
Objetivo: Integrar pedido.py, la recomendación por niveles y buscar_pelicula en responder_reglas y responder_ollama, agregar la red de seguridad y la batería de 30 pedidos con LLM simulados.
Requisitos: 6, 8 · Archivos: recomendador/llm.py, app/app.py, tests/test_bateria.py, tests/test_red_seguridad.py · Depende de: 3
- Criterio: SISTEMA incluye una regla sobre ánimos, ocasiones y buscar_pelicula, y el modo Ollama completa con pedido.py los filtros que el modelo omitió
- Criterio: Si se pidieron recomendaciones y _validar deja cero películas, el código llama a recomendar con lo detectado y muestra esas tarjetas
- Criterio: El texto 'No pude obtener recomendaciones' no aparece en ningún camino con catálogo disponible
- Criterio: Cada recomendación dice qué condiciones cumple, cuáles no y cuáles no se pudieron evaluar, y app/app.py lo muestra en la tarjeta
- Criterio: Un título que no está en el catálogo genera una frase ('No encontré «El mecanismo» en el catálogo…') y aun así recomendaciones
- Criterio: tests/test_bateria.py tiene 30 pedidos parametrizados, cada uno en modo reglas y con 3 LLM simulados (uno correcto, uno sin películas y uno que inventa IDs); los mensajes que no son pedidos reciben un
- Criterio: La batería completa tarda menos de 2 minutos (Herramientas se crea una vez por módulo)
- Prueba de aceptacion: Batería de 30 pedidos en modo reglas y con LLM simulado: el comando «.venv/Scripts/python -m pytest -q tests/test_bateria.py» debe terminar sin error
- Prueba de aceptacion: Red de seguridad y mensaje de título inexistente: el comando «.venv/Scripts/python -m pytest -q tests/test_red_seguridad.py» debe terminar sin error
### 5. 5. Figura tipos_espectador legible y cierre (README) [pendiente]
Objetivo: Rehacer la figura tipos_espectador con barras horizontales sin etiquetas superpuestas y documentar en el README el corte en todos los modos, los niveles de coincidencia, buscar_pelicula con TMDb opcional y los pasos manuales sobre los 25M.
Requisitos: 7, 1 · Archivos: recomendador/poster.py, tests/test_poster_tipos.py, README.md · Depende de: 1
- Criterio: poster.py genera tipos_espectador con barh, ordenada y con las etiquetas a la izquierda
- Criterio: tests/test_poster_tipos.py renderiza con datos de la muestra mini y comprueba que las cajas de las etiquetas (get_window_extent) no se intersectan
- Criterio: El README explica el corte en todos los modos, que TMDB_API_KEY es opcional para los títulos en español, y que la regeneración sobre los 25M se hace a mano con correr_25m.ps1 y scripts/figuras_poster.
- Criterio: test_readme.py y test_poster.py siguen pasando
- Prueba de aceptacion: La figura no tiene etiquetas superpuestas: el comando «.venv/Scripts/python -m pytest -q tests/test_poster_tipos.py» debe terminar sin error

## Riesgos
- Aplicar el corte en todos los modos puede romper pruebas actuales (test_herramientas, test_app, test_streaming) que esperaban ver ratings posteriores. Hay que ajustarlas y documentarlo, sin borrar ninguna.
- Los perfiles precalculados sobre los 25M siguen incluyendo datos posteriores al corte hasta que el usuario corra correr_25m.ps1 con --hasta.
- Pasar de filtros duros a niveles puede cambiar resultados que esperan las pruebas actuales. La prueba 'sin condiciones = orden híbrido' protege el caso base.
- La coincidencia aproximada puede dar falsos positivos con títulos cortos; el umbral se fija en 0.8 y se prueba.
- Recargar el catálogo en cada caso hace lenta la batería de 30 × 4 modos; hay que usar una fixture de módulo.

## Limitaciones
- Los artefactos de los 25M (perfiles con --hasta y figuras del póster) no se regeneran dentro de los paquetes: los regenera el usuario con correr_25m.ps1 y scripts/figuras_poster.py.
- Sin TMDB_API_KEY, un título solo en español se reconoce si se parece lo suficiente al de MovieLens o ya está en la caché.
- La duración depende de IMDb; sin ese dato, la condición queda como no evaluable.
- En modo reglas, la tolerancia a errores de tipeo es léxica (difflib) y no entiende frases muy libres.