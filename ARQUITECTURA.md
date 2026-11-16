# Plan de «recomendador-peliculas-v2» (version 4)
Lo mantiene Delegado; no lo edites.

Vision: CineRuta: una app Streamlit de estética oscura, inspirada en Letterboxd pero con paleta propia, montada sobre el recomendador MovieLens 25M ya verificado (ALS híbrido, pedido.py, chat; 31 requisitos). Tiene cuentas locales, onboarding, filas de carátulas personalizadas, buscador, ficha, reseñas públicas moderadas fuera de la app, modo explorar y chat como pestaña. Sigue una arquitectura Lambda: Spark batch escribe parquets, Kafka (topics 'ratings' y 'resenas') + Structured Streaming manejan los eventos, y la app solo lee lo precalculado. Es para mostrarla en una expo (póster con QR) y publicarla por Cloudflare Tunnel.
Stack: Mantener Python 3.12 + .venv, pyspark 3.5.5, confluent-kafka, Streamlit 1.40.1 y pytest (Las 374 pruebas verificadas corren sobre este stack. Cambiarlo rompería la regresión.); Spark (no pandas) para todo cálculo sobre 25M/IMDb, en recomendador/precalculo.py + scripts/precalcular_app.py, que escriben artefactos/<perfil>/app/*.parquet (Es la capa batch Lambda que pide el usuario. Con la muestra mini tarda segundos y sobre 25M la ejecuta el usuario con correr_25m.ps1.); BucketedRandomProjectionLSH (Spark ML) sobre itemFactors ALS para 'Porque te gustó X' (Lo pide el usuario y escala sin producto cruzado completo.); La app lee los parquets con pandas/pyarrow + st.cache_data/cache_resource y nunca importa pyspark (Así cada página carga en menos de 2 s sin levantar una JVM.); bcrypt + SQLite (artefactos/<perfil>/cuentas.db) para las cuentas; las reseñas van en una tabla 'resenas' de estado.db con columna 'oculta' (Es simple y sin servidor. Las reseñas siguen el mismo camino que los eventos (streaming o respaldo directo) y la moderación solo cambia un flag.); Kafka: topic 'ratings' (formato sin cambios) y topic nuevo 'resenas' (key userId) (Es el ajuste del usuario: así no se toca el esquema verificado de ratings.); Tipo de espectador de los usuarios nuevos con perfiles.asignar_tipo (media/std/centroides de tipos.json) y un vector crudo construido igual que vectores_gusto (Reutiliza los clusters existentes de perfilar_usuarios con el mismo escalado del KMeans, sin Spark en la app.); segno para el QR; TMDb vía requests con caché JSON; sin TMDB_API_KEY, carátula SVG generada (Son Python puro y deterministas, y las pruebas no dependen de la red.)
Arquitectura: recomendador/ (núcleo verificado: config, datos, batch_als, foldin, perfiles, herramientas, pedido, llm, titulos, streaming, kafka_utils, estado, poster) + módulos nuevos:
- precalculo.py (Spark): peliculas.parquet (stats antes del corte, histograma de 10 casillas, tags), personas.parquet (IMDb vía links.csv), populares_por_tipo.parquet (cluster de perfiles.parquet), vecinos.parquet (LSH, top-20). Lo llama scripts/precalcular_app.py; correr_25m.ps1 agrega el paso.
- cuentas.py: registro/login bcrypt y userId > 162541; Limitador (20 acciones/min por sesión).
- moderacion.py: filtro de palabras ofensivas (recomendador/palabras_ofensivas.txt, sin tildes, palabra completa) y validación de 500 caracteres.
- eventos.py: publicar_rating → topic 'ratings', publicar_resena → topic 'resenas', key userId; si Kafka no responde en 3 s, escribe directo en estado.db y devuelve un aviso.
- streaming.py: además procesa el topic 'resenas' → tabla resenas.
- perfiles.py: + vector_crudo(ratings) y tipo_usuario(userId), que recalcula el tipo con cada calificación nueva.
- scripts/moderar_resenas.py (solo local): --listar / --ocultar ID / --mostrar ID.
- catalogo_app.py (solo lectura, con caché): buscar, ficha, filas_inicio, explicar, explorar. tmdb.py: póster, sinopsis y tráiler, o SVG generado.
- app/app.py ('CineRuta'): login → onboarding → pestañas Inicio | Buscar | Explorar | Chat; ficha por ?pelicula=<id>; vistas en app/vistas/*.py; estilo en app/estilo.py.
Flujo: batch Spark → parquets → app. Evento → Kafka → Structured Streaming → estado.db → fold-in y tipo actualizado en la app. Las pruebas usan muestras/ml-mini, muestras/imdb-mini y artefactos/mini.
Diseno: Marca 'CineRuta' (título de página, cabecera y pie). Paleta oscura propia, inspirada en Letterboxd pero sin sus colores exactos (prohibidos #14181c, #2c3440, #00e054, #ff8000, #40bcf4, #9ab):
- fondo #0e1216, superficies #1f2730, bordes #34404c
- texto #aab4be, títulos #f3f5f7
- acento principal ámbar #f0b23a (botones, estrellas propias, barras del histograma)
- secundarios coral #e5675c ('puede que no') y turquesa #3cb4a8 ('probablemente te guste', enlaces)
Tipografía sans del sistema (system-ui), títulos en serif ligera (Georgia). Carátulas 2:3 con borde redondeado de 4 px en filas horizontales desplazables. Ficha con fondo difuminado del póster e histograma 0,5–5 en ámbar; la afinidad se muestra como texto 'afinidad alta/media'. Las carátulas generadas llevan un color por género y el título centrado. Tono en español, cercano y breve. Al pie, la atribución a TMDb. El CSS está en app/estilo.py y se inyecta una sola vez.
Supuestos: Las reseñas se guardan en una tabla 'resenas' de estado.db (columna 'oculta'), alimentada por streaming desde el topic 'resenas' o por el respaldo directo. Las cuentas van en cuentas.db, aparte.; Una reseña con una palabra ofensiva se rechaza entera (no se censura con asteriscos).; El límite de 20/min es por sesión de Streamlit (en memoria) y cuenta solo acciones que escriben o llaman al LLM, no la navegación.; El vector del usuario nuevo usa la misma función de features que perfilar_usuarios (géneros + tags genome + log(1+n)). Si un rasgo de tipos.json no se puede calcular, toma el valor medio (0 al estandarizar).; Paleta CineRuta: fondo #0e1216, superficie #1f2730, texto #aab4be, ámbar #f0b23a, turquesa #3cb4a8, coral #e5675c.; Los paquetes 1–5 del plan anterior ya están verificados y no se rehacen. Los 6 paquetes de este plan reemplazan a los pendientes 6–11 con los ajustes incorporados.

## Paquetes de trabajo
### 1. 1. Corte temporal en todos los modos y demo.json relativo [verificado]
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
### 2. 2. Interpretación del pedido y búsqueda de títulos (MovieLens + TMDb opcional) [verificado]
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
### 3. 3. Recomendar por niveles de coincidencia y herramienta buscar_pelicula [verificado]
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
### 4. 4. Chat robusto: red de seguridad, regla de sistema y batería de 30 pedidos [verificado]
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
### 5. 5. Figura tipos_espectador legible y cierre (README) [verificado]
Objetivo: Rehacer la figura tipos_espectador con barras horizontales sin etiquetas superpuestas y documentar en el README el corte en todos los modos, los niveles de coincidencia, buscar_pelicula con TMDb opcional y los pasos manuales sobre los 25M.
Requisitos: 7, 1 · Archivos: recomendador/poster.py, tests/test_poster_tipos.py, README.md · Depende de: 1
- Criterio: poster.py genera tipos_espectador con barh, ordenada y con las etiquetas a la izquierda
- Criterio: tests/test_poster_tipos.py renderiza con datos de la muestra mini y comprueba que las cajas de las etiquetas (get_window_extent) no se intersectan
- Criterio: El README explica el corte en todos los modos, que TMDB_API_KEY es opcional para los títulos en español, y que la regeneración sobre los 25M se hace a mano con correr_25m.ps1 y scripts/figuras_poster.
- Criterio: test_readme.py y test_poster.py siguen pasando
- Prueba de aceptacion: La figura no tiene etiquetas superpuestas: el comando «.venv/Scripts/python -m pytest -q tests/test_poster_tipos.py» debe terminar sin error
### 6. 1. Capa batch Spark para la app (parquets) + limpieza de llm.py [pendiente]
Objetivo: Crear recomendador/precalculo.py y scripts/precalcular_app.py, que con Spark generen en artefactos/<perfil>/app/ cuatro parquets: peliculas (id, título, año, géneros, duración, nota IMDb, votos y promedio antes del corte, histograma 0,5–5, tags principales), personas (directores/actores por movieId vía links.csv + title.crew/principals/name.basics, con nombre normalizado sin tildes), populares_por_tipo (agregación por cluster de perfiles.parquet, antes del corte) y vecinos (LSH sobre itemFactors, top-20). Agregar el paso a correr_25m.ps1. Quitar de llm.py _validar, completar_args, como_dict y 
Requisitos: 32, 35, 37, 42, 47, 48 · Archivos: recomendador/precalculo.py, scripts/precalcular_app.py, correr_25m.ps1, recomendador/llm.py, tests/test_precalculo.py · Depende de: -
- Criterio: scripts/precalcular_app.py --perfil mini termina en menos de 3 minutos y crea los 4 parquets
- Criterio: recomendador/precalculo.py no importa pandas; todo se hace con pyspark.sql/pyspark.ml
- Criterio: El histograma, el promedio, los votos y populares_por_tipo excluyen timestamps >= FECHA_CORTE_MINI (prueba con valores conocidos)
- Criterio: populares_por_tipo usa la columna cluster de perfiles.parquet (los mismos tipos de perfilar_usuarios)
- Criterio: vecinos.parquet no incluye a la propia película y tiene a lo sumo 20 vecinos por movieId
- Criterio: personas filtra por los tconst de links.csv antes de cruzar con principals
- Criterio: Si imdb-mini no trae un director conocido (Nolan), se agregan filas conocidas a la muestra
- Criterio: llm.py ya no define los 4 símbolos y la suite previa sigue pasando
- Prueba de aceptacion: Precálculos Spark sobre la muestra mini (corte, LSH, personas, populares por tipo): el comando «.venv/Scripts/python -m pytest -q tests/test_precalculo.py» debe terminar sin error
- Prueba de aceptacion: Regresión del chat tras la limpieza de llm.py: el comando «.venv/Scripts/python -m pytest -q tests/test_llm_redactor.py tests/test_bateria.py» debe terminar sin error
### 7. 2. Cuentas, límite de 20/min, reseñas moderadas, eventos Kafka y tipo de espectador de usuarios nuevos [pendiente]
Objetivo: Crear cuentas.py (bcrypt + SQLite, userId > 162541, Limitador de 20 acciones/min por sesión), moderacion.py (máx. 500 caracteres y filtro de palabras ofensivas), eventos.py (ratings → topic 'ratings' sin cambiar el formato; reseñas → topic 'resenas'; key userId; respaldo directo a estado.db con aviso), extender streaming.py para el topic 'resenas', crear scripts/moderar_resenas.py y agregar a perfiles.py vector_crudo/tipo_usuario para asignar el centroide más cercano con el mismo escalado. Agregar bcrypt y segno a requirements.txt.
Requisitos: 33, 41, 44, 46, 49, 50, 51, 52 · Archivos: recomendador/cuentas.py, recomendador/moderacion.py, recomendador/palabras_ofensivas.txt, recomendador/eventos.py, recomendador/streaming.py, recomendador/estado.py, recomendador/perfiles.py, scripts/moderar_resenas.py, requirements.txt, tests/test_cuentas.py, tests/test_eventos.py, tests/test_tipo_nuevo.py · Depende de: -
- Criterio: La contraseña se guarda solo como hash bcrypt; el login con clave incorrecta falla y con la correcta devuelve el userId
- Criterio: El primer usuario recibe userId >= 162542 y el siguiente uno mayor; un nombre duplicado se rechaza
- Criterio: El Limitador acepta 20 acciones y rechaza la 21.ª dentro del mismo minuto; la acepta tras 60 s (reloj simulado)
- Criterio: Una reseña de 501 caracteres o con una palabra de la lista (también con mayúsculas o tildes) se rechaza con un mensaje; una válida se acepta
- Criterio: Con un productor simulado, la calificación va a 'ratings' con el mismo formato que antes y la reseña a 'resenas', ambas con key=str(userId)
- Criterio: Con Kafka caído (simulado), la calificación o reseña queda en estado.db y se devuelve un aviso no vacío
- Criterio: scripts/moderar_resenas.py --ocultar ID marca la reseña como oculta y las consultas públicas ya no la devuelven; ningún módulo de app/ importa moderar_resenas
- Criterio: tipo_usuario sobre las 10 calificaciones de onboarding coincide con asignar_tipo(vector_crudo) usando media/std de tipos.json, y cambia al agregar calificaciones que mueven el vector hacia otro centro
- Prueba de aceptacion: Cuentas bcrypt, userIds > 162541 y límite de 20 por minuto: el comando «.venv/Scripts/python -m pytest -q tests/test_cuentas.py» debe terminar sin error
- Prueba de aceptacion: Ratings y reseñas por Kafka en topics separados, respaldo directo, filtro de reseñas y moderación local: el comando «.venv/Scripts/python -m pytest -q tests/test_eventos.py tests/test_streaming.py» debe terminar sin error
- Prueba de aceptacion: Tipo de espectador del centroide más cercano para usuarios nuevos, con el mismo escalado: el comando «.venv/Scripts/python -m pytest -q tests/test_tipo_nuevo.py» debe terminar sin error
### 8. 3. Capa de lectura: catálogo, buscador, ficha, explicación 'Para ti' y TMDb [pendiente]
Objetivo: Crear recomendador/catalogo_app.py (solo lectura de parquets y estado.db, con caché): buscar por título o persona sin tildes; ficha (incluye las reseñas no ocultas); filas_inicio con 'Para ti' (fold-in), 'Populares entre espectadores como tú' (según tipo_usuario) y 'Porque te gustó X' (vecinos); explicar con veredicto, la más parecida calificada alto y la calificada baja, tags a favor/en contra y persona en común. Crear tmdb.py: póster, sinopsis y tráiler con caché, o SVG generado sin clave.
Requisitos: 35, 36, 37, 38, 39, 48, 52 · Archivos: recomendador/catalogo_app.py, recomendador/tmdb.py, tests/test_catalogo_app.py, tests/test_tmdb.py · Depende de: 6, 7
- Criterio: catalogo_app no importa pyspark (comprobado con sys.modules)
- Criterio: buscar('nolan') y buscar('NOLÁN') devuelven las películas de Nolan de la muestra; la búsqueda por título tolera tildes
- Criterio: ficha() trae año, géneros, duración, nota IMDb, director, reparto, histograma de 10 casillas, promedio, tags y reseñas visibles (una reseña oculta no aparece)
- Criterio: 'Populares entre espectadores como tú' de un usuario nuevo usa el cluster que da tipo_usuario
- Criterio: explicar() usa solo ratings anteriores al corte más los eventos en vivo, y su texto trae títulos y tags concretos, sin números de afinidad tipo estrellas
- Criterio: Sin TMDB_API_KEY, poster() devuelve un SVG con el título y el color del género, y trailer() devuelve None sin excepción
- Criterio: 20 llamadas a ficha() y filas_inicio() con caché caliente tardan menos de 2 s en total
- Prueba de aceptacion: Buscador, ficha, filas de inicio por tipo y explicación con la muestra mini: el comando «.venv/Scripts/python -m pytest -q tests/test_catalogo_app.py» debe terminar sin error
- Prueba de aceptacion: TMDb con caché y respaldo sin clave: el comando «.venv/Scripts/python -m pytest -q tests/test_tmdb.py» debe terminar sin error
### 9. 4. App CineRuta: estilo propio, login, onboarding e Inicio [pendiente]
Objetivo: Reestructurar app/app.py como 'CineRuta': estilo en app/estilo.py con la paleta propia; pantalla de login/registro; onboarding (calificar ~10 populares o importar CSV de Letterboxd) con fold-in y tipo de espectador al instante; pestañas Inicio | Buscar | Explorar | Chat. Inicio muestra las tres filas y el tipo de espectador del usuario. El chat pasa a su pestaña sin cambiar su lógica. Cada acción (calificar, reseñar, mensaje de chat, importar) pasa por el Limitador.
Requisitos: 32, 33, 34, 35, 51, 52, 53 · Archivos: app/app.py, app/estilo.py, app/vistas/login.py, app/vistas/onboarding.py, app/vistas/inicio.py, app/vistas/chat.py, muestras/letterboxd_mini.csv, tests/test_app_letterboxd.py, tests/test_app.py · Depende de: 7, 8
- Criterio: Con AppTest, un usuario sin sesión ve solo el login/registro y el nombre 'CineRuta'
- Criterio: Tras registrarse ve el onboarding; al calificar 10 películas, Inicio muestra 'Para ti', 'Populares entre espectadores como tú' y 'Porque te gustó <título>' con al menos una carátula cada una, y el nom
- Criterio: Importar muestras/letterboxd_mini.csv (10 filas conocidas, creado aquí) salta el onboarding
- Criterio: La acción 21 en menos de un minuto muestra un aviso de límite y no se registra
- Criterio: app/estilo.py no contiene ninguno de #14181c, #2c3440, #00e054, #ff8000, #40bcf4 (prueba de texto) y define los colores de la paleta CineRuta
- Criterio: La pestaña Chat responde 'drama policial con un twist final' con la línea 'Entendí:'
- Criterio: La app no importa pyspark al navegar
- Prueba de aceptacion: Flujo login → onboarding → Inicio con tres filas, tipo de espectador, límite y paleta CineRuta: el comando «.venv/Scripts/python -m pytest -q tests/test_app_letterboxd.py» debe terminar sin error
- Prueba de aceptacion: Regresión del chat dentro de la app: el comando «.venv/Scripts/python -m pytest -q tests/test_app.py» debe terminar sin error
### 10. 5. Buscar, Ficha con reseñas, tarjeta 'Para ti' y Explorar [pendiente]
Objetivo: Implementar las vistas Buscar (título o persona); Ficha (póster/sinopsis TMDb o carátula generada, datos IMDb, histograma, promedio, tags, reseñas públicas, botones para calificar y reseñar con el contador de 500 caracteres, el filtro y el aviso si Kafka falla, tráiler si existe, atribución TMDb); y Explorar (una película a la vez con la explicación personalizada y el botón Siguiente). La tarjeta 'Para ti' usa explicar(); el LLM solo redacta, con la plantilla como respaldo. Al calificar se recalcula el tipo de espectador.
Requisitos: 36, 37, 38, 39, 40, 41, 46, 48, 49, 52 · Archivos: app/vistas/buscar.py, app/vistas/ficha.py, app/vistas/explorar.py, app/vistas/tarjetas.py, tests/test_app_ficha.py · Depende de: 9
- Criterio: AppTest: buscar 'nolan' muestra al menos un resultado; la ficha muestra 'Director', 'Reparto', el histograma y la atribución a TMDb
- Criterio: Sin TMDB_API_KEY la ficha no muestra 'Error' ni 'Traceback'
- Criterio: Calificar con Kafka caído (simulado) muestra el aviso y la calificación aparece en estado.db
- Criterio: Una reseña válida se publica y aparece en la ficha; una con palabra ofensiva o de más de 500 caracteres muestra un mensaje y no se guarda
- Criterio: No hay en la app ningún botón ni control para ocultar reseñas
- Criterio: Explorar: 'Siguiente' cambia a otra película y muestra 'probablemente te guste' o 'puede que no'
- Prueba de aceptacion: Buscar, ficha sin clave TMDb, calificar y reseñar con filtro y respaldo, explorar: el comando «.venv/Scripts/python -m pytest -q tests/test_app_ficha.py» debe terminar sin error
### 11. 6. Chat por persona, modo público, QR, rendimiento y cierre [pendiente]
Objetivo: Extender pedido.py para detectar personas ('algo de Nolan', 'con Tom Hanks') con personas.parquet, como condición del orden por niveles. Agregar el modo público (MODO_PUBLICO=1): login obligatorio, límite de 20/min, LLM de a una solicitud (si está ocupado, plantilla) y sin controles de administración. Agregar la figura QR hacia URL_PUBLICA. Medir los tiempos por página. Actualizar el README: orden de scripts (precalcular_app, streaming, cloudflared), moderar_resenas.py y CineRuta. Regenerar scripts/vista_previa_app.py.
Requisitos: 43, 44, 45, 50, 51 · Archivos: recomendador/pedido.py, recomendador/herramientas.py, recomendador/llm.py, app/app.py, scripts/figuras_poster.py, scripts/vista_previa_app.py, README.md, tests/test_personas_chat.py, tests/test_modo_publico.py, tests/test_rendimiento_app.py · Depende de: 10
- Criterio: pedido.analizar('algo de Nolan') produce una condición de persona; el chat devuelve películas de Nolan de la muestra y 'Entendí:' lo menciona
- Criterio: Con MODO_PUBLICO=1, AppTest sin sesión no muestra pestañas ni controles de administración (rutas, recarga, selector de usuario, moderación)
- Criterio: Con el semáforo del LLM tomado, una segunda solicitud responde con la plantilla sin esperar
- Criterio: figuras_poster.py genera qr_publico.png si URL_PUBLICA está definida y avisa si no
- Criterio: Inicio, Buscar, Ficha y Explorar cargan cada una en menos de 2 s con caché caliente (muestra mini)
- Criterio: El README documenta scripts/moderar_resenas.py como único camino de moderación
- Criterio: La suite completa pasa
- Prueba de aceptacion: Chat entiende pedidos por persona: el comando «.venv/Scripts/python -m pytest -q tests/test_personas_chat.py» debe terminar sin error
- Prueba de aceptacion: Modo público: login, límite 20/min, LLM de a uno, sin administración, QR: el comando «.venv/Scripts/python -m pytest -q tests/test_modo_publico.py» debe terminar sin error
- Prueba de aceptacion: Cada página en menos de 2 s con datos precalculados: el comando «.venv/Scripts/python -m pytest -q tests/test_rendimiento_app.py» debe terminar sin error

## Riesgos
- Los AppTest con login, onboarding y pestañas pueden volverse lentos o frágiles. Conviene que las vistas sean funciones puras y probar la lógica fuera de Streamlit.
- Reestructurar app/app.py puede romper tests/test_app.py (verificado). El chat debe mantener su API.
- title.principals de 25M pesa varios GB: hay que filtrar por los tconst de links.csv antes de cruzar, o Spark local puede quedarse sin memoria.
- Si el vector del usuario nuevo no replica exactamente las features de vectores_gusto, el tipo asignado no será coherente con los clusters. La prueba debe comparar con un usuario del perfilado.
- Un filtro por lista de palabras es básico: tiene falsos positivos y negativos, y no reemplaza la moderación manual.
- bcrypt y segno deben instalarse en .venv. Sin red, fallan las pruebas de los paquetes 2 y 6.

## Limitaciones
- Las corridas sobre 25M e IMDb completo (precalcular_app.py --perfil 25m, perfilar_usuarios) las ejecuta el usuario con correr_25m.ps1 y no se verifican automáticamente.
- Kafka y Structured Streaming reales los levanta el usuario a mano. Las pruebas usan un productor simulado y el respaldo directo.
- Cloudflare Tunnel (cloudflared) es un paso manual. El plan deja listos el modo público y el QR.
- Sin TMDB_API_KEY no hay pósters reales, sinopsis ni tráileres.
- El tiempo de menos de 2 s se mide con la muestra mini.
- El límite por sesión se reinicia si el usuario abre una sesión nueva. No es una protección fuerte contra abuso.