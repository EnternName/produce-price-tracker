import pytest

from pricetracker.units import (
    parse_number,
    parse_price,
    parse_promo,
    parse_quantity,
    quantity_from_parts,
    to_base,
    unit_price,
)


@pytest.mark.parametrize(
    "text,amount,unit",
    [
        ("500 g", 0.5, "kg"),
        ("1,5 kg", 1.5, "kg"),
        ("1.5kg", 1.5, "kg"),
        ("Malla 2 kg", 2.0, "kg"),
        ("bandeja 400 g aprox.", 0.4, "kg"),
        ("1 L", 1.0, "L"),
        ("75 cl", 0.75, "L"),
        ("750ml", 0.75, "L"),
        ("Pack 6 x 1 L", 6.0, "L"),
        ("6x330 ml", 1.98, "L"),
        ("4 x 125 g", 0.5, "kg"),
        ("330 ml x 6", 1.98, "L"),
        ("6 latas de 33 cl", 1.98, "L"),
        ("pack 6 llaunes 33 cl", 1.98, "L"),
        ("Peso neto 400 g, escurrido 240 g", 0.24, "kg"),
        ("1.000 g", 1.0, "kg"),
        ("safata 500 grams", 0.5, "kg"),
        ("1 quilo", 1.0, "kg"),
        ("ampolla 1,5 litres", 1.5, "L"),
        ("12 uds", 12.0, "ud"),
        ("1 docena", 12.0, "ud"),
        ("media docena", 6.0, "ud"),
        ("mitja dotzena", 6.0, "ud"),
        ("12 huevos", 12.0, "ud"),
        ("12 ous", 12.0, "ud"),
    ],
)
def test_parse_quantity(text, amount, unit):
    q = parse_quantity(text)
    assert q is not None, text
    assert q.unit == unit
    assert q.amount == pytest.approx(amount)


def test_range_is_averaged_and_approx():
    q = parse_quantity("pieza 1-1,5 kg")
    assert q.amount == pytest.approx(1.25)
    assert q.approx


@pytest.mark.parametrize(
    "text,unit", [("1,99 €/kg", "kg"), ("el kilo", "kg"), ("a granel", "kg"), ("al pes", "kg"),
                  ("€/l", "L"), ("la unidad", "ud"), ("per unitat", "ud"), ("c/u", "ud")]
)
def test_sold_by_measure(text, unit):
    q = parse_quantity(text)
    assert q.sold_by_measure
    assert q.unit == unit
    assert unit_price(1.99, q) == pytest.approx(1.99)


def test_eggs_with_size_spec_compare_per_unit():
    q = parse_quantity("Huevos L 12 uds (63-73 g)")
    assert q.item_count == 12
    assert unit_price(2.40, q, "ud") == pytest.approx(0.20)


def test_pack_keeps_item_count():
    q = parse_quantity("Pack 6 x 1 L")
    assert q.pack_count == 6 and q.item_count == 6
    assert unit_price(5.40, q) == pytest.approx(0.90)
    assert unit_price(5.40, q, "ud") == pytest.approx(0.90)


def test_unknown_text_returns_none():
    assert parse_quantity("oferta especial") is None
    assert parse_quantity(None) is None
    assert parse_quantity("lata") is None  # "l" must not match inside "lata"


def test_quantity_from_parts():
    q = quantity_from_parts(6, 330, "ml")
    assert q.amount == pytest.approx(1.98) and q.unit == "L" and q.item_count == 6
    q = quantity_from_parts(1, 12, "uds")
    assert q.unit == "ud" and q.item_count == 12
    assert quantity_from_parts(1, 500, "furlongs") is None
    assert quantity_from_parts(1, None, "g") is None


def test_to_base():
    assert to_base(250, "gramos") == (0.25, "kg")
    assert to_base(2, "Litres") == (2, "L")
    assert to_base(1, "dotzena") == (12, "ud")


@pytest.mark.parametrize(
    "text,value", [("1,5", 1.5), ("1.000", 1000), ("1.234,56", 1234.56), ("2'49", 2.49), ("0.89", 0.89)]
)
def test_parse_number(text, value):
    assert parse_number(text) == pytest.approx(value)


@pytest.mark.parametrize(
    "text,value", [("1,99 €", 1.99), ("2'49€", 2.49), ("€ 3,10", 3.10), ("0.89", 0.89), (1.2, 1.2), ("PVP 12,95 €/kg", 12.95)]
)
def test_parse_price(text, value):
    assert parse_price(text) == pytest.approx(value)


@pytest.mark.parametrize(
    "text,factor,min_units",
    [
        ("3x2", 2 / 3, 3),
        ("2x1", 0.5, 2),
        ("4 x 3", 0.75, 4),
        ("2ª unidad -50%", 0.75, 2),
        ("2ª unidad al 70%", 0.65, 2),
        ("2a unitat al 50%", 0.75, 2),
        ("-50% en la 2ª unidad", 0.75, 2),
        ("50% dto. 2ª ud.", 0.75, 2),
        ("3ª unidad gratis", 2 / 3, 3),
        ("3a unitat de regal", 2 / 3, 3),
        ("segunda unidad a mitad de precio", 0.75, 2),
        ("segona unitat al 50%", 0.75, 2),
    ],
)
def test_multibuy_promos(text, factor, min_units):
    promo = parse_promo(text)
    assert promo is not None, text
    assert promo.factor == pytest.approx(factor)
    assert promo.min_units == min_units


def test_plain_discount_does_not_change_effective_price():
    promo = parse_promo("-30%")
    assert promo.kind == "discount" and promo.factor == 1.0


def test_quantity_is_not_a_promo():
    assert parse_promo("6 x 1 l") is None
    assert parse_promo("3 x 2,5 kg") is None


def test_loyalty_only_price():
    promo = parse_promo("Con Lidl Plus 3x2")
    assert promo.loyalty_only and promo.kind == "multibuy"
    assert parse_promo("Precio Club Dia").loyalty_only
    assert parse_promo("sin descuento") is None
