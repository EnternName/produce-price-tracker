"""CSV and manual entry: for stores without a usable feed, or prices noted in the shop."""

from __future__ import annotations

import csv
import io
import sqlite3
from datetime import date

from . import db
from .pricing import RawOffer, build_observation, insert_observation
from .units import parse_price

# CSV header -> RawOffer field. Spanish headers are accepted too.
COLUMNS = {
    "store": "store", "tienda": "store", "supermercado": "store", "botiga": "store",
    "date": "date", "fecha": "date", "data": "date",
    "product": "raw_name", "producto": "raw_name", "name": "raw_name", "nombre": "raw_name", "producte": "raw_name",
    "generic_name": "generic_name", "generico": "generic_name",
    "brand": "brand", "marca": "brand",
    "quantity": "quantity_text", "cantidad": "quantity_text", "formato": "quantity_text", "quantitat": "quantity_text",
    "price": "price", "precio": "price", "preu": "price",
    "regular_price": "regular_price", "precio_anterior": "regular_price",
    "unit_price": "printed_unit_price", "precio_unidad": "printed_unit_price", "precio_kg": "printed_unit_price",
    "unit": "printed_unit", "unidad": "printed_unit",
    "promo": "promo_text", "promocion": "promo_text", "oferta": "promo_text",
    "valid_from": "valid_from", "desde": "valid_from",
    "valid_to": "valid_to", "hasta": "valid_to",
}

CSV_TEMPLATE = (
    "store,date,product,brand,quantity,price,unit_price,unit,promo,valid_from,valid_to\n"
    "Lidl,2026-10-13,Plátano de Canarias,,1 kg,1.79,,,,2026-10-13,2026-10-19\n"
    "Aldi,2026-10-13,Leche semidesnatada,Milsani,6 x 1 L,5.10,0.85,L,,,\n"
    "Carrefour,2026-10-13,Yogur griego,Carrefour,4 x 125 g,1.60,3.20,kg,3x2,,\n"
)


def import_csv(conn: sqlite3.Connection, text: str, *, default_store: str | None = None, origin: str = "csv") -> dict:
    """Import offers from CSV text. One source is created per (store, date)."""
    sample = text[:2048]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    matcher, units = db.load_matcher(conn), db.product_units(conn)
    sources: dict[tuple[int, str], int] = {}
    counts = {"rows": 0, "skipped": 0, "sources": 0}
    errors: list[str] = []
    for line_no, raw in enumerate(reader, start=2):
        row = {COLUMNS[k.strip().lower()]: (v or "").strip() for k, v in raw.items() if k and k.strip().lower() in COLUMNS}
        store_name = row.get("store") or default_store
        price = parse_price(row.get("price"))
        if not store_name or not row.get("raw_name") or not price:
            counts["skipped"] += 1
            errors.append(f"line {line_no}: needs store, product and price")
            continue
        observed_on = row.get("date") or date.today().isoformat()
        sid = db.store_id(conn, store_name)
        if (sid, observed_on) not in sources:
            cur = conn.execute(
                "INSERT INTO sources (store_id, kind, title, origin, observed_on) VALUES (?, 'csv', ?, ?, ?)",
                (sid, f"{store_name} {observed_on}", origin, observed_on),
            )
            sources[(sid, observed_on)] = cur.lastrowid
        offer = RawOffer(
            raw_name=row["raw_name"],
            price=price,
            generic_name=row.get("generic_name") or None,
            brand=row.get("brand") or None,
            quantity_text=row.get("quantity_text") or None,
            regular_price=parse_price(row.get("regular_price")),
            printed_unit_price=parse_price(row.get("printed_unit_price")),
            printed_unit=row.get("printed_unit") or None,
            promo_text=row.get("promo_text") or None,
            valid_from=row.get("valid_from") or None,
            valid_to=row.get("valid_to") or None,
        )
        obs = build_observation(
            offer, matcher=matcher, product_units=units, source_id=sources[(sid, observed_on)],
            store_id=sid, observed_on=observed_on,
        )
        insert_observation(conn, obs)
        counts["rows"] += 1
    conn.commit()
    counts["sources"] = len(sources)
    counts["errors"] = errors[:20]
    return counts


def add_manual(
    conn: sqlite3.Connection,
    *,
    store: str,
    raw_name: str,
    price: float,
    quantity_text: str | None = None,
    observed_on: str | None = None,
    promo_text: str | None = None,
    product_id: int | None = None,
) -> int:
    """Record one price seen in a shop. Returns the observation id."""
    observed_on = observed_on or date.today().isoformat()
    sid = db.store_id(conn, store)
    cur = conn.execute(
        "INSERT INTO sources (store_id, kind, title, observed_on) VALUES (?, 'manual', ?, ?)",
        (sid, f"Manual: {raw_name}", observed_on),
    )
    row = build_observation(
        RawOffer(raw_name=raw_name, price=price, quantity_text=quantity_text, promo_text=promo_text, product_id=product_id),
        matcher=db.load_matcher(conn), product_units=db.product_units(conn),
        source_id=cur.lastrowid, store_id=sid, observed_on=observed_on,
    )
    obs_id = insert_observation(conn, row)
    conn.commit()
    return obs_id
