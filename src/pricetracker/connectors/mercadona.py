"""Mercadona online shop (tienda.mercadona.es).

Mercadona does not print weekly flyers like Lidl or Aldi ("Siempre Precios Bajos"), but its web shop
is backed by a JSON API that lists every product with its price and the reference price per kg/L/unit:

    GET /api/categories/?lang=es&wh=<warehouse>          category tree
    GET /api/categories/<id>/?lang=es&wh=<warehouse>     products of a category

Prices depend on the warehouse (region) serving your postal code; set MERCADONA_WAREHOUSE
(e.g. "bcn1", "mad1", "vlc1") or pass --warehouse.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import time
from datetime import date
from typing import Iterator

from .. import db
from ..pricing import RawOffer, build_observation, insert_observation
from . import get_with_retry, http_client

log = logging.getLogger(__name__)

BASE_URL = "https://tienda.mercadona.es/api"
DEFAULT_WAREHOUSE = os.environ.get("MERCADONA_WAREHOUSE", "vlc1")

# Top-level categories that are not food (the user tracks food only, for now).
NON_FOOD_CATEGORIES = {
    "cuidado del cabello", "cuidado facial y corporal", "fitoterapia y parafarmacia", "limpieza y hogar",
    "maquillaje", "mascotas",
}
NON_FOOD_SUBCATEGORY_WORDS = ("pañal", "toallita", "higiene", "colonia", "perfume", "afeitado")


def _num(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def product_to_offer(product: dict, category: str | None = None) -> RawOffer | None:
    pi = product.get("price_instructions") or {}
    price = _num(pi.get("unit_price"))
    if not price:
        return None
    reference_format = (pi.get("reference_format") or "").strip() or None
    drained = _num(pi.get("drained_weight"))
    unit_amount = drained or _num(pi.get("unit_size"))
    unit = "kg" if drained else (pi.get("size_format") or None)
    total_units = pi.get("total_units")
    quantity_text = product.get("packaging") or ""
    if total_units:
        quantity_text = f"{quantity_text} {total_units} ud".strip()
    return RawOffer(
        raw_name=product.get("display_name") or "?",
        price=price,
        category_hint=category,
        quantity_text=quantity_text or None,
        pack_count=1,
        unit_amount=unit_amount,
        unit=unit,
        approx=bool(pi.get("approx_size")),
        regular_price=_num(pi.get("previous_unit_price")),
        printed_unit_price=_num(pi.get("reference_price")) or _num(pi.get("bulk_price")),
        printed_unit=reference_format,
        external_id=str(product.get("id")) if product.get("id") is not None else None,
    )


def iter_products(warehouse: str = DEFAULT_WAREHOUSE, *, delay: float = 0.5, client=None) -> Iterator[tuple[str, dict]]:
    """Yield (category name, product json) for every food product in the shop."""
    params = {"lang": "es", "wh": warehouse}
    own_client = client is None
    client = client or http_client()
    try:
        tree = get_with_retry(client, f"{BASE_URL}/categories/", params=params).json()
        for top in tree.get("results", []):
            if (top.get("name") or "").strip().lower() in NON_FOOD_CATEGORIES:
                continue
            for sub in top.get("categories", []):
                name = (sub.get("name") or "").lower()
                if any(word in name for word in NON_FOOD_SUBCATEGORY_WORDS):
                    continue
                time.sleep(delay)
                detail = get_with_retry(client, f"{BASE_URL}/categories/{sub['id']}/", params=params).json()
                for leaf in detail.get("categories", []) or [detail]:
                    for product in leaf.get("products", []):
                        yield f"{top.get('name')} / {leaf.get('name') or sub.get('name')}", product
    finally:
        if own_client:
            client.close()


def fetch(conn: sqlite3.Connection, warehouse: str = DEFAULT_WAREHOUSE, *, products: Iterator[tuple[str, dict]] | None = None) -> dict:
    """Snapshot the shop's food prices into one 'api' source. Returns counts."""
    store = db.store_id(conn, "mercadona")
    today = date.today().isoformat()
    cur = conn.execute(
        "INSERT INTO sources (store_id, kind, title, origin, region, observed_on, status) VALUES (?, 'api', ?, ?, ?, ?, 'processing')",
        (store, f"Mercadona online {today}", BASE_URL, warehouse, today),
    )
    source_id = cur.lastrowid
    conn.commit()
    matcher, units = db.load_matcher(conn), db.product_units(conn)
    counts = {"products": 0, "matched": 0}
    seen: set[str] = set()
    try:
        for category, product in products if products is not None else iter_products(warehouse):
            offer = product_to_offer(product, category)
            if offer is None or (offer.external_id and offer.external_id in seen):
                continue
            if offer.external_id:
                seen.add(offer.external_id)
            row = build_observation(
                offer, matcher=matcher, product_units=units, source_id=source_id, store_id=store,
                observed_on=today, flag_unmatched=False,
            )
            insert_observation(conn, row)
            counts["products"] += 1
            counts["matched"] += row["product_id"] is not None
        conn.execute("UPDATE sources SET status = 'done' WHERE id = ?", (source_id,))
    except Exception as e:
        conn.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?", (str(e), source_id))
        conn.commit()
        raise
    conn.commit()
    log.info("mercadona: %s products, %s matched", counts["products"], counts["matched"])
    return counts
