import pytest

from pricetracker import db
from pricetracker.pricing import RawOffer, build_observation, insert_observation, reassign_product


@pytest.fixture
def ctx(conn, matcher):
    store = db.store_id(conn, "lidl")
    cur = conn.execute(
        "INSERT INTO sources (store_id, kind, observed_on) VALUES (?, 'flyer', '2026-10-12')", (store,)
    )
    return {
        "matcher": matcher,
        "product_units": db.product_units(conn),
        "source_id": cur.lastrowid,
        "store_id": store,
        "observed_on": "2026-10-12",
    }


def test_pack_price_per_litre(ctx, slug_of):
    row = build_observation(
        RawOffer(raw_name="Leche entera", price=5.34, pack_count=6, unit_amount=1, unit="l"), **ctx
    )
    assert slug_of(row["product_id"]) == "leche-entera"
    assert row["price_unit"] == "L"
    assert row["unit_price"] == pytest.approx(0.89)
    assert not row["needs_review"]


def test_per_kg_price(ctx):
    row = build_observation(RawOffer(raw_name="Plátano de Canarias", price=1.99, price_basis="per_kg"), **ctx)
    assert row["unit_price"] == pytest.approx(1.99) and row["price_unit"] == "kg"


def test_multibuy_effective_price(ctx):
    row = build_observation(
        RawOffer(raw_name="Yogur griego", price=1.50, quantity_text="4 x 125 g", promo_text="3x2"), **ctx
    )
    assert row["unit_price"] == pytest.approx(3.00)
    assert row["effective_unit_price"] == pytest.approx(2.00)
    assert row["promo_kind"] == "multibuy" and row["promo_min_units"] == 3


def test_eggs_priced_per_unit(ctx):
    row = build_observation(RawOffer(raw_name="Huevos frescos L", price=2.88, quantity_text="12 uds"), **ctx)
    assert row["price_unit"] == "ud" and row["unit_price"] == pytest.approx(0.24)


def test_printed_unit_price_mismatch_flags_review(ctx):
    row = build_observation(
        RawOffer(raw_name="Tomate cherry", price=1.29, unit_amount=250, unit="g",
                 printed_unit_price=7.16, printed_unit="€/kg"),
        **ctx,
    )
    assert row["unit_price"] == pytest.approx(5.16)
    assert row["needs_review"] and "printed" in row["review_notes"]


def test_printed_unit_price_per_100g(ctx):
    row = build_observation(
        RawOffer(raw_name="Jamón serrano", price=2.49, printed_unit_price=2.49, printed_unit="€/100 g"), **ctx
    )
    assert row["unit_price"] == pytest.approx(24.9)  # taken from the printed price, no quantity given


def test_unmatched_offer_needs_review(ctx):
    row = build_observation(RawOffer(raw_name="Gazpacho Alvalle", price=2.95, quantity_text="1 l"), **ctx)
    assert row["product_id"] is None and row["needs_review"]
    assert row["unit_price"] == pytest.approx(2.95) and row["price_unit"] == "L"


def test_reassign_learns_alias(conn, ctx, slug_of):
    row = build_observation(RawOffer(raw_name="Gazpacho Alvalle", price=2.95, quantity_text="1 l"), **ctx)
    obs_id = insert_observation(conn, row)
    zumo = conn.execute("SELECT id FROM products WHERE slug = 'zumo'").fetchone()["id"]
    reassign_product(conn, obs_id, zumo)
    updated = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
    assert updated["product_id"] == zumo and updated["match_method"] == "user" and not updated["needs_review"]
    assert slug_of(db.load_matcher(conn).match("Gazpacho Alvalle").product_id) == "zumo"
