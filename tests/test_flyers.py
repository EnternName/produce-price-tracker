import json

import pytest
from PIL import Image, ImageDraw

from pricetracker import flyers
from pricetracker.extract import ClaudeExtractor, ExtractionError, PageResult

PAGE = {
    "language": "ca",
    "valid_from": "2026-10-13",
    "valid_to": "2026-10-19",
    "items": [
        {
            "name_printed": "Plàtan de Canàries IGP", "generic_name_es": "plátano", "brand": None, "is_food": True,
            "category": "Fruta", "quantity_text": None, "pack_count": None, "unit_amount": None, "unit": None,
            "approx_weight": False, "price_basis": "per_kg", "price": 1.79, "regular_price": 2.15,
            "loyalty_price": None, "printed_unit_price": None, "printed_unit": None, "promo_text": "-17%",
            "confidence": "high",
        },
        {
            "name_printed": "Llet semidesnatada", "generic_name_es": "leche semidesnatada", "brand": "Milbona",
            "is_food": True, "category": "Lácteos", "quantity_text": "6 x 1 L", "pack_count": 6, "unit_amount": 1,
            "unit": "l", "approx_weight": False, "price_basis": "item", "price": 5.34, "regular_price": None,
            "loyalty_price": 4.74, "printed_unit_price": 0.89, "printed_unit": "l", "promo_text": "Lidl Plus",
            "confidence": "high",
        },
        {
            "name_printed": "Detergent líquid", "generic_name_es": "detergente", "brand": None, "is_food": False,
            "category": "Otros", "quantity_text": "3 l", "pack_count": 1, "unit_amount": 3, "unit": "l",
            "approx_weight": False, "price_basis": "item", "price": 6.99, "regular_price": None,
            "loyalty_price": None, "printed_unit_price": None, "printed_unit": None, "promo_text": None,
            "confidence": "high",
        },
    ],
}


class FakeExtractor:
    def __init__(self, fail_pages=()):
        self.calls = []
        self.fail_pages = set(fail_pages)

    def extract(self, page, *, store_name, total_pages, today):
        self.calls.append((page.number, len(page.images), store_name, total_pages))
        if page.number in self.fail_pages:
            raise ExtractionError("boom")
        return PageResult(PAGE if page.number == 1 else {"language": "ca", "valid_from": None, "valid_to": None, "items": []}, 1000, 500)


@pytest.fixture
def flyer_pdf(tmp_path):
    pages = []
    for text in ("Plàtan 1,79 €/kg", "Portada"):
        img = Image.new("RGB", (800, 1100), "white")
        ImageDraw.Draw(img).text((40, 40), text, fill="black")
        pages.append(img)
    path = tmp_path / "lidl.pdf"
    pages[0].save(path, save_all=True, append_images=pages[1:])
    return path


@pytest.fixture(autouse=True)
def upload_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(flyers, "UPLOAD_DIR", tmp_path / "uploads")


def test_pdf_pages_are_rendered(flyer_pdf):
    pages = flyers.load_pages([flyer_pdf])
    assert [p.number for p in pages] == [1, 2]
    assert all(p.images[0][:2] == b"\xff\xd8" for p in pages)  # JPEG


def test_tall_screenshot_is_sliced(tmp_path):
    path = tmp_path / "scroll.png"
    Image.new("RGB", (1000, 5000), "white").save(path)
    (page,) = flyers.load_pages([path])
    assert len(page.images) >= 3


def test_process_flyer(conn, slug_of, flyer_pdf):
    source_id = flyers.create_flyer_source(conn, store="Lidl", files=[flyer_pdf])
    fake = FakeExtractor()
    totals = flyers.process_flyer(conn, source_id, extractor=fake)

    assert {c[0] for c in fake.calls} == {1, 2}
    assert totals["items"] == 3  # plátano, milk, milk with Lidl Plus; detergent skipped
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert src["status"] == "done" and src["language"] == "ca"
    assert src["valid_to"] == "2026-10-19" and src["pages_done"] == 2
    assert src["input_tokens"] == 2000

    rows = conn.execute("SELECT * FROM observations WHERE source_id = ? ORDER BY price", (source_id,)).fetchall()
    platano = next(r for r in rows if slug_of(r["product_id"]) == "platano")
    assert platano["unit_price"] == pytest.approx(1.79) and platano["price_unit"] == "kg"
    milk = [r for r in rows if slug_of(r["product_id"]) == "leche-semidesnatada"]
    assert sorted(r["loyalty_only"] for r in milk) == [0, 1]
    normal = next(r for r in milk if not r["loyalty_only"])
    assert normal["unit_price"] == pytest.approx(0.89) and not normal["needs_review"]


def test_rerun_replaces_items_and_partial_failure_is_reported(conn, flyer_pdf):
    source_id = flyers.create_flyer_source(conn, store="Lidl", files=[flyer_pdf])
    flyers.process_flyer(conn, source_id, extractor=FakeExtractor())
    flyers.process_flyer(conn, source_id, extractor=FakeExtractor(fail_pages={2}))
    src = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
    assert src["status"] == "done" and "1 of 2 pages failed" in src["error"]
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE source_id = ?", (source_id,)).fetchone()[0] == 3


def test_claude_request_shape(flyer_pdf):
    """Build the real request against a mock transport and check what would be sent."""
    import anthropic
    import httpx2

    captured = {}

    def handler(request):
        captured["body"] = json.loads(request.content)
        captured["headers"] = request.headers
        return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "stop"}})

    client = anthropic.Anthropic(api_key="test", http_client=httpx2.Client(transport=httpx2.MockTransport(handler)), max_retries=0)
    page = flyers.load_pages([flyer_pdf])[0]
    with pytest.raises(ExtractionError, match="400"):
        ClaudeExtractor(client=client).extract(page, store_name="Lidl", total_pages=2, today="2026-10-13")

    body = captured["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in captured["headers"]["anthropic-beta"]
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert body["thinking"] == {"type": "adaptive"}
    assert body["stream"] is True
    content = body["messages"][0]["content"]
    assert content[0]["type"] == "image" and content[0]["source"]["media_type"] == "image/jpeg"
    assert "Store: Lidl. Page 1 of 2" in content[-1]["text"]
