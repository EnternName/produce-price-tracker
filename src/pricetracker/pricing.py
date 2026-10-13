"""Turn a raw offer (from a flyer, an online shop or a CSV row) into a normalized price observation."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, fields

from .matching import Matcher
from .units import Quantity, parse_promo, parse_quantity, quantity_from_parts, to_base, unit_price

# Printed and computed EUR/kg may differ by rounding; beyond this the offer goes to review.
UNIT_PRICE_TOLERANCE = 0.12


@dataclass
class RawOffer:
    raw_name: str
    price: float
    generic_name: str | None = None  # e.g. "leche entera" (the extractor translates Catalan to Spanish)
    brand: str | None = None
    category_hint: str | None = None
    quantity_text: str | None = None
    pack_count: int | None = None
    unit_amount: float | None = None  # size of one item, e.g. 330 for "6 x 330 ml"
    unit: str | None = None  # unit of `unit_amount` (g, kg, ml, l, ud...)
    price_basis: str = "item"  # "item" or "per_kg" / "per_l" / "per_unit" when the price is per measure
    approx: bool = False
    regular_price: float | None = None
    printed_unit_price: float | None = None
    printed_unit: str | None = None
    promo_text: str | None = None
    external_id: str | None = None
    page: int | None = None
    valid_from: str | None = None
    valid_to: str | None = None
    product_id: int | None = None  # pre-assigned product (skips matching)


_BASIS_UNIT = {"per_kg": "kg", "per_l": "L", "per_unit": "ud"}


def resolve_quantity(offer: RawOffer) -> Quantity | None:
    if offer.price_basis in _BASIS_UNIT:
        base = _BASIS_UNIT[offer.price_basis]
        return Quantity(1.0, base, approx=offer.approx, sold_by_measure=True, item_count=1 if base == "ud" else None)
    structured = quantity_from_parts(offer.pack_count, offer.unit_amount, offer.unit, approx=offer.approx)
    parsed = parse_quantity(offer.quantity_text)
    if structured and parsed and structured.item_count is None and parsed.item_count:
        structured = Quantity(
            structured.amount, structured.unit, structured.pack_count, structured.approx,
            structured.sold_by_measure, parsed.item_count,
        )
    return structured or parsed or parse_quantity(offer.raw_name)


def printed_unit_price_in(printed_price: float | None, printed_unit: str | None, base: str) -> float | None:
    """A store's printed unit price ("2,90 €/kg", "0,45 €/100 g") converted to EUR per `base`."""
    if printed_price is None or not printed_unit:
        return None
    text = printed_unit.replace("€", " ").replace("/", " ").strip()
    converted = to_base(1.0, text)
    if converted is None:
        q = parse_quantity(text)
        converted = (q.amount, q.unit) if q and not q.sold_by_measure else None
    if converted is None:
        return None
    amount, unit = converted
    if unit != base or amount <= 0:
        return None
    return printed_price / amount


def compute_prices(row: dict, base_unit: str | None) -> dict:
    """(Re)compute unit price fields of an observation row for the product's base unit."""
    q = None
    if row.get("amount") is not None and row.get("unit"):
        q = Quantity(row["amount"], row["unit"], approx=bool(row.get("approx")),
                     sold_by_measure=bool(row.get("sold_by_measure")), item_count=row.get("item_count"))
    target = base_unit or (q.unit if q else None)
    notes: list[str] = []
    up = unit_price(row["price"], q, target) if (q and target) else None

    printed = row.get("_printed_in_target")
    if up is None and printed is not None:
        up = printed
        notes.append("unit price taken from the store's printed price")
    elif up is not None and printed is not None and printed > 0:
        if abs(up - printed) / printed > UNIT_PRICE_TOLERANCE:
            notes.append(f"computed {up:.2f} vs printed {printed:.2f} €/{target}")
    if up is None:
        notes.append("could not compute a unit price" + (f" in {target}" if target else ""))

    promo = parse_promo(row.get("promo_text"))
    effective = up * promo.factor if (up is not None and promo) else up
    if promo and promo.factor != 1.0 and row.get("sold_by_measure"):
        notes.append("multi-buy promo on a per-kg price")

    row.update(
        price_unit=target,
        unit_price=round(up, 4) if up is not None else None,
        effective_unit_price=round(effective, 4) if effective is not None else None,
        promo_kind=promo.kind if promo else None,
        promo_min_units=promo.min_units if promo else None,
        loyalty_only=int(promo.loyalty_only) if promo else 0,
    )
    return {"notes": notes}


def build_observation(
    offer: RawOffer,
    *,
    matcher: Matcher,
    product_units: dict[int, str],
    source_id: int,
    store_id: int,
    observed_on: str,
    flag_unmatched: bool = True,
) -> dict:
    """Normalize, match to a product and price-check one offer. Returns a row for `observations`.

    `flag_unmatched=False` is for bulk shop catalogs, where most products are simply not tracked."""
    q = resolve_quantity(offer)

    if offer.product_id is not None:
        product_id, method, score = offer.product_id, "user", 1.0
    elif m := matcher.match(offer.generic_name, offer.raw_name, brand=offer.brand):
        product_id, method, score = m.product_id, m.method, m.score
    else:
        product_id, method, score = None, "none", 0.0
    base_unit = product_units.get(product_id) if product_id else None

    row = {
        "source_id": source_id,
        "store_id": store_id,
        "product_id": product_id,
        "external_id": offer.external_id,
        "raw_name": offer.raw_name.strip(),
        "generic_name": offer.generic_name,
        "brand": offer.brand,
        "category_hint": offer.category_hint,
        "quantity_text": offer.quantity_text,
        "amount": q.amount if q else None,
        "unit": q.unit if q else None,
        "item_count": q.item_count if q else None,
        "approx": int(q.approx) if q else 0,
        "sold_by_measure": int(q.sold_by_measure) if q else 0,
        "price": offer.price,
        "regular_price": offer.regular_price,
        "printed_unit_price": offer.printed_unit_price,
        "printed_unit": offer.printed_unit,
        "promo_text": offer.promo_text,
        "observed_on": observed_on,
        "valid_from": offer.valid_from,
        "valid_to": offer.valid_to,
        "page": offer.page,
        "match_method": method,
        "match_score": score,
    }
    target = base_unit or (q.unit if q else None)
    row["_printed_in_target"] = (
        printed_unit_price_in(offer.printed_unit_price, offer.printed_unit, target) if target else None
    )
    notes = compute_prices(row, base_unit)["notes"]
    del row["_printed_in_target"]

    if product_id is None:
        if not flag_unmatched:
            notes = []
        else:
            notes.append("no matching product")
    elif method == "phrase" and score < 0.34:
        notes.append(f"weak product match ({score:.0%})")
    row["needs_review"] = int(bool(notes))
    row["review_notes"] = "; ".join(notes) or None
    return row


def insert_observation(conn: sqlite3.Connection, row: dict) -> int:
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(observations)")}
    cols = [c for c in row if c in columns]
    cur = conn.execute(
        f"INSERT INTO observations ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [row[c] for c in cols],
    )
    return cur.lastrowid


def reassign_product(conn: sqlite3.Connection, observation_id: int, product_id: int | None, *, learn: bool = True) -> None:
    """Point an observation at another product (from the review screen), recompute its unit price
    in that product's base unit, and remember the raw name as an alias for next time."""
    obs = conn.execute("SELECT * FROM observations WHERE id = ?", (observation_id,)).fetchone()
    if obs is None:
        raise KeyError(observation_id)
    row = dict(obs)
    row["product_id"] = product_id
    base = None
    if product_id is not None:
        base = conn.execute("SELECT base_unit FROM products WHERE id = ?", (product_id,)).fetchone()["base_unit"]
    target = base or row["unit"]
    row["_printed_in_target"] = (
        printed_unit_price_in(row["printed_unit_price"], row["printed_unit"], target) if target else None
    )
    notes = compute_prices(row, base)["notes"]
    conn.execute(
        """UPDATE observations SET product_id = ?, price_unit = ?, unit_price = ?, effective_unit_price = ?,
           match_method = 'user', match_score = 1.0, needs_review = ?, review_notes = ? WHERE id = ?""",
        (product_id, row["price_unit"], row["unit_price"], row["effective_unit_price"],
         int(row["unit_price"] is None), "; ".join(notes) or None, observation_id),
    )
    if learn and product_id is not None:
        conn.execute(
            "DELETE FROM product_aliases WHERE source = 'user' AND alias = ? AND product_id != ?",
            (row["raw_name"], product_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO product_aliases (product_id, alias, source) VALUES (?, ?, 'user')",
            (product_id, row["raw_name"]),
        )
    conn.commit()


RAW_OFFER_FIELDS = {f.name for f in fields(RawOffer)}
