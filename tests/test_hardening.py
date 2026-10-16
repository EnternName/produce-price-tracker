"""Regression tests from the second review: hostile downloads, page identity, recovery paths."""

import gzip
import http.server
import io
import threading
import time

import httpx
import pypdfium2 as pdfium
import pytest

from pricetracker import capture, flyers, web
from pricetracker.extract import PageResult
from tests.test_api import pdf_bytes, png
from tests.test_review_fixes import mock_client


@pytest.fixture
def local_server():
    """A real HTTP server (mock transports hand httpx bodies that are already decoded)."""
    routes = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            route = routes.get(self.path)
            if route is None:
                self.send_response(404)
                self.end_headers()
                return
            route(self)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", routes
    server.shutdown()


def test_compression_bomb_is_stopped_early(local_server, monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    base, routes = local_server
    bomb = gzip.compress(b"\0" * (200 * 1024 * 1024), compresslevel=9)  # ~200 KB on the wire

    def gz(handler, encoding="gzip", body=bomb):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/pdf")
        handler.send_header("Content-Encoding", encoding)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    routes["/bomb.pdf"] = gz
    routes["/double.pdf"] = lambda h: gz(h, "gzip, gzip", gzip.compress(bomb))
    with capture.new_client() as client:
        start = time.monotonic()
        with pytest.raises(ValueError, match="too large"):
            capture.fetch_public(f"{base}/bomb.pdf", client=client, max_bytes=5 * 1024 * 1024)
        assert time.monotonic() - start < 10
        with pytest.raises(ValueError, match="unsupported content encoding"):
            capture.fetch_public(f"{base}/double.pdf", client=client)


def test_gzip_bodies_still_work(local_server, monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    base, routes = local_server
    body = pdf_bytes()

    def serve(handler):
        packed = gzip.compress(body)
        handler.send_response(200)
        handler.send_header("Content-Encoding", "gzip")
        handler.send_header("Content-Length", str(len(packed)))
        handler.end_headers()
        handler.wfile.write(packed)

    routes["/f.pdf"] = serve
    with capture.new_client() as client:
        data, _, _ = capture.fetch_public(f"{base}/f.pdf", client=client)
    assert data == body


def test_slow_downloads_have_a_deadline(local_server, monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    base, routes = local_server

    def drip(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        for _ in range(100):
            handler.wfile.write(b"x")
            handler.wfile.flush()
            time.sleep(0.2)

    routes["/slow.jpg"] = drip
    with capture.new_client() as client, pytest.raises(ValueError, match="too long"):
        capture.fetch_public(f"{base}/slow.jpg", client=client, max_seconds=1)


def test_hostile_viewer_json_is_skipped_not_a_500(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    routes = {
        "https://v.example/deep.json": (200, b"[" * 100000 + b"]" * 100000, {"content-type": "application/json"}),
        "https://v.example/ctrl.json": (200, b'{"pdfUrl": "http://93.184.216.34/fly\\u0001er.pdf"}', {"content-type": "application/json"}),
        "https://v.example/p.png": (200, png(900, 1200), {"content-type": "image/png"}),
    }
    fetched, skipped = capture.download_all(list(routes), client=mock_client(routes))
    assert [f.url for f in fetched] == ["https://v.example/p.png"]
    assert len(skipped) == 2


def test_idn_hosts_are_sent_as_punycode(dns):
    dns["xn--supermercado-espaa-30b.example"] = ["93.184.216.34"]
    seen = []
    routes = {"http://93.184.216.34/folleto.pdf": (200, pdf_bytes(), {"content-type": "application/pdf"})}
    capture.fetch_public("http://supermercado-españa.example/folleto.pdf", client=mock_client(routes, seen))
    assert seen[0].headers["host"] == "xn--supermercado-espaa-30b.example"


def test_next_address_is_tried_when_one_is_down(dns):
    dns["cdn.example"] = ["93.184.216.34", "93.184.216.35"]

    def handler(request):
        if request.url.host == "93.184.216.34":
            raise httpx.ConnectError("refused", request=request)
        return httpx.Response(200, content=pdf_bytes())

    data, _, _ = capture.fetch_public("http://cdn.example/f.pdf", client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert data[:5] == b"%PDF-"


def test_pdfs_with_absurd_page_counts_are_refused(tmp_path):
    doc = pdfium.PdfDocument.new()
    for _ in range(capture.MAX_PDF_PAGES + 1):
        doc.new_page(100, 100)
    buf = io.BytesIO()
    doc.save(buf)
    with pytest.raises(ValueError, match="pages"):
        capture.inspect("https://x.example/huge.pdf", buf.getvalue())
    path = tmp_path / "huge.pdf"
    path.write_bytes(buf.getvalue())
    with pytest.raises(ValueError, match="pages"):
        flyers.load_pages([path])


def test_only_ordinary_image_formats_are_decoded():
    eps = b"%!PS-Adobe-3.0 EPSF-3.0\n%%BoundingBox: 0 0 600 800\nshowpage\n"
    with pytest.raises(ValueError):
        capture.inspect("https://x.example/page.jpg", eps)


@pytest.mark.parametrize("a,b", [
    ("https://img.example/62135438/10/708x1200/folleto-semana.jpg", "https://img.example/62135438/11/1415x2400/folleto-semana.jpg"),
    ("https://img.example/folleto/page-1/large.jpg", "https://img.example/folleto/page-2/large.jpg"),
    ("https://img.example/p_2/w_600/x.jpg", "https://img.example/p_3/w_1600/x.jpg"),
    ("https://img.example/0b6f3a2e9c4d4e5f8a7b6c5d4e3f2a1b/thumb.jpg", "https://img.example/7c1d2e3f4a5b6c7d8e9f0a1b2c3d4e5f/thumb.jpg"),
])
def test_page_folders_are_not_mistaken_for_sizes(a, b):
    assert capture.asset_key(a) != capture.asset_key(b)


def test_size_folders_still_match():
    assert capture.asset_key("https://i.example/f/708x1200/p3.jpg") == capture.asset_key("https://i.example/f/1415x2400/p3.jpg")
    assert capture.asset_key("https://i.example/f/thumb/p3.jpg") == capture.asset_key("https://i.example/f/zoom/p3.jpg")


class Deleter:
    """Reads page 1, then (as if the user deleted the flyer and uploaded another) swaps the row."""

    def __init__(self, conn, source_id, other_file):
        self.conn, self.source_id, self.other_file, self.calls = conn, source_id, other_file, 0

    def extract(self, page, **kwargs):
        self.calls += 1
        if self.calls == 2:
            self.conn.execute("UPDATE sources SET file_path = ? WHERE id = ?", (self.other_file, self.source_id))
            self.conn.commit()
        item = {"name_printed": f"Oferta p{page.number}", "generic_name_es": "leche entera", "is_food": True,
                "category": "Lácteos", "price_basis": "item", "price": 1.0, "confidence": "high"}
        return PageResult({"language": "es", "items": [item]}, 1, 1)


def test_a_reading_stops_when_its_flyer_is_replaced(conn, tmp_path):
    files = []
    for i in range(3):
        path = tmp_path / f"p{i}.png"
        path.write_bytes(png(900, 1200, seed=i))
        files.append(path)
    source_id = flyers.create_flyer_source(conn, store="Lidl", files=files)
    with pytest.raises(flyers.FlyerGone):
        flyers.process_flyer(conn, source_id, extractor=Deleter(conn, source_id, "/elsewhere"), workers=1)
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE source_id = ?", (source_id,)).fetchone()[0] <= 1


def test_flyer_being_read_cannot_be_deleted(client):
    draft = client.post("/api/flyers", json={"store": "Aldi"}, headers=client.ext_headers).json()
    db_conn = __import__("pricetracker.db", fromlist=["connect"]).connect(web.app.state.db_path)
    db_conn.execute("UPDATE sources SET status = 'processing' WHERE id = ?", (draft["id"],))
    db_conn.commit()
    db_conn.close()
    assert client.post(f"/sources/{draft['id']}/delete", follow_redirects=False).status_code == 409


def test_a_flyer_that_never_started_can_be_read_again(client, monkeypatch):
    draft = client.post("/api/flyers", json={"store": "Aldi"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})

    def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(flyers, "process_flyer", broken)
    client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
    status = client.get(f"/api/flyers/{draft['id']}", headers=client.ext_headers).json()
    assert status["status"] == "error" and "database is locked" in status["error"]
    assert "Read again" in client.get(f"/sources/{draft['id']}").text


def test_different_offers_with_the_same_name_are_kept():
    a = {"name_printed": "Yogur griego", "price": 1.99, "quantity_text": "4 x 115 g", "brand": "Danone", "is_food": True}
    b = {**a, "brand": "Oikos", "loyalty_price": 1.59}
    offers, keys = flyers._offers_from_page({"items": [a]}, 1, False)
    later, _ = flyers._offers_from_page({"items": [b]}, 3, False, skip=keys)
    assert len(offers) == 1 and len(later) == 2  # Oikos, plus its card price


def test_web_upload_keeps_very_long_names(client, tmp_path):
    name = ("Screenshot 2026-10-14 at 09-12-33 Ofertas de la semana en alimentación, droguería y frescos  "
            "Carrefour Supermercado Online - Compra online.png")
    with io.BytesIO(png(900, 1200)) as fh:
        r = client.post("/flyers", data={"store": "Carrefour"}, files={"files": (name, fh, "image/png")}, follow_redirects=False)
    assert r.status_code == 303


def test_blank_page_text_is_refused(client):
    draft = client.post("/api/flyers", json={"store": "Dia"}, headers=client.ext_headers).json()
    r = client.post(f"/api/flyers/{draft['id']}/text", headers=client.ext_headers, json={"text": " " * 40})
    assert r.status_code == 422


def test_send_racing_a_delete_is_a_409_not_a_500(client, monkeypatch):
    import shutil

    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    folder = __import__("pricetracker.db", fromlist=["connect"]).connect(web.app.state.db_path).execute(
        "SELECT file_path FROM sources WHERE id = ?", (draft["id"],)).fetchone()["file_path"]

    def download_then_vanish(urls, **kwargs):  # the folder is deleted while the images download
        shutil.rmtree(folder)
        return [capture.inspect("https://cdn.example/p.png", png(900, 1200, seed=4))], []

    monkeypatch.setattr(capture, "download_all", download_then_vanish)
    r = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers, json={"urls": ["https://cdn.example/p.png"]})
    assert r.status_code == 409
