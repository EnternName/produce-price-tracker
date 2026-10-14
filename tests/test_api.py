"""Browser add-on API, the localhost guard, and page-image capture."""

import io

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw

from pricetracker import capture, flyers, web
from tests.test_flyers import FakeExtractor

EXT_ORIGIN = "moz-extension://5b6c6f0e-0000-4000-8000-000000000001"


def png(width, height, text="page", color="white", scale_to=None, seed=0):
    """A fake flyer page: shared header/footer like real flyers, page-specific "product" blocks."""
    import random

    rng = random.Random(seed)
    img = Image.new("RGB", (width, height), color)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, width, height // 10], fill="#0050aa")  # same header on every page
    draw.rectangle([0, height - height // 12, width, height], fill="#ffcc00")  # same footer
    for _ in range(12 if width > 300 else 0):  # product photos and price tags differ per page
        x, y = rng.randrange(0, width - 200), rng.randrange(height // 10, height - height // 6)
        draw.rectangle([x, y, x + rng.randrange(80, 200), y + rng.randrange(80, 200)], fill=rng.choice(["#c33", "#3a3", "#333", "#e90"]))
    draw.text((30, 30), text, fill="white")
    if scale_to:  # the same page at a lower resolution
        img = img.resize(scale_to)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web.app.state, "db_path", tmp_path / "web.db")
    monkeypatch.setattr(flyers, "ClaudeExtractor", lambda **kw: FakeExtractor())
    monkeypatch.delenv("PRICETRACKER_TOKEN", raising=False)
    with TestClient(web.app) as c:
        c.token = (tmp_path / "api_token").read_text().strip()
        c.ext_headers = {"X-Pricetracker-Token": c.token, "Origin": EXT_ORIGIN}
        yield c


# --- guard -------------------------------------------------------------------------------------


def test_api_requires_token(client):
    assert client.get("/api/ping").status_code == 401
    assert client.get("/api/ping", headers={"X-Pricetracker-Token": "wrong"}).status_code == 401
    r = client.get("/api/ping", headers=client.ext_headers)
    assert r.status_code == 200 and r.json()["app"] == "pricetracker"
    assert r.headers["access-control-allow-origin"] == EXT_ORIGIN


def test_preflight_only_for_extensions(client):
    ok = client.options("/api/flyers", headers={"Origin": EXT_ORIGIN, "Access-Control-Request-Method": "POST",
                                                  "Access-Control-Request-Headers": "x-pricetracker-token,content-type"})
    assert ok.status_code == 204 and "x-pricetracker-token" in ok.headers["access-control-allow-headers"]
    bad = client.options("/api/flyers", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
    assert bad.status_code == 403 and "access-control-allow-origin" not in bad.headers


def test_website_cannot_use_api_even_with_token(client):
    r = client.post("/api/flyers", json={"store": "Lidl"}, headers={"X-Pricetracker-Token": client.token, "Origin": "https://evil.example"})
    assert r.status_code == 403


def test_cross_site_form_posts_are_blocked(client):
    r = client.post("/flyers/url", data={"store": "Lidl", "url": "https://x.example/a.pdf"}, headers={"Origin": "https://evil.example"})
    assert r.status_code == 403
    r = client.post("/import/manual", data={"store": "Dia", "name": "Leche entera", "price": "1"},
                    headers={"Referer": "https://evil.example/page"}, follow_redirects=False)
    assert r.status_code == 403
    assert client.post("/import/manual", data={"store": "Dia", "name": "x", "price": "1"}, headers={"Origin": "null"}).status_code == 403
    same = client.post("/import/manual", data={"store": "Dia", "name": "Leche entera", "price": "1"},
                       headers={"Origin": "http://testserver"}, follow_redirects=False)
    assert same.status_code == 303


def test_dns_rebinding_host_is_refused(client):
    r = client.get("/extension", headers={"Host": "evil.example"})
    assert r.status_code == 421 and client.token not in r.text
    assert client.get("/api/ping", headers={**client.ext_headers, "Host": "evil.example:8000"}).status_code == 421
    assert client.get("/", headers={"Host": "127.0.0.1:8000"}).status_code == 200
    assert client.get("/", headers={"Host": "localhost"}).status_code == 200
    assert client.get("/", headers={"Host": "[::1]:8000"}).status_code == 200


def test_host_name_parsing():
    from pricetracker.security import host_name

    assert host_name("127.0.0.1:8000") == "127.0.0.1"
    assert host_name("LOCALHOST") == "localhost"
    assert host_name("[::1]:8000") == "::1"
    assert host_name("::1") == "::1"


def test_token_file_is_reused(tmp_path, monkeypatch):
    from pricetracker.security import load_token

    monkeypatch.delenv("PRICETRACKER_TOKEN", raising=False)
    first = load_token(tmp_path)
    assert load_token(tmp_path) == first and len(first) >= 30
    monkeypatch.setenv("PRICETRACKER_TOKEN", "from-env")
    assert load_token(tmp_path) == "from-env"


# --- draft flyers ------------------------------------------------------------------------------


def test_screenshot_draft_then_finish(client):
    r = client.post("/api/flyers", json={"store": "Bonpreu / Esclat", "title": "Fulletó", "page_url": "https://www.bonpreuesclat.cat/fulletons"},
                    headers=client.ext_headers)
    assert r.status_code == 201
    draft = r.json()
    assert draft["collecting"] and draft["files"] == 0

    assert client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers).status_code == 422  # no pages yet

    for n in (1, 2):
        r = client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers,
                        files={"files": (f"screenshot-{n}.png", png(900, 1200, f"p{n}"), "image/png")})
        assert r.status_code == 200 and r.json()["files"] == n
    assert "collecting pages" in client.get("/flyers").text

    r = client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
    assert r.status_code == 200 and not r.json()["collecting"]
    status = client.get(f"/api/flyers/{draft['id']}", headers=client.ext_headers).json()
    assert status["status"] == "done" and status["items"] == 3 and status["pages_total"] == 2
    assert client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers,
                       files={"files": ("late.png", png(900, 1200), "image/png")}).status_code == 409


def test_pages_reject_other_files(client):
    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    r = client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("x.exe", b"MZ", "application/octet-stream")})
    assert r.status_code == 400


def test_discard_draft(client):
    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})
    assert client.delete(f"/api/flyers/{draft['id']}", headers=client.ext_headers).status_code == 200
    assert client.get(f"/api/flyers/{draft['id']}", headers=client.ext_headers).status_code == 404


def test_finish_draft_from_web_ui(client):
    draft = client.post("/api/flyers", json={"store": "Aldi"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})
    page = client.get(f"/sources/{draft['id']}").text
    assert "Collecting pages from the browser add-on" in page and 'http-equiv="refresh"' not in page
    r = client.post(f"/sources/{draft['id']}/finish", follow_redirects=False)
    assert r.status_code == 303


def test_urls_endpoint(client, monkeypatch):
    pages = {"https://cdn.example/p1.jpg": png(1000, 1400, "one"), "https://cdn.example/p2.jpg": png(1000, 1400, "two", "#ffe")}
    captured = {}

    def fake_collect(urls, **kwargs):
        captured.update(kwargs, urls=urls)
        return capture.CaptureResult(files=[capture.inspect(u, pages[u]) for u in urls if u in pages],
                                     skipped=[{"url": u, "reason": "404"} for u in urls if u not in pages])

    monkeypatch.setattr(capture, "collect", fake_collect)
    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    r = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers,
                    json={"urls": [*pages, "https://cdn.example/missing.jpg"], "page_url": "https://www.lidl.es/folleto", "user_agent": "Firefox"})
    body = r.json()
    assert r.status_code == 200 and body["added"] == 2 and body["files"] == 2 and body["skipped_count"] == 1
    assert captured["page_url"] == "https://www.lidl.es/folleto" and captured["user_agent"] == "Firefox"


def test_extension_page_and_zip(client):
    page = client.get("/extension").text
    assert client.token in page
    r = client.get("/extension/pricetracker-addon.zip")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    import zipfile

    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert "manifest.json" in names and "popup.js" in names and "icons/icon-48.png" in names


# --- capture -----------------------------------------------------------------------------------


def _mock_client(routes):
    def handler(request):
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404)
        data, ctype = routes[url]
        return httpx.Response(200, content=data, headers={"content-type": ctype})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_collect_filters_and_dedupes(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    big = png(1200, 1600, "page one", seed=1)
    routes = {
        "https://cdn.example/page1-small.jpg": (png(1200, 1600, "page one", seed=1, scale_to=(600, 800)), "image/png"),  # same page, lower res
        "https://cdn.example/page1.jpg": (big, "image/png"),
        "https://cdn.example/page2.jpg": (png(1200, 1600, "page two", seed=2), "image/png"),  # same layout, other products
        "https://cdn.example/logo.png": (png(120, 60), "image/png"),
        "https://cdn.example/flyer.pdf": (b"%PDF-1.7 fake", "application/pdf"),
        "https://cdn.example/app.js": (b"console.log(1)", "text/javascript"),
    }
    result = capture.collect(list(routes) + ["https://cdn.example/gone.jpg"], client=_mock_client(routes))
    kept = [f.url for f in result.files]
    assert "https://cdn.example/page1.jpg" in kept and "https://cdn.example/page1-small.jpg" not in kept
    assert "https://cdn.example/page2.jpg" in kept and "https://cdn.example/flyer.pdf" in kept
    reasons = {s["url"]: s["reason"] for s in result.skipped}
    assert "too small" in reasons["https://cdn.example/logo.png"]
    assert "not a PDF" in reasons["https://cdn.example/app.js"]
    assert "404" in reasons["https://cdn.example/gone.jpg"]
    assert "https://cdn.example/page1-small.jpg" in reasons


def test_private_urls_are_blocked(monkeypatch):
    monkeypatch.delenv("PRICETRACKER_ALLOW_PRIVATE_URLS", raising=False)
    for url in ("http://127.0.0.1:8000/api/ping", "http://192.168.1.1/admin", "http://[::1]/", "file:///etc/passwd", "ftp://x/y"):
        with pytest.raises(capture.BlockedURL):
            capture.assert_public_url(url)
    result = capture.collect(["http://127.0.0.1:9/x.jpg"], client=_mock_client({}))
    assert result.files == [] and "local/private" in result.skipped[0]["reason"]


def test_too_many_urls_are_reported(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    urls = [f"https://cdn.example/{i}.jpg" for i in range(capture.MAX_URLS + 5)]
    result = capture.collect(urls, client=_mock_client({}))
    assert sum("limit" in s["reason"] for s in result.skipped) == 5


def test_page_text_flyer(client):
    text = "Ofertas de la semana\n" + "\n".join(f"Leche entera 1 L  0,{80 + i} €  0,{80 + i} €/L" for i in range(10))
    draft = client.post("/api/flyers", json={"store": "Carrefour"}, headers=client.ext_headers).json()
    r = client.post(f"/api/flyers/{draft['id']}/text", headers=client.ext_headers,
                    json={"text": text, "page_url": "https://www.carrefour.es/supermercado/ofertas/g"})
    assert r.status_code == 200 and r.json()["files"] == 1 and r.json()["chunks"] == 1
    client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
    status = client.get(f"/api/flyers/{draft['id']}", headers=client.ext_headers).json()
    assert status["status"] == "done" and status["pages_total"] == 1


def test_text_pages_reach_the_model_without_images(tmp_path):
    path = tmp_path / "0000_page.txt"
    path.write_bytes(flyers.page_text_blob("Yogur griego 4x125 g 1,65 €\n" * 2000, "https://www.aldi.es/ofertas.html"))
    pages = flyers.load_pages([path])
    assert len(pages) > 1 and all(not p.images for p in pages)
    assert all(len(p.text_layer) <= flyers.TEXT_CHUNK for p in pages)
    assert pages[0].extra["url"] == "https://www.aldi.es/ofertas.html"
    assert "# page:" not in pages[0].text_layer


def test_chunk_text_splits_on_lines():
    chunks = flyers.chunk_text("a" * 30 + "\n" + "b" * 30 + "\n\n" + "c" * 70, size=64)
    assert chunks == ["a" * 30 + "\n" + "b" * 30, "c" * 64, "c" * 6]


def test_pdf_link_in_viewer_json(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    viewer = b'{"flyer": {"name": "Folleto", "hiResPdfUrl": "https://assets.example/hi.pdf", "pdfUrl": "https://assets.example/f.pdf"}}'
    routes = {
        "https://endpoints.example/v4/flyer?flyer_identifier=x": (viewer, "application/json"),
        "https://assets.example/f.pdf": (b"%PDF-1.7 regular", "application/pdf"),
        "https://assets.example/hi.pdf": (b"%PDF-1.7 huge", "application/pdf"),
    }
    result = capture.collect(["https://endpoints.example/v4/flyer?flyer_identifier=x"], client=_mock_client(routes))
    assert [f.url for f in result.files] == ["https://assets.example/f.pdf"]
    assert capture.pdf_link_in_json(b"not json") is None
