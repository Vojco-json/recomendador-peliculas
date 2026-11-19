"""Interpretación del pedido (recomendador/pedido.py): ES/EN, tipeo, ánimos y ocasiones."""

import pytest

from recomendador import temas

from recomendador import config, herramientas, llm
from recomendador.pedido import cargar_tags_genome, interpretar_pedido

TAGS = {"sad", "tearjerker", "emotional", "family", "feel-good", "funny",
        "thought-provoking", "philosophical", "twist ending", "plot twist", "scary"}


def p(texto, tags=TAGS):
    return interpretar_pedido(texto, tags_validos=tags)


# C1: ánimos y ocasiones -> UN tema cada uno (PEDIDO_FINAL.md §2)
@pytest.mark.parametrize("texto, tema", [
    ("algo para llorar", "para llorar"),
    ("una película para ver en familia", "para ver en familia"),
    ("algo liviano", "algo liviano"),
    ("algo que me haga pensar", "que haga pensar"),
    ("una con giro final", "giro final"),
    ("something scary", "que dé miedo"),
    ("a feel good movie", "algo liviano"),
    ("family movie tonight", "para ver en familia"),
])
def test_animos(texto, tema):
    r = p(texto)
    assert r["temas"] == [tema]
    assert r["tags_genome"] == []          # los tags del ánimo no son condiciones sueltas
    assert r["pide_recomendacion"]


# C2: inglés y errores de tipeo
@pytest.mark.parametrize("texto, genero", [
    ("a comedy please", "Comedy"),
    ("una comdia", "Comedy"), ("algo de terorr", "Horror"), ("algo de teror", "Horror"),
    ("ciensia ficcion", "Sci-Fi"), ("Ciencia Ficción", "Sci-Fi"),
    ("una de ACCIÓN", "Action"), ("suspenso", "Thriller"),
])
def test_generos_es_en_tipeo(texto, genero):
    assert genero in p(texto)["generos"]


@pytest.mark.parametrize("texto, minutos", [
    ("algo corto", 100), ("something short", 100), ("menos de 90 min", 90),
    ("under 95 minutes", 95), ("menos de 2 horas", 120), ("una comedia", None),
])
def test_duracion(texto, minutos):
    assert p(texto)["max_duracion_min"] == minutos


def test_sin_falsos_positivos():
    assert p("hubo un error en la app")["generos"] == []      # 'error' no es 'terror'
    assert p("un deporte")["max_duracion_min"] is None
    assert p("una comida rica")["generos"] == []              # 'comida' no es 'comedia'


# C3: título mencionado y saludos
@pytest.mark.parametrize("texto, titulo", [
    ("algo como Toy Story", "Toy Story"),
    ("quiero una película parecida a Matrix, pero corta", "Matrix"),
    ("something like The Dark Knight", "The Dark Knight"),
    ("algo similar a «Pulp Fiction» para ver en familia", "Pulp Fiction"),
])
def test_titulo_mencionado(texto, titulo):
    r = p(texto)
    assert r["titulo_mencionado"] == titulo and r["pide_recomendacion"]


def test_titulo_no_cuenta_como_genero():
    assert "War" not in p("algo como Star Wars")["generos"]


@pytest.mark.parametrize("texto", ["hola", "Hola, ¿cómo estás?", "gracias!", "hello",
                                   "¿Qué es MovieLens?", "¿cómo funciona esto?"])
def test_saludos_no_piden(texto):
    r = p(texto)
    assert r["pide_recomendacion"] is False and r["titulo_mencionado"] is None


@pytest.mark.parametrize("texto", ["recomiéndame algo", "dame 3 películas",
                                   "what should I watch?"])
def test_pedidos_generales(texto):
    assert p(texto)["pide_recomendacion"] is True


# C4: condiciones en español y tags filtrados por genome-tags.csv
def test_condiciones_es_y_tags_filtrados():
    mini = cargar_tags_genome(config.MUESTRA_MINI)
    assert "twist ending" in mini and "sad" not in mini
    r = interpretar_pedido("algo triste con giro final, corto", tags_validos=mini)
    assert r["temas"] == ["para llorar", "giro final"]
    assert temas.tags_del_tema("giro final", mini) == ["twist ending"]
    assert "sad" not in temas.tags_del_tema("para llorar", mini)
    assert set(r["tags_genome"]) <= mini
    assert r["condiciones"] == ["género: drama", "de 100 min o menos",
                                "para emocionarse (drama triste)", "con giro final"]
    r = interpretar_pedido("algo para llorar", datos=config.MUESTRA_MINI)
    assert r["tags_genome"] == []                 # descartados sin error


def test_n_y_constantes():
    assert p("dame 3 comedias")["n"] == 3
    from recomendador import pedido
    assert pedido.DURACION_CORTA_MIN == herramientas.DURACION_CORTA_MIN
    assert pedido.N_DEFECTO == herramientas.N_DEFECTO


# C5: llm.interpretar delega en pedido.py y conserva su forma
def test_llm_interpretar_forma():
    f = llm.interpretar("Algo CORTO y liviano")
    assert set(f) == {"max_duracion_min", "generos", "n"}
    assert f["max_duracion_min"] == 100
    assert "Horror" in llm.interpretar("algo de terorr")["generos"]
    assert "Sci-Fi" in llm.interpretar("a sci-fi movie")["generos"]
