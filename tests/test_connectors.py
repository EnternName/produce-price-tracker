"""Connector tests use JSON shaped like the community-documented responses; the live endpoints were
not reachable from the sandbox these were written in."""

import httpx
import pytest

from pricetracker import queries
from pricetracker.connectors import download, lidl, mercadona
from pricetracker.importers import CSV_TEMPLATE, add_manual, import_csv

MERCADONA_PRODUCTS = [
    ("Aceite, especias y salsas / Aceite de oliva", {
        "id": "4241", "display_name": "Aceite de oliva virgen extra Hacendado", "packaging": "Garrafa",
        "price_instructions": {"unit_price": "21.95", "bulk_price": "4.39", "reference_price": "4.390",
                               "reference_format": "L", "unit_size": 5.0, "size_format": "l",
                               "previous_unit_price": "24.50", "approx_size": False, "total_units": None},
    }),
    ("Huevos, leche y mantequilla / Leche y bebidas vegetales", {
        "id": "10379", "display_name": "Leche semidesnatada Hacendado", "packaging": "Pack-6",
        "price_instructions": {"unit_price": "5.40", "bulk_price": "0.90", "reference_price": "0.900",
                               "reference_format": "L", "unit_size": 6.0, "size_format": "l", "total_units": 6},
    }),
    ("Huevos, leche y mantequilla / Huevos", {
        "id": "31001", "display_name": "Huevos grandes L", "packaging": "Paquete",
        "price_instructions": {"unit_price": "2.70", "bulk_price": "0.23", "reference_price": "0.225",
                               "reference_format": "ud", "unit_size": 12, "size_format": "ud", "total_units": 12},
    }),
    ("Fruta y verdura / Fruta", {
        "id": "3317", "display_name": "Plátano de Canarias", "packaging": "Pieza",
        "price_instructions": {"unit_price": "0.42", "bulk_price": "2.10", "reference_price": "2.100",
                               "reference_format": "kg", "unit_size": 0.2, "size_format": "kg", "approx_size": True},
    }),
    ("Postres y yogures / Yogures", {
        "id": "20001", "display_name": "Bebida de sabores rara Hacendado", "packaging": "Botella",
        "price_instructions": {"unit_price": "1.10", "reference_price": "1.100", "reference_format": "L",
                               "unit_size": 1, "size_format": "l"},
    }),
]


def test_mercadona_fetch(conn, slug_of):
    counts = mercadona.fetch(conn, products=iter(MERCADONA_PRODUCTS))
    assert counts == {"products": 5, "matched": 4}
    rows = {slug_of(r["product_id"]): r for r in conn.execute("SELECT * FROM observations")}
    assert rows["aceite-oliva-virgen-extra"]["unit_price"] == pytest.approx(4.39)
    assert rows["aceite-oliva-virgen-extra"]["regular_price"] == pytest.approx(24.50)
    assert rows["leche-semidesnatada"]["unit_price"] == pytest.approx(0.90)
    assert rows["huevos"]["unit_price"] == pytest.approx(0.225)
    assert rows["platano"]["unit_price"] == pytest.approx(2.10)
    assert rows[None]["needs_review"] == 0  # untracked shop products don't clog the review queue
    assert not any(r["needs_review"] for r in rows.values())


def test_mercadona_iter_products_skips_non_food():
    def handler(request):
        path = request.url.path
        assert request.url.params["wh"] == "bcn1"
        if path.endswith("/categories/"):
            return httpx.Response(200, json={"results": [
                {"id": 1, "name": "Fruta y verdura", "categories": [{"id": 27, "name": "Fruta"}]},
                {"id": 2, "name": "Limpieza y hogar", "categories": [{"id": 99, "name": "Detergente"}]},
            ]})
        assert path.endswith("/categories/27/")
        return httpx.Response(200, json={"id": 27, "categories": [{"name": "Fruta", "products": [MERCADONA_PRODUCTS[3][1]]}]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    products = list(mercadona.iter_products("bcn1", delay=0, client=client))
    assert [p["id"] for _, p in products] == ["3317"]


def test_lidl_find_leaflets():
    data = {"categories": [{"name": "Folletos", "flyers": [
        {"title": "Folleto semanal", "startDate": "2026-10-13T00:00:00", "endDate": "2026-10-19", "pdfUrl": "https://x/semana.pdf", "slug": "semana-42"},
        {"title": "Bazar", "pdfUrl": "https://x/bazar.pdf"},
        {"title": "Duplicate", "pdfUrl": "https://x/semana.pdf"},
    ]}]}
    leaflets = lidl.find_leaflets(data)
    assert [l.pdf_url for l in leaflets] == ["https://x/semana.pdf", "https://x/bazar.pdf"]
    assert leaflets[0].valid_from == "2026-10-13" and leaflets[0].valid_to == "2026-10-19"


def test_download(tmp_path):
    client = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, content=b"%PDF-1.4", headers={"content-type": "application/pdf"})))
    path = download.download("https://example.com/folleto?id=3", tmp_path, client=client)
    assert path.suffix == ".pdf" and path.read_bytes() == b"%PDF-1.4"
    html = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>", headers={"content-type": "text/html"})))
    with pytest.raises(ValueError):
        download.download("https://example.com/viewer", tmp_path, client=html)


def test_csv_import_and_comparison(conn):
    counts = import_csv(conn, CSV_TEMPLATE)
    assert counts["rows"] == 3 and counts["sources"] == 3
    semicolon = "tienda;fecha;producto;formato;precio\nDia;2026-10-13;Leche semidesnatada;1 l;0,95\nbad;;;;\n"
    counts = import_csv(conn, semicolon)
    assert counts["rows"] == 1 and counts["skipped"] == 1

    best = {p["slug"]: p for p in queries.best_prices(conn, days=10_000, include_expired=True)}
    milk = best["leche-semidesnatada"]
    assert [s["store_name"] for s in milk["stores"]] == ["Aldi", "Dia"]
    assert milk["best"]["unit_price"] == pytest.approx(0.85)
    assert best["yogur-griego"]["best"]["effective_unit_price"] == pytest.approx(3.20 * 2 / 3, rel=1e-3)


def test_manual_entry(conn, slug_of):
    obs_id = add_manual(conn, store="Bonpreu", raw_name="Pomes Fuji", price=2.49, quantity_text="safata 1 kg")
    row = conn.execute("SELECT * FROM observations WHERE id = ?", (obs_id,)).fetchone()
    assert slug_of(row["product_id"]) == "manzana" and row["unit_price"] == pytest.approx(2.49)
