import pytest
from fastapi.testclient import TestClient
from PIL import Image

from pricetracker import flyers, web
from pricetracker.importers import CSV_TEMPLATE
from tests.test_flyers import FakeExtractor


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web.app.state, "db_path", tmp_path / "web.db")
    monkeypatch.setattr(flyers, "UPLOAD_DIR", tmp_path / "uploads")
    with TestClient(web.app) as c:
        yield c


def test_pages_render_empty(client):
    for path in ("/", "/flyers", "/review", "/products", "/import"):
        r = client.get(path)
        assert r.status_code == 200, path
    assert "No comparable prices yet" in client.get("/").text


def test_csv_import_then_compare_and_history(client):
    r = client.post("/import/csv", files={"file": ("p.csv", CSV_TEMPLATE.encode(), "text/csv")})
    assert r.status_code == 200 and "Imported <b>3</b>" in r.text
    page = client.get("/?days=10000&expired=1").text
    assert "Leche semidesnatada" in page and "Aldi" in page
    product_link = page.split('href="/products/')[1].split('"')[0]
    detail = client.get(f"/products/{product_link}?days=3650")
    assert detail.status_code == 200 and 'id="series-data"' in detail.text


def test_manual_entry_and_assign(client):
    r = client.post("/import/manual", data={"store": "Condis", "name": "Gazpacho fresco", "price": "2,95", "quantity": "1 l"},
                    follow_redirects=False)
    assert r.headers["location"] == "/review"
    review = client.get("/review").text
    obs_id = review.split('id="obs-')[1].split('"')[0]
    r = client.post(f"/observations/{obs_id}/assign", data={"product": "Zumo [zumo]", "back": "/review"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Gazpacho fresco" not in client.get("/review").text  # resolved
    r = client.post(f"/observations/{obs_id}/assign", data={"product": "nonsense"})
    assert r.status_code == 400


def test_create_product_from_item(client):
    client.post("/import/manual", data={"store": "Condis", "name": "Hummus clásico", "price": "1,80", "quantity": "240 g"})
    obs_id = client.get("/review").text.split('id="obs-')[1].split('"')[0]
    r = client.post("/products", data={"name_es": "Hummus", "category": "Despensa", "base_unit": "kg", "obs": obs_id},
                    follow_redirects=False)
    assert r.status_code == 303
    assert "Hummus" in client.get("/?days=30").text


def test_flyer_upload_runs_in_background(client, tmp_path, monkeypatch):
    monkeypatch.setattr(flyers, "ClaudeExtractor", lambda **kw: FakeExtractor())
    img = tmp_path / "page.png"
    Image.new("RGB", (800, 1000), "white").save(img)
    with img.open("rb") as fh:
        r = client.post("/flyers", data={"store": "Bonpreu"}, files={"files": ("page.png", fh, "image/png")}, follow_redirects=False)
    assert r.status_code == 303
    source_page = client.get(r.headers["location"]).text
    assert "done" in source_page and "Plàtan de Canàries IGP" in source_page


def test_flyer_upload_rejects_other_files(client):
    r = client.post("/flyers", data={"store": "Lidl"}, files={"files": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_open_redirect_is_blocked(client):
    client.post("/import/manual", data={"store": "Dia", "name": "Cosa rara", "price": "1"})
    obs_id = client.get("/review").text.split('id="obs-')[1].split('"')[0]
    r = client.post(f"/observations/{obs_id}/ok", data={"back": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/review"
