"""Pruebas del pedido final (PEDIDO_FINAL.md §1-§8), sin artefactos de 25M."""
from recomendador import config, temas
from recomendador.catalogo_app import Catalogo, _entero
from recomendador.pedido import cargar_tags_genome, interpretar_pedido


def test_drama_policial_giro_son_tres_condiciones():
    p = interpretar_pedido("drama policial con giro final", tags_validos=set())
    assert p["generos"] == ["Drama"] and p["temas"] == ["policial", "giro final"]
    assert p["condiciones"] == ["género: drama", "tema: policial", "tema: giro final"]


def test_epoca_decadas_anios_clasica_reciente():
    def ep(t):
        return interpretar_pedido(t, tags_validos=set())["epoca"]
    assert ep("comedia de los 90") == {"desde": 1990, "hasta": 1999, "texto": "años 90"}
    assert ep("algo de los 2000")["hasta"] == 2009
    assert ep("una del 2005") == {"desde": 2005, "hasta": 2005, "texto": "2005"}
    assert ep("una clásica")["hasta"] == 1979 and ep("algo reciente")["desde"] == 2010
    assert ep("algo como 2001: Odisea del espacio") is None     # título, no época
    assert ep("menos de 90 minutos") is None


def test_catalogo_de_temas_con_tags_de_genome():
    assert len(temas.TEMAS_CATALOGO) == 18
    validos = cargar_tags_genome(config.DATOS) or cargar_tags_genome(config.MUESTRA_MINI)
    if len(validos) > 100:                          # genome completo disponible
        for t in temas.TEMAS:
            assert temas.tags_del_tema(t, validos), t
    assert "policial" not in interpretar_pedido("una policial", tags_validos=set())["generos"]


def test_fuerte_vale_mas_que_debil():
    genome = {"police": {1: 0.9}}
    fuerte = temas.evaluar_tema("policial", {"movieId": 1, "generos": ["Drama"]}, genome)
    debil = temas.evaluar_tema("policial", {"movieId": 2, "generos": ["Crime"]}, genome)
    nada = temas.evaluar_tema("policial", {"movieId": 3, "generos": ["Comedy"]}, genome)
    assert fuerte == (1.0, "policial")
    assert debil == (0.5, "policial (solo por género Crimen)")
    assert nada[0] == 0.0


def test_tags_sueltos_se_convierten_en_tema():
    assert temas.tema_de_tag("twist ending") == "giro final"
    assert temas.tema_de_tag("serial killer") == "asesino en serie"
    assert temas.nombre_tema("cop") == "policial"
    assert temas.validar_epoca({"desde": 1990, "hasta": 1999})["texto"] == "años 90"
    assert temas.validar_epoca({"desde": "x"}) is None


def _exp(probable, **kw):
    base = {"veredicto": "Probablemente te guste" if probable else "Puede que no",
            "probable": probable, "parecida_alta": None, "parecida_baja": None,
            "tags_a_favor": [], "tags_en_contra": [], "persona_comun": None,
            "generos_favoritos": ["drama", "crimen"]}
    return {**base, **kw}


def test_puede_que_no_nunca_cita_calificada_alto():
    e = _exp(False, parecida_alta={"titulo": "Pulp Fiction", "rating": 5})
    texto = Catalogo._texto_explicacion(e)
    assert "calificaste alto:" not in texto.replace("sueles calificar alto:", "")
    assert "Pulp Fiction" not in texto
    assert "tus géneros favoritos son drama y crimen" in texto
    assert Catalogo._texto_explicacion(_exp(True)).endswith(
        "Les gusta a espectadores con gustos como los tuyos.")


def test_anios_enteros_y_resena_con_estrellas():
    assert _entero(1990.0) == 1990 and str(_entero(1990.0)) == "1990"
    assert _entero(None) is None
    from app.vistas.tarjetas import resenas_html
    html = resenas_html([{"autor": "ana", "texto": "buena", "timestamp": 0, "rating": 4.5}])
    assert "★★★★½" in html
    assert "★" not in resenas_html([{"autor": "b", "texto": "x", "timestamp": 0,
                                      "rating": None}])
