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

- **Toy Story** (`movieId` 1 → `tt0114709`): 1995, **81 min**, nota 8.3.
- **The Dark Knight** (58559) **no** está en title.basics: queda sin duración (año 2008 tomado del
  título de MovieLens) y con nota 9.0.
- **Groundhog Day** (1265) **no** está en title.ratings: 101 min y sin nota.
- Resultado del cruce: 25 películas, **24 con duración** y **24 con nota IMDb**.
