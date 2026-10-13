import pytest


@pytest.mark.parametrize(
    "name,expected",
    [
        ("Plátano de Canarias IGP", "platano"),
        ("Plàtan de Canàries", "platano"),
        ("Tomates rama", "tomate"),
        ("Tomàquets de penjar", "tomate"),
        ("Tomate cherry 250 g", "tomate-cherry"),
        ("Tomate frito Orlando 400 g", "tomate-frito"),
        ("Limones malla 1 kg", "limon"),
        ("Taronges de taula", "naranja"),
        ("Pomes Golden", "manzana"),
        ("Naranja zumo malla 4 kg", "naranja"),
        ("Zumo de naranja exprimido", "zumo-naranja"),
        ("Suc de taronja", "zumo-naranja"),
        ("Pechuga de pollo fileteada", "pechuga-pollo"),
        ("Pit de pollastre a filets", "pechuga-pollo"),
        ("Pollo entero limpio", "pollo-entero"),
        ("Leche entera Hacendado", "leche-entera"),
        ("Llet semidesnatada", "leche-semidesnatada"),
        ("Leche semidesnatada sin lactosa", "leche-sin-lactosa"),
        ("Aceite de oliva virgen extra", "aceite-oliva-virgen-extra"),
        ("Oli d'oliva verge extra", "aceite-oliva-virgen-extra"),
        ("Aceite de oliva suave 0,4º", "aceite-oliva"),
        ("Huevos frescos clase M", "huevos"),
        ("Ous de gallina", "huevos"),
        ("Yogur griego natural", "yogur-griego"),
        ("Nuevo yogur griego", "yogur-griego"),
        ("Atún claro en aceite de oliva", "atun-lata"),
        ("Atún rojo fresco", "atun-fresco"),
        ("Xampinyons laminats", "champinon"),
        ("Albergínies", "berenjena"),
        ("Pastanagues", "zanahoria"),
        ("Nueces peladas", "nueces"),
        ("Cerveza sin alcohol", "cerveza-sin"),
        ("Cervesa", "cerveza"),
        ("Pan de molde integral", "pan-molde-integral"),
        ("Patatas fritas onduladas", "patatas-chips"),
        ("Patatas prefritas congeladas", "patatas-prefritas"),
        ("Patatas para freír", "patata"),
    ],
)
def test_match(matcher, slug_of, name, expected):
    m = matcher.match(name)
    assert m is not None, name
    assert slug_of(m.product_id) == expected


def test_brand_is_ignored(matcher, slug_of):
    m = matcher.match("Milbona Yogur griego", brand="Milbona")
    assert slug_of(m.product_id) == "yogur-griego"


def test_generic_name_is_preferred(matcher, slug_of):
    m = matcher.match("Cola Cao Original", "Cola Cao Original")
    assert slug_of(m.product_id) == "refresco-cola"  # misleading printed name...
    m = matcher.match("cacao soluble", "Cola Cao Original")
    assert slug_of(m.product_id) == "cacao-soluble"  # ...fixed by the extractor's generic name


def test_no_match(matcher):
    assert matcher.match("Detergente líquido") is None
    assert matcher.match("") is None
    assert matcher.match(None) is None
