# Muestra mini de IMDb (imdb-mini)

Recorte con el formato exacto de los datasets no comerciales de IMDb
(https://datasets.imdbws.com/, TSV con tabulaciones y `\N` como nulo), sin comprimir.
Sirve para probar el cruce con `muestras/ml-mini` sin descargar nada:

    .venv/Scripts/python scripts/descargar_imdb.py --sin-descargar --imdb muestras/imdb-mini \
        --datos muestras/ml-mini --salida artefactos/mini

Las duraciones, notas y votos son **aproximados** (valores de referencia para pruebas, no una
copia actualizada de IMDb). Los datos de IMDb son solo para uso personal y no comercial.

## Valores conocidos

| Archivo | Filas | Contenido |
|---|---|---|
| title.basics.tsv | 27 | 24 películas de ml-mini + Carmencita (short), Breaking Bad (tvSeries) y una fila sin datos |
| title.ratings.tsv | 26 | 24 películas de ml-mini + Carmencita y Breaking Bad |
| title.crew.tsv | 7 | directores de 6 películas de ml-mini + Breaking Bad |
| title.principals.tsv | 19 | reparto y directores de 6 películas + Breaking Bad |
| name.basics.tsv | 26 | nombres de esas personas |

- **Toy Story** (`movieId` 1 → `tt0114709`): 1995, **81 min**, nota 8.3.
- **The Dark Knight** (58559) **no** está en title.basics: queda sin duración (año 2008 tomado del
  título de MovieLens) y con nota 9.0.
- **Groundhog Day** (1265) **no** está en title.ratings: 101 min y sin nota.
- Resultado del cruce: 25 películas, **24 con duración** y **24 con nota IMDb**.

## Personas (title.crew, title.principals, name.basics)

Filas agregadas para `scripts/precalcular_app.py --perfil mini` (personas.parquet):

- **The Dark Knight** (58559 → `tt0468569`): dirigida por **Christopher Nolan** (`nm0634240`),
  con Christian Bale y Heath Ledger. Hans Zimmer figura como `composer` (no es actor ni director).
- **Tom Hanks** actúa en Toy Story (1) y Forrest Gump (356).
- **Breaking Bad** (`tt0903747`, Bryan Cranston, Vince Gilligan) no está en links.csv: no debe
  aparecer en personas.parquet (se filtra por los tconst de links.csv antes del cruce).
- `nm9999901` «Penélope Cruz Sánchez» en Pulp Fiction es una **fila ficticia de prueba** para
  comprobar el nombre normalizado sin tildes (`penelope cruz sanchez`).
- Resultado: **20 filas** (7 directores y 13 actores) en 6 películas.
