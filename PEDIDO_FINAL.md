# Pedido final para la expo: chat más preciso, explicaciones coherentes y ficha completa

Esta es la especificación completa del último pedido. Léela entera antes de empezar.
No se toca el batch, el streaming, Kafka ni los modelos: solo app, pedido, herramientas,
conversación, catálogo y pruebas. Todo se prueba con la muestra mini; nada en una prueba
puede tardar más de 10 minutos.

Objetivo de fondo: en la expo la gente va a escribir pedidos variados en el chat y va a
leer las tarjetas. Cada recomendación tiene que (a) corresponder a lo que se pidió y
(b) venir con una explicación que no se contradiga.

---

## 1. Años enteros

`catalogo_app` guarda `anio` con `_num(...)` y aparece como «1990.0» en carátulas,
fichas, chat y tarjetas. El año se guarda y se muestra siempre como entero («1990»).
Sin dato, no se muestra. Aplica también a los datos que recibe el LLM y a
`importar_letterboxd` («Título (1990)»). Una prueba revisa que ningún HTML de carátula,
ficha, Explorar o chat contenga un año con «.0».

## 2. Temas con evidencia fuerte y débil (incluye los ánimos)

Hoy «policial» se traduce solo al género Crime, que es demasiado amplio (5.319 películas,
muchas sin policías). Además, cada tag de un ánimo cuenta como una condición distinta,
así que «giro final» suma 3 condiciones (twist ending, plot twist, surprise ending).

Nuevo concepto, un **tema**: una condición del orden por niveles con dos tipos de
evidencia.
- **Fuerte**: algún tag del grupo del tema tiene relevancia genome ≥ 0,5 → cumple (vale 1).
- **Débil**: no hay evidencia fuerte, pero la película tiene el género señal del tema
  → cumple parcial (vale 0,5).
- Ninguna de las dos → no cumple (vale 0). Sin datos genome, solo cuenta la señal débil.

El nivel de cada película es la suma de lo que vale cada condición (los géneros pedidos
explícitamente, la duración, la persona y la época siguen valiendo 1 o 0, igual que hoy).
Se ordena por nivel de mayor a menor y, **dentro del mismo nivel, por el orden híbrido
ALS + popularidad, igual que hoy**. El corte temprano (`en_maximo`) se mantiene con el
nivel máximo posible.

Los ánimos de `pedido.ANIMOS` pasan a ser temas: sus tags forman el grupo (evidencia
fuerte) y su género pasa a ser la señal débil, en vez de una condición de género
aparte. Así «para llorar» es UNA condición, no Drama + 3 tags.

En la tarjeta y en «Entendí:» el tema aparece con su nombre en español. En
cumple / no_cumple se informa como «policial» (evidencia directa) o
«policial (solo por género Crimen)» (parcial).

## 3. Catálogo de temas

Claves en español y en inglés, con la misma tolerancia a tipeos que los géneros. Usa
solo los tags que existan en genome-tags.csv: descarta los que no existan y prueba que
cada tema conserva al menos uno.

| Tema | Claves (ejemplos) | Tags (fuerte) | Señal débil |
|---|---|---|---|
| policial | policial, policías, policiaca, detective, cop, police | police, police investigation, police corruption, undercover cop, detective, private detective, fbi | Crime |
| mafia | mafia, gánster, gangsters, crimen organizado | mafia, gangster, gangsters, organized crime | Crime |
| atracos | atraco, robo, golpe, heist | heist, bank robbery, robbery | Crime |
| espías | espías, espionaje, agente secreto, spy | spy, espionage, cia | Thriller |
| juicios | juicio, abogados, tribunal, courtroom | courtroom, courtroom drama, lawyer, lawyers | Drama |
| asesino en serie | asesino en serie, serial killer | serial killer | Thriller |
| superhéroes | superhéroes, superhero | superhero, superheroes, comic book | Action |
| zombis | zombis, zombies | zombie, zombies | Horror |
| vampiros | vampiros, vampire | vampire, vampires | Horror |
| viajes en el tiempo | viajes en el tiempo, time travel | time travel | Sci-Fi |
| extraterrestres | extraterrestres, aliens, alienígenas | alien, aliens, alien invasion, first contact | Sci-Fi |
| espacio | espacio, espacial, astronautas | space, space travel, space opera | Sci-Fi |
| distopía | distopía, postapocalíptica, fin del mundo | dystopia, post-apocalyptic, apocalypse | Sci-Fi |
| cárcel | cárcel, prisión, prison | prison, prison escape | Drama |
| deportes | deportes, deportiva, boxeo, fútbol, sports | sports, boxing, football, basketball | Drama |
| hechos reales | basada en hechos reales, historia real, true story | based on a true story, true story, biographical | Drama |
| artes marciales | artes marciales, kung fu, samurái | martial arts, kung fu, samurai | Action |
| segunda guerra | segunda guerra mundial, nazis, ww2 | world war ii, nazis | War |

«crimen» y «crime» siguen siendo el género Crime. «mafia», «gangster» y «gangsters»
salen de las claves del género Crime y pasan al tema mafia. «drama policial» =
género Drama + tema policial. «policial» ya no agrega el género Crime como condición
aparte.

## 4. Época

Una condición más (vale 1 o 0; sin año, neutral como la duración), con el año entero
del título:
- «de los 70/80/90», «años 90», «noventera» → la década;
- «de los 2000» → 2000–2009; «de 2005», «del 2005» → ese año;
- «clásica», «antigua», «vieja», «old» → antes de 1980;
- «reciente», «nueva», «moderna», «recent» → 2010 o después.

En «Entendí:» aparece como «época: años 90», «época: 2005», «época: clásica (antes de 1980)»
o «época: reciente (desde 2010)». No confundir con títulos: «2001: Odisea del espacio»
mencionado como título no es una época.

## 5. El chat usa temas y épocas

- `pedido.interpretar_pedido` devuelve `temas` y `epoca`, y `Herramientas.recomendar`
  los recibe.
- El JSON del LLM del chat (`conversacion.py`) agrega `"temas": []` y `"epoca": null`
  con vocabulario cerrado: los nombres de temas de la tabla y una época con la forma
  `{"desde": int|null, "hasta": int|null, "texto": str}`. Si el modelo propone tags
  sueltos que pertenecen a un tema, el código los convierte en ese tema; lo inválido se
  ignora. Las intenciones nuevo_pedido, ajuste y mas conservan y combinan temas y época
  igual que los géneros.
- El modo reglas hace lo mismo con `pedido.py`.
- Los chips de condiciones activas muestran temas y época, cada uno con su ×.

## 6. Similares en la ficha

Debajo de los datos de la ficha, la sección «Similares» muestra hasta 6 vecinos de
`vecinos.parquet` que el usuario no haya calificado, con carátula clicable
(`comun.caratulas_clicables`, abre esa ficha), título, año y una tarjetita con el
veredicto y una razón **del mismo `explicar()` del punto 7** (sin LLM). Sin vecinos, la
sección no aparece.

## 7. Explicaciones coherentes con el veredicto

Problema actual: una tarjeta «Puede que no» dice «Se parece a «X», que calificaste alto»,
lo que se contradice, y «se parece» no tiene umbral (Star Trek II «se parece» a Pulp
Fiction).

Reglas para `CatalogoApp.explicar` y `_texto_explicacion`:
- «Se parece a X» solo si la similitud coseno de factores ALS entre ambas es ≥ 0,75
  (constante `UMBRAL_PARECIDA = 0.75`). Medido sobre las 15.999 películas con 20+ votos:
  el 31 % de los pares tomados AL AZAR ya supera 0,3 y el percentil 95 de esos pares es
  0,735, así que 0,3 no distingue nada; 0,75 exige más parecido que el 95 % de los pares
  al azar (los 20 vecinos exactos de cada película promedian 0,85). Cumple el requisito
  «solo con coseno ≥ 0,3» con un margen honesto.
- **Probable**: solo razones a favor, máximo 2, en este orden: parecida a una que
  calificó alto; director o actor que le gustó; tags a favor; un género que califica
  sobre su promedio.
- **Puede que no**: solo razones en contra, máximo 2: parecida a una que calificó
  bajo; director o actor que no le convenció; tags en contra; un género que califica
  bajo su promedio o que casi no ve (menos del 5 % de sus calificaciones). **Nunca**
  menciona una película que calificó alto como razón.
- Sin ninguna razón alineada, una frase de respaldo verdadera: probable → «Les gusta a
  espectadores con gustos como los tuyos.»; puede que no → «Está lejos de lo que sueles
  calificar alto: tus géneros favoritos son A y B.» (sus dos géneros con mejor promedio,
  con al menos 3 calificaciones).
- En la tarjeta «Para ti», la improbable se elige entre las candidatas de menor
  afinidad (las mismas de hoy), prefiriendo, de mayor a menor votos, la primera que
  tenga al menos una razón concreta en contra (no la frase de respaldo).
- Los datos que recibe el LLM para redactar una tarjeta incluyen **solo** las razones
  alineadas con su veredicto, para que la redacción tampoco se contradiga.
- Aplica a Para ti, Explorar, la ficha y las similares.

## 8. Reseñas con la nota

Cada reseña de la ficha muestra, junto al autor, las estrellas de la última calificación
de ese usuario a esa película (estado.db), si calificó; si no, nada. Solo visual: no
cambia cómo se publican los eventos.

## 9. Rendimiento

- Similares y explicaciones en caché por (userId, movieId, versión de sus eventos).
- Con los 25M y la caché caliente: abrir una ficha con similares tarda menos de 1 s, y
  un mensaje de chat con tema y época en modo reglas («una policial de los 90») también.
- Agregar ambos pasos a `scripts/medir_app.py`. El usuario lo corre con 25M; la prueba
  usa la muestra mini.

## 10. Pruebas (muestra mini) y suite completa

- «una policial»: el top tiene primero las películas con evidencia fuerte policial y
  después las de solo Crime; dentro de cada nivel se respeta el orden híbrido.
- «drama policial con giro final»: «Entendí:» muestra drama, policial y giro final
  (3 condiciones, no 5).
- «comedia de los 90»: todas las del nivel máximo son de 1990–1999.
- Un tema con evidencia débil queda por debajo de uno con fuerte en el mismo pedido.
- JSON del LLM simulado con tags sueltos de un tema → se convierten en el tema.
- La tarjeta «Puede que no» nunca contiene «calificaste alto»; ninguna explicación dice
  «se parece» con coseno < 0,75 (UMBRAL_PARECIDA).
- Similares: hasta 6, sin calificadas, con veredicto y razón.
- Reseña con estrellas del autor.
- Años sin «.0».
- La suite completa sigue en verde.
