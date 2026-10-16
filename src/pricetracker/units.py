"""Parse quantities, prices and multi-buy promotions from Spanish / Catalan grocery text.

Everything is normalized to one of three base units so offers can be compared:
``kg`` (weight), ``L`` (volume) and ``ud`` (countable units, e.g. eggs).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .textnorm import fold

BASE_UNITS = ("kg", "L", "ud")

# folded unit word -> (base unit, factor to convert one of it into the base unit)
UNIT_ALIASES: dict[str, tuple[str, float]] = {
    # weight
    "mg": ("kg", 1e-6),
    "g": ("kg", 1e-3), "gr": ("kg", 1e-3), "grs": ("kg", 1e-3), "gramo": ("kg", 1e-3),
    "gramos": ("kg", 1e-3), "gram": ("kg", 1e-3), "grams": ("kg", 1e-3),
    "kg": ("kg", 1.0), "kgs": ("kg", 1.0), "kilo": ("kg", 1.0), "kilos": ("kg", 1.0),
    "quilo": ("kg", 1.0), "quilos": ("kg", 1.0), "kilogramo": ("kg", 1.0),
    "kilogramos": ("kg", 1.0), "quilogram": ("kg", 1.0), "quilograms": ("kg", 1.0),
    # volume
    "ml": ("L", 1e-3), "mililitro": ("L", 1e-3), "mililitros": ("L", 1e-3),
    "millilitre": ("L", 1e-3), "millilitres": ("L", 1e-3),
    "cl": ("L", 1e-2), "centilitro": ("L", 1e-2), "centilitros": ("L", 1e-2),
    "centilitre": ("L", 1e-2), "centilitres": ("L", 1e-2),
    "dl": ("L", 1e-1),
    "l": ("L", 1.0), "lt": ("L", 1.0), "lts": ("L", 1.0), "ltr": ("L", 1.0),
    "litro": ("L", 1.0), "litros": ("L", 1.0), "litre": ("L", 1.0), "litres": ("L", 1.0),
    # count
    "ud": ("ud", 1.0), "uds": ("ud", 1.0), "u": ("ud", 1.0), "un": ("ud", 1.0),
    "unid": ("ud", 1.0), "unidad": ("ud", 1.0), "unidades": ("ud", 1.0),
    "unitat": ("ud", 1.0), "unitats": ("ud", 1.0),
    "pieza": ("ud", 1.0), "piezas": ("ud", 1.0), "pza": ("ud", 1.0), "pzas": ("ud", 1.0),
    "peca": ("ud", 1.0), "peces": ("ud", 1.0),
    "docena": ("ud", 12.0), "docenas": ("ud", 12.0),
    "dotzena": ("ud", 12.0), "dotzenes": ("ud", 12.0),
}

# Words that name a countable container/item ("6 latas de 33 cl", "12 huevos").
COUNT_NOUNS = (
    "latas", "lata", "llaunes", "llauna", "botellas", "botella", "ampolles", "ampolla",
    "bricks", "brick", "briks", "brik", "envases", "envase", "envasos", "envas",
    "botes", "bote", "pots", "pot", "tarrinas", "tarrina", "sobres", "sobre",
    "bolsitas", "bolsas", "bosses", "bossetes", "paquetes", "paquets", "rollos", "rotllos",
    "capsulas", "capsules", "huevos", "ous", "yogures", "iogurts", "piezas", "peces",
    "raciones", "racions", "barritas", "barretes", "porciones", "porcions",
    "unidades", "unitats", "uds", "ud", "u", "un",
)

_NUM = r"\d+(?:[.,']\d+)?"
_UNIT_RE = "|".join(sorted((re.escape(u) for u in UNIT_ALIASES), key=len, reverse=True))
_COUNT_RE = "|".join(sorted((re.escape(w) for w in COUNT_NOUNS), key=len, reverse=True))
_X = r"\s*(?:x|×|\*)\s*"
_END = r"(?![a-z0-9])"  # unit must not be the prefix of a longer word ("l" in "lata")

_AMOUNT = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}")
_RANGE = re.compile(rf"(?P<lo>{_NUM})\s*(?:-|a|–)\s*(?P<hi>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}")
_PACK = re.compile(
    rf"(?P<count>\d+)\s*(?:(?:{_COUNT_RE}){_END}\s*)?(?:x|×|\*|de)\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}"
)
_PACK_REVERSED = re.compile(rf"(?P<num>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}{_X}(?P<count>\d+){_END}(?!\s*[.,]?\d)")
_PACK_NOUN = re.compile(rf"(?P<count>\d+)\s*(?:{_COUNT_RE}){_END}\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}")
_COUNT_ONLY = re.compile(rf"(?P<count>\d+)\s*(?:{_COUNT_RE}){_END}")
_DOZEN = re.compile(r"(?:(?P<n>\d+)\s*)?(?P<half>media|mitja)?\s*(?:docenas?|dotzenes|dotzena)\b")
_DRAINED = re.compile(
    rf"(?:escurrido|escurr\.|esc\.|escorregut|escorr\.)\s*:?\s*(?P<num>{_NUM})\s*(?P<unit>{_UNIT_RE}){_END}"
    rf"|(?P<num2>{_NUM})\s*(?P<unit2>{_UNIT_RE}){_END}\s*(?:peso\s+)?(?:escurrido|escorregut)"
)
_PER_WEIGHT = re.compile(r"(?:€|eur|euros?)?\s*/\s*(?:kg|kilo|quilo)\b|\b(?:el|al|por|per|cada)\s+(?:kg|kilo|quilo)\b|\bgranel\b|\bal pes\b")
_PER_LITRE = re.compile(r"(?:€|eur|euros?)?\s*/\s*(?:l|litro|litre)\b|\b(?:el|por|per)\s+(?:litro|litre)\b")
_PER_UNIT = re.compile(
    r"(?:€|eur|euros?)?\s*/\s*(?:ud|u|unidad|unitat|pieza|peca)\b|\b(?:la|por|per|cada)\s+(?:unidad|unitat|pieza|peca)\b"
    r"|\bc/u\b|\bcada uno\b|\bcada una\b"
)
_APPROX = re.compile(r"\baprox|\bapprox|\baproximat|~|\bc\.a\.")


def parse_number(text: str) -> float:
    """Parse a Spanish-formatted number: '1,5' -> 1.5, '1.000' -> 1000, "1'99" -> 1.99."""
    text = text.strip().replace("'", ",")
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+", text):  # thousands separators
        return float(text.replace(".", ""))
    if "," in text and "." in text:  # 1.234,56
        return float(text.replace(".", "").replace(",", "."))
    return float(text.replace(",", "."))


@dataclass(frozen=True)
class Quantity:
    """Total quantity of one sellable item, in a base unit."""

    amount: float  # total, in `unit`
    unit: str  # "kg" | "L" | "ud"
    pack_count: int = 1  # number of sub-items (6 in "6 x 1 L")
    approx: bool = False  # weight is approximate ("pieza 1,2 kg aprox.")
    sold_by_measure: bool = False  # price is per kg / L / ud itself ("1,99 €/kg")
    item_count: int | None = None  # countable items when stated ("12 huevos", "6 latas")

    def amount_in(self, base: str) -> float | None:
        """Total amount expressed in `base`, if this quantity can be expressed in it."""
        if base == self.unit:
            return self.amount
        if base == "ud" and self.item_count:
            return float(self.item_count)
        return None

    def describe(self) -> str:
        prefix = "~" if self.approx else ""
        text = f"{prefix}{self.amount:g} {self.unit}"
        if self.unit != "ud" and self.item_count and self.item_count > 1:
            text += f" ({self.item_count} ud)"
        return text


def to_base(amount: float, unit: str) -> tuple[float, str] | None:
    """Convert an amount in any recognized unit (Spanish/Catalan names ok) to (amount, base unit)."""
    alias = UNIT_ALIASES.get(fold(unit).strip(". "))
    if alias is None:
        return None
    base, factor = alias
    return amount * factor, base


def _base(num: str, unit: str) -> tuple[float, str]:
    converted = to_base(parse_number(num), unit)
    assert converted is not None  # regexes only match known units
    return converted


def _measure(t: str) -> tuple[float, str, int, bool] | None:
    """Weight/volume in `t` as (amount, base, pack_count, is_range); count-only units are ignored."""
    if m := _DRAINED.search(t):  # canned goods: compare on drained weight
        num, unit = (m["num"], m["unit"]) if m["num"] else (m["num2"], m["unit2"])
        amount, base = _base(num, unit)
        return amount, base, 1, False
    for pattern in (_PACK, _PACK_NOUN, _PACK_REVERSED):
        for m in pattern.finditer(t):
            count = int(m["count"])
            amount, base = _base(m["num"], m["unit"])
            if count > 0 and base != "ud":
                return amount * count, base, count, False
    for m in _RANGE.finditer(t):
        lo, base = _base(m["lo"], m["unit"])
        hi, _ = _base(m["hi"], m["unit"])
        if base != "ud":
            return (lo + hi) / 2, base, 1, True
    for m in _AMOUNT.finditer(t):
        amount, base = _base(m["num"], m["unit"])
        if base != "ud":
            return amount, base, 1, False
    return None


def _count(t: str) -> int | None:
    if m := _DOZEN.search(t):
        n = int(m["n"]) if m["n"] else 1
        return int(12 * n * (0.5 if m["half"] else 1))
    if m := _COUNT_ONLY.search(t):
        return int(m["count"]) or None
    for pattern in (_PACK, _PACK_NOUN, _PACK_REVERSED):
        if m := pattern.search(t):
            return int(m["count"]) or None
    return None


def parse_quantity(text: str | None) -> Quantity | None:
    """Parse a quantity/format string such as '6 x 1 L', '500 g', 'malla 2 kg', '1 docena', '€/kg'."""
    if not text:
        return None
    t = fold(text)
    approx = bool(_APPROX.search(t))
    count = _count(t)

    if measure := _measure(t):
        amount, base, pack_count, is_range = measure
        return Quantity(amount, base, pack_count, approx or is_range, item_count=count)
    if count:
        return Quantity(float(count), "ud", pack_count=count, item_count=count)
    if _PER_WEIGHT.search(t):
        return Quantity(1.0, "kg", approx=approx, sold_by_measure=True)
    if _PER_LITRE.search(t):
        return Quantity(1.0, "L", sold_by_measure=True)
    if _PER_UNIT.search(t):
        return Quantity(1.0, "ud", sold_by_measure=True, item_count=1)
    return None


def quantity_from_parts(
    pack_count: int | None,
    unit_amount: float | None,
    unit: str | None,
    *,
    approx: bool = False,
    item_count: int | None = None,
) -> Quantity | None:
    """Build a Quantity from structured fields (e.g. what the flyer extractor returns)."""
    if unit_amount is None or not unit or unit_amount <= 0:
        return None
    converted = to_base(unit_amount, unit)
    if converted is None:
        return None
    amount, base = converted
    count = pack_count if pack_count and pack_count > 0 else 1
    if base == "ud":
        item_count = int(amount * count)
    elif item_count is None and count > 1:
        item_count = count
    return Quantity(amount * count, base, pack_count=count, approx=approx, item_count=item_count)


_PRICE = re.compile(r"(?P<num>\d+(?:[.,']\d{1,2})?)\s*(?:€|eur\b|euros?\b)|(?:€|eur)\s*(?P<num2>\d+(?:[.,']\d{1,2})?)")


def parse_price(text: str | float | int | None) -> float | None:
    """'1,99 €' -> 1.99, "2'49€" -> 2.49, '0.89' -> 0.89."""
    if text is None:
        return None
    if isinstance(text, (int, float)):
        return float(text)
    t = fold(text)
    if m := _PRICE.search(t):
        return parse_number(m["num"] or m["num2"])
    if m := re.search(_NUM, t):
        return parse_number(m.group(0))
    return None


def unit_price(price: float, quantity: Quantity | None, base: str | None = None) -> float | None:
    """Price per base unit (EUR/kg, EUR/L, EUR/ud); `base` defaults to the quantity's own unit."""
    if quantity is None:
        return None
    if quantity.sold_by_measure and (base is None or base == quantity.unit):
        return price
    amount = quantity.amount_in(base or quantity.unit)
    if not amount or amount <= 0:
        return None
    return price / amount


# --- promotions --------------------------------------------------------------------------------

_LOYALTY = re.compile(
    r"\b(?:lidl plus|club dia|mi dia|con tarjeta|amb targeta|mi alcampo|club carrefour|carrefour club"
    r"|eroski club|mundo consum|mon consum|socios?|socis?|cupon|cupo|app|targeta bonpreu|precio socio)\b"
)

_ORD = r"(?P<n>[2-5])\s*(?:ª|a|º|o|da|na|ra|ta)?\.?"
_UNIT_WORD = r"(?:unidad|unitat|ud|u)\.?"
_MULTIBUY = re.compile(rf"(?<![\d.,])(?P<buy>[2-9])\s*(?:x|×)\s*(?P<pay>[1-8])(?![\d.,])(?!\s*(?:{_UNIT_RE}){_END})")
_NTH_PCT = re.compile(rf"{_ORD}\s*{_UNIT_WORD}\s*(?:al|a|con|amb|-|:)?\s*-?\s*(?P<pct>\d{{1,3}})\s*%")
_PCT_NTH = re.compile(rf"-?\s*(?P<pct>\d{{1,3}})\s*%\s*(?:de\s+)?(?:descuento|dto\.?|descompte)?\s*(?:en|a)?\s*(?:la)?\s*{_ORD}\s*{_UNIT_WORD}")
_NTH_FREE = re.compile(rf"{_ORD}\s*{_UNIT_WORD}\s*(?:gratis|gratuita|de regal|regal)")
_NTH_HALF = re.compile(r"(?P<w>segunda|segona)\s+(?:unidad|unitat)\s+(?:a\s+)?(?:mitad|meitat)\s+de\s+(?:precio|preu)")
_WORD_ORD = {"segunda": 2, "segona": 2, "tercera": 3}
_NTH_WORD_PCT = re.compile(r"(?P<w>segunda|segona|tercera)\s+(?:unidad|unitat)\s*(?:al|a|con|amb|-)?\s*-?\s*(?P<pct>\d{1,3})\s*%")
_PLAIN_PCT = re.compile(r"-\s*(?P<pct>\d{1,2})\s*%")


@dataclass(frozen=True)
class Promo:
    kind: str  # "multibuy" | "nth_unit" | "discount" | "loyalty"
    factor: float = 1.0  # average price multiplier per unit when buying `min_units`
    min_units: int = 1
    loyalty_only: bool = False  # price needs a loyalty card/app (Lidl Plus, Club Dia, ...)
    label: str = ""


def _nth_unit(n: int, pct: float, loyalty: bool) -> Promo:
    pct = max(0.0, min(pct, 100.0))
    factor = (n - 1 + (1 - pct / 100)) / n
    return Promo("nth_unit", factor, n, loyalty, f"{n}ª unidad -{pct:g}%")


def parse_promo(text: str | None) -> Promo | None:
    """Recognize Spanish/Catalan multi-buy promos.

    '3x2' -> pay 2 of 3; '2ª unidad -50%' / '2a unitat al 50%' -> 50% off the 2nd unit (in Spanish
    retail "al 70%" means 70% *discount*); '3ª unidad gratis'. A bare '-30%' is informational: flyers
    print the already-discounted price, so it does not change the effective price.
    """
    if not text:
        return None
    t = fold(text)
    loyalty = bool(_LOYALTY.search(t))

    if m := _NTH_FREE.search(t):
        return _nth_unit(int(m["n"]), 100, loyalty)
    if m := _NTH_HALF.search(t):
        return _nth_unit(_WORD_ORD[m["w"]], 50, loyalty)
    if m := _NTH_WORD_PCT.search(t):
        return _nth_unit(_WORD_ORD[m["w"]], float(m["pct"]), loyalty)
    if m := (_NTH_PCT.search(t) or _PCT_NTH.search(t)):
        return _nth_unit(int(m["n"]), float(m["pct"]), loyalty)
    if m := _MULTIBUY.search(t):
        buy, pay = int(m["buy"]), int(m["pay"])
        if pay < buy:
            return Promo("multibuy", pay / buy, buy, loyalty, f"{buy}x{pay}")
    if m := _PLAIN_PCT.search(t):
        return Promo("discount", 1.0, 1, loyalty, f"-{m['pct']}%")
    if loyalty:
        return Promo("loyalty", 1.0, 1, True, "precio socio")
    return None
