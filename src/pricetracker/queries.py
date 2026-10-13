"""Read-side queries: best price per product across stores, price history, review queue."""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

# For each (product, store): the cheapest offer on the most recent date that store was seen.
_LATEST_PER_STORE = """
WITH candidates AS (
    SELECT o.*, s.name AS store_name, s.slug AS store_slug,
           MAX(o.observed_on) OVER (PARTITION BY o.product_id, o.store_id) AS latest_on
    FROM observations o
    JOIN stores s ON s.id = o.store_id
    JOIN products p ON p.id = o.product_id
    WHERE o.unit_price IS NOT NULL
      AND o.price_unit = p.base_unit
      AND o.observed_on >= :since
      AND (:include_expired OR o.valid_to IS NULL OR o.valid_to >= :today)
      {extra}
),
ranked AS (
    SELECT *, ROW_NUMBER() OVER (
        PARTITION BY product_id, store_id ORDER BY unit_price ASC, id DESC
    ) AS rn
    FROM candidates
    WHERE observed_on = latest_on
)
SELECT * FROM ranked WHERE rn = 1
"""


def _since(days: int) -> str:
    return (date.today() - timedelta(days=days)).isoformat()


def best_prices(
    conn: sqlite3.Connection,
    *,
    days: int = 30,
    category: str | None = None,
    search: str | None = None,
    include_expired: bool = False,
) -> list[dict]:
    """One row per product: cheapest store, its EUR/unit, and every store's latest price."""
    extra, params = "", {"since": _since(days), "today": date.today().isoformat(), "include_expired": int(include_expired)}
    if category:
        extra += " AND p.category = :category"
        params["category"] = category
    if search:
        extra += " AND (p.name_es LIKE :q OR p.name_ca LIKE :q OR o.raw_name LIKE :q)"
        params["q"] = f"%{search}%"
    rows = conn.execute(_LATEST_PER_STORE.format(extra=extra), params).fetchall()

    products = {
        r["id"]: dict(r)
        for r in conn.execute("SELECT id, slug, name_es, name_ca, category, base_unit FROM products")
    }
    by_product: dict[int, dict] = {}
    for r in rows:
        entry = by_product.setdefault(r["product_id"], {**products[r["product_id"]], "stores": []})
        entry["stores"].append(dict(r))
    result = []
    for entry in by_product.values():
        entry["stores"].sort(key=lambda s: s["unit_price"])
        best = entry["stores"][0]
        prices = [s["unit_price"] for s in entry["stores"]]
        entry["best"] = best
        entry["spread"] = (max(prices) - min(prices)) / min(prices) if len(prices) > 1 and min(prices) > 0 else 0.0
        result.append(entry)
    result.sort(key=lambda e: (e["category"], e["name_es"]))
    return result


def product_history(conn: sqlite3.Connection, product_id: int, *, days: int = 365) -> dict[str, list[dict]]:
    """Per store: the cheapest EUR/unit seen on each date (for the history chart)."""
    rows = conn.execute(
        """
        SELECT s.name AS store_name, o.observed_on AS day, MIN(o.unit_price) AS unit_price,
               MIN(o.effective_unit_price) AS effective_unit_price
        FROM observations o
        JOIN stores s ON s.id = o.store_id
        JOIN products p ON p.id = o.product_id
        WHERE o.product_id = ? AND o.unit_price IS NOT NULL AND o.price_unit = p.base_unit
          AND o.observed_on >= ?
        GROUP BY s.name, o.observed_on
        ORDER BY o.observed_on
        """,
        (product_id, _since(days)),
    ).fetchall()
    series: dict[str, list[dict]] = {}
    for r in rows:
        series.setdefault(r["store_name"], []).append(dict(r))
    return series


def product_observations(conn: sqlite3.Connection, product_id: int, limit: int = 200) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT o.*, s.name AS store_name, src.kind AS source_kind, src.title AS source_title
        FROM observations o
        JOIN stores s ON s.id = o.store_id
        JOIN sources src ON src.id = o.source_id
        WHERE o.product_id = ?
        ORDER BY o.observed_on DESC, o.unit_price ASC
        LIMIT ?
        """,
        (product_id, limit),
    ).fetchall()


def sources(conn: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT src.*, s.name AS store_name,
               (SELECT COUNT(*) FROM observations o WHERE o.source_id = src.id) AS n_items,
               (SELECT COUNT(*) FROM observations o WHERE o.source_id = src.id AND o.needs_review) AS n_review
        FROM sources src JOIN stores s ON s.id = src.store_id
        ORDER BY src.created_at DESC, src.id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def source_observations(
    conn: sqlite3.Connection, source_id: int, *, only_review: bool = False, limit: int = 1000
) -> list[sqlite3.Row]:
    """Items of one source; matched products first so big shop snapshots stay useful when truncated."""
    return conn.execute(
        f"""
        SELECT o.*, p.name_es AS product_name, p.base_unit
        FROM observations o LEFT JOIN products p ON p.id = o.product_id
        WHERE o.source_id = ? {"AND o.needs_review" if only_review else ""}
        ORDER BY o.needs_review DESC, o.product_id IS NULL, o.page, o.id
        LIMIT ?
        """,
        (source_id, limit),
    ).fetchall()


def review_queue(conn: sqlite3.Connection, limit: int = 300) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT o.*, p.name_es AS product_name, p.base_unit, s.name AS store_name
        FROM observations o
        JOIN stores s ON s.id = o.store_id
        LEFT JOIN products p ON p.id = o.product_id
        WHERE o.needs_review
        ORDER BY o.observed_on DESC, o.id
        LIMIT ?
        """,
        (limit,),
    ).fetchall()


def stats(conn: sqlite3.Connection) -> dict:
    row = conn.execute(
        """
        SELECT (SELECT COUNT(*) FROM observations) AS observations,
               (SELECT COUNT(*) FROM observations WHERE needs_review) AS needs_review,
               (SELECT COUNT(DISTINCT product_id) FROM observations WHERE product_id IS NOT NULL) AS products_seen,
               (SELECT COUNT(DISTINCT store_id) FROM observations) AS stores_seen,
               (SELECT COUNT(*) FROM sources) AS sources
        """
    ).fetchone()
    return dict(row)
