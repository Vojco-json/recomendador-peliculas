# Muestra mini de MovieLens (ml-mini)

Muestra pequeña y **fija** con el mismo formato que `ml-25m` (mismos archivos, columnas,
encabezados y comillas). Se usa en las pruebas automáticas y en los scripts con
`--datos muestras/ml-mini`. Los ratings son **sintéticos**, diseñados para tener valores conocidos.
Las películas son reales: `movieId`, título, géneros, `imdbId` y `tmdbId` son los de ml-25m.

## Conteos totales

| Archivo | Filas (sin encabezado) |
|---|---|
| ratings.csv | **300** (15 usuarios × 20 ratings) |
| movies.csv | **25** |
| links.csv | **25** |
| tags.csv | **13** |
| genome-tags.csv | **10** |
| genome-scores.csv | **250** (25 películas × 10 tags) |

- Usuarios: **15** (userId 1 a 15). Películas calificadas: **25** (todas).
- Ratings entre **2010-01-01 y 2019-12-31** (mínimo 1262404000 = 2010-01-02, máximo 1547869200 = 2019-01-19).
- Escala de 0.5 a 5.0 en pasos de 0.5, como ml-25m.

## Partición temporal (corte 2016-01-01 = epoch 1451606400)

| Conjunto | Ratings |
|---|---|
| Antes del corte (entrenamiento) | **210** (14 por usuario) |
| Desde el corte (prueba / eventos del productor) | **90** (6 por usuario) |
| Desde 2018-01-01 | **30** (2 por usuario) |

Todas las películas y usuarios de la prueba aparecen en el entrenamiento (con
`coldStartStrategy='drop'` no se descarta ningún rating).

## Modo demo (DEMO_DESDE = 2015-01-01 = epoch 1420070400)

- Ratings `>= 2015-01-01` y anteriores al corte 2016-01-01: **90** (6 por usuario). Es el
  tamaño esperado de `base_demo.parquet` al preparar la demo sobre la muestra con
  `--desde 2015-01-01` y corte 2016-01-01.
- Ratings `>= 2015-01-01` en total: **180**.

## Popularidad antes del corte

**Toy Story (1995)** (`movieId` 1) es la más popular: la califican los **15** usuarios antes del
corte (media 3.6). Le siguen con **11** Pulp Fiction (296) y Matrix (2571), y con **10**
Braveheart (110), Star Wars IV (260), Jurassic Park (480), Terminator 2 (589) y Die Hard (1036).

## Grupos de usuarios (gustos conocidos)

| Usuarios | Orientación | Qué califican alto | Qué califican bajo |
|---|---|---|---|
| 1, 3, 5, 7, 9, 11 | **Action / Thriller** | Heat, Die Hard, Matrix, T2, Seven… | Titanic, Forrest Gump, animación |
| 2, 4, 6, 8, 10 | **Romance / Drama** | Titanic, Sleepless in Seattle, Shawshank, Forrest Gump… | acción y thrillers |
| 12, 13, 14, 15 | **Familia / Animación** | Toy Story, Lion King, Shrek, Finding Nemo | Pulp Fiction |

- **Usuario 1**: con pesos `rating - media del usuario` (media 3.875), sus 2 géneros principales son
  **Thriller (6.625)** y **Action (6.5)**. Usando solo los ratings anteriores al corte también
  quedan Action y Thriller arriba.
- **Usuario 2**: con el mismo cálculo (media 3.275), sus 2 géneros principales son
  **Romance (8.075)** y **Drama (6.575)**.

## tags y genome

- `tags.csv`: 13 tags libres de usuarios 1, 2, 3, 12, 13 y 14 (timestamp = rating + 60 s).
- `genome-tags.csv` es un **subconjunto reducido de 10 tags con tagId renumerado 1 a 10**
  (action, romance, funny, dark, animation, classic, twist ending, family, violence, feel-good).
  Los tagId **no** coinciden con la numeración original de ml-25m; las relevancias de
  `genome-scores.csv` son valores plausibles escritos a mano, no copiados de ml-25m.
