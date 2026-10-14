"""SQLite storage: stores, sources (a flyer, an API snapshot, a CSV), products, and price observations."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from .catalog import CATALOG, STORES
from .matching import Matcher

DEFAULT_DB_PATH = Path(os.environ.get("PRICETRACKER_DB", "data/prices.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS stores (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    website TEXT
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    name_es TEXT NOT NULL,
    name_ca TEXT,
    category TEXT NOT NULL,
    base_unit TEXT NOT NULL CHECK (base_unit IN ('kg', 'L', 'ud')),
    user_created INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS product_aliases (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id) ON DELETE CASCADE,
    alias TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'seed' CHECK (source IN ('seed', 'user')),
    UNIQUE (product_id, alias)
);

-- One flyer, one API snapshot, one CSV import...
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    kind TEXT NOT NULL CHECK (kind IN ('flyer', 'api', 'csv', 'manual')),
    title TEXT,
    origin TEXT,            -- file name or URL
    file_path TEXT,         -- stored copy of an uploaded flyer
    language TEXT,          -- es / ca, as detected
    region TEXT,
    observed_on TEXT NOT NULL,   -- ISO date the prices were published/seen
    valid_from TEXT,
    valid_to TEXT,
    status TEXT NOT NULL DEFAULT 'done' CHECK (status IN ('pending', 'processing', 'done', 'error')),
    pages_total INTEGER,
    pages_done INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    input_tokens INTEGER,   -- Claude usage for flyer extraction
    output_tokens INTEGER,
    collecting INTEGER NOT NULL DEFAULT 0,  -- draft still receiving pages from the browser add-on
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS observations (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    store_id INTEGER NOT NULL REFERENCES stores(id),
    product_id INTEGER REFERENCES products(id) ON DELETE SET NULL,
    external_id TEXT,
    raw_name TEXT NOT NULL,
    generic_name TEXT,
    brand TEXT,
    category_hint TEXT,
    quantity_text TEXT,
    amount REAL,                 -- total quantity, in `unit`
    unit TEXT,                   -- kg / L / ud
    item_count INTEGER,
    approx INTEGER NOT NULL DEFAULT 0,
    sold_by_measure INTEGER NOT NULL DEFAULT 0,   -- `price` is already per kg/L/ud
    price REAL NOT NULL,
    regular_price REAL,          -- crossed-out / previous price
    price_unit TEXT,             -- unit `unit_price` is expressed in
    unit_price REAL,             -- EUR per price_unit, buying one
    effective_unit_price REAL,   -- EUR per price_unit with the multi-buy promo applied
    printed_unit_price REAL,     -- EUR/kg etc. as printed by the store, for cross-checking
    printed_unit TEXT,
    promo_text TEXT,
    promo_kind TEXT,
    promo_min_units INTEGER,
    loyalty_only INTEGER NOT NULL DEFAULT 0,
    observed_on TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    page INTEGER,
    match_method TEXT,           -- alias / phrase / user / none
    match_score REAL,
    needs_review INTEGER NOT NULL DEFAULT 0,
    review_notes TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_obs_product_date ON observations(product_id, observed_on);
CREATE INDEX IF NOT EXISTS idx_obs_source ON observations(source_id);
CREATE INDEX IF NOT EXISTS idx_obs_review ON observations(needs_review);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    path = Path(path) if path else DEFAULT_DB_PATH
    if str(path) != ":memory:":
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create tables, apply column additions to older databases, and (idempotently) seed data."""
    conn.executescript(SCHEMA)
    _add_missing_columns(conn)
    conn.executemany(
        "INSERT OR IGNORE INTO stores (slug, name, website) VALUES (?, ?, ?)",
        [(s.slug, s.name, s.website) for s in STORES],
    )
    for p in CATALOG:
        conn.execute(
            "INSERT OR IGNORE INTO products (slug, name_es, name_ca, category, base_unit) VALUES (?, ?, ?, ?, ?)",
            (p.slug, p.name_es, p.name_ca, p.category, p.base_unit),
        )
        (product_id,) = conn.execute("SELECT id FROM products WHERE slug = ?", (p.slug,)).fetchone()
        conn.executemany(
            "INSERT OR IGNORE INTO product_aliases (product_id, alias, source) VALUES (?, ?, 'seed')",
            [(product_id, alias) for alias in dict.fromkeys((p.name_es, p.name_ca, *p.synonyms))],
        )
    conn.commit()


# Columns added after the first release: (table, column, definition)
_ADDED_COLUMNS = [
    ("sources", "collecting", "INTEGER NOT NULL DEFAULT 0"),
]


def _add_missing_columns(conn: sqlite3.Connection) -> None:
    for table, column, definition in _ADDED_COLUMNS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def load_matcher(conn: sqlite3.Connection) -> Matcher:
    rows = conn.execute("SELECT product_id, alias, source FROM product_aliases").fetchall()
    return Matcher((r["product_id"], r["alias"], r["source"]) for r in rows)


def store_id(conn: sqlite3.Connection, slug_or_name: str) -> int:
    """Look a store up by slug or name, creating it if it is new."""
    key = slug_or_name.strip()
    row = conn.execute(
        "SELECT id FROM stores WHERE slug = lower(?) OR lower(name) = lower(?)", (key, key)
    ).fetchone()
    if row:
        return row["id"]
    slug = "-".join(key.lower().split())
    cur = conn.execute("INSERT INTO stores (slug, name) VALUES (?, ?)", (slug, key))
    conn.commit()
    return cur.lastrowid


def product_units(conn: sqlite3.Connection) -> dict[int, str]:
    return {r["id"]: r["base_unit"] for r in conn.execute("SELECT id, base_unit FROM products")}
