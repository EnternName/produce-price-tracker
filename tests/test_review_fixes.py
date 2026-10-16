"""Regression tests for problems found in review of the browser add-on change."""

import base64
import io

import httpx
import pytest
from PIL import Image, ImageDraw

from pricetracker import capture, flyers, web
from pricetracker.extract import PageResult
from tests.test_api import pdf_bytes, png
from tests.test_flyers import FakeExtractor


def mock_client(routes, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        url = str(request.url)
        if url not in routes:
            return httpx.Response(404)
        status, body, headers = routes[url]
        return httpx.Response(status, content=body, headers=headers)

    return httpx.Client(transport=httpx.MockTransport(handler))


def text_page(width, height, rows, seed):
    """A text-heavy flyer page: same header and layout as its siblings, different product rows."""
    img = Image.new("RGB", (width, height), "white")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width, height // 12], fill="#c00")
    for i in range(rows):
        y = height // 10 + i * (height // (rows + 4))
        d.text((40, y), f"Producto {seed}-{i}  {seed + i},{(seed * 7 + i) % 100:02d} EUR  ({i},{seed:02d} EUR/kg)", fill="black")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=88)
    return buf.getvalue()


# --- duplicate detection must never drop distinct pages ------------------------------------------


def test_same_template_pages_are_all_kept():
    pages = [capture.inspect(f"https://cdn.example/folleto/p{i}.jpg", text_page(1415, 2400, 18, i)) for i in range(1, 7)]
    zoomed = [capture.inspect(f"https://cdn.example/folleto/zoom/p{i}.jpg", text_page(1000, 1696, 18, i + 10)) for i in range(1, 7)]
    result = capture.select_pages(pages + zoomed)
    assert len(result.files) == 12, result.skipped


# imgproxy names the source image in base64 (here "s3://leaflets/images/<uuid>/page-03_<hash>.png")
LIDL_FILE = "czM6Ly9sZWFmbGV0cy9pbWFnZXMvMDE5MmQ0ZTEvcGFnZS0wM181YTkzLnBuZw.jpg"


def test_size_variants_of_one_file_are_merged():
    big = text_page(1415, 2400, 18, 3)
    with Image.open(io.BytesIO(big)) as img:
        small = io.BytesIO()
        img.resize((708, 1200), Image.LANCZOS).save(small, format="JPEG", quality=85)
    lidl_like = [
        capture.inspect(f"https://imgproxy.example/AbCdEfGhIjKlMnOpQrStUv/rs:fit:708:1200:1/g:no/{LIDL_FILE}", small.getvalue()),
        capture.inspect(f"https://imgproxy.example/ZyXwVuTsRqPoNmLkJiHgFe/rs:fit:1415:2400:1/g:no/{LIDL_FILE}", big),
    ]
    result = capture.select_pages(lidl_like)
    assert [f.width for f in result.files] == [1415]
    assert capture.select_pages([lidl_like[1], lidl_like[1]]).files == [lidl_like[1]]  # identical bytes


def test_asset_key():
    assert capture.asset_key("https://c.example/w_600,q_auto/p1.jpg") == capture.asset_key("https://c.example/w_1600,q_auto/p1.jpg")
    assert capture.asset_key("https://c.example/p1.jpg?width=600") == capture.asset_key("https://c.example/p1.jpg?width=2400")
    assert capture.asset_key("https://c.example/p1.jpg") != capture.asset_key("https://c.example/p2.jpg")
    assert capture.asset_key("https://c.example/page.jpg?n=1") != capture.asset_key("https://c.example/page.jpg?n=2")


# --- file names keep their extension -------------------------------------------------------------


def test_long_names_keep_their_extension(client, tmp_path):
    name = "Screenshot 2026-10-14 at 09-12-33 Ofertas de la semana en alimentación y frescos  Carrefour Supermercado.png"
    path = tmp_path / name
    path.write_bytes(png(900, 1200, seed=3))
    with path.open("rb") as fh:
        r = client.post("/flyers", data={"store": "Carrefour"}, files={"files": (name, fh, "image/png")}, follow_redirects=False)
    source_id = int(r.headers["location"].rsplit("/", 1)[1])
    status = client.get(f"/api/flyers/{source_id}", headers=client.ext_headers).json()
    assert status["status"] == "done" and status["files"] == 1

    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    r = client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers,
                    files={"files": ("f" * 150 + ".png", png(900, 1200), "image/png")})
    assert r.json()["files"] == 1


def test_safe_name():
    assert flyers._safe_name("x" * 200 + ".PDF").endswith(".pdf") and len(flyers._safe_name("x" * 200 + ".pdf")) <= 90
    assert flyers._safe_name("../../etc/passwd") == "passwd"
    assert flyers._safe_name("ñandú fresco.jpg") == "ñandú_fresco.jpg"


def test_unreadable_formats_are_converted():
    buf = io.BytesIO()
    Image.open(io.BytesIO(png(900, 1200))).save(buf, format="BMP")
    item = capture.inspect("https://x.example/p.bmp", buf.getvalue())
    assert item.suffix == ".png" and item.data[:4] == b"\x89PNG"


# --- downloads only reach the public internet ----------------------------------------------------


def test_connection_is_pinned_to_the_checked_address(dns):
    dns["cdn.example"] = ["93.184.216.34"]
    seen = []
    routes = {"https://93.184.216.34/p.pdf": (200, pdf_bytes(), {"content-type": "application/pdf"})}
    data, ctype, final = capture.fetch_public("https://cdn.example/p.pdf", client=mock_client(routes, seen))
    assert data[:5] == b"%PDF-" and final == "https://cdn.example/p.pdf"
    assert seen[0].headers["host"] == "cdn.example" and seen[0].extensions["sni_hostname"] == "cdn.example"
    assert seen[0].headers["connection"] == "close"  # a pooled connection is never reused for another host


def test_redirect_into_the_local_network_is_blocked(dns):
    dns["cdn.example"] = ["93.184.216.34"]
    routes = {"http://93.184.216.34/p.jpg": (302, b"", {"location": "http://192.168.1.1/admin"})}
    with pytest.raises(capture.BlockedURL):
        capture.fetch_public("http://cdn.example/p.jpg", client=mock_client(routes))


@pytest.mark.parametrize("answer", [["93.184.216.34", "127.0.0.1"], ["100.100.100.100"], ["10.0.0.5"]])
def test_private_or_mixed_dns_answers_are_blocked(dns, answer):
    dns["evil.example"] = answer
    with pytest.raises(capture.BlockedURL):
        capture.assert_public_url("https://evil.example/x.jpg")


def test_unresolvable_names_fail_closed(dns):
    with pytest.raises(capture.BlockedURL):
        capture.assert_public_url("https://nowhere.example/x.jpg")


@pytest.mark.parametrize("url", ["http://100.64.0.1:8080/x.png", "http://[::ffff:127.0.0.1]/x", "http://0.0.0.0/x", "http://169.254.169.254/latest"])
def test_special_addresses_are_blocked(dns, url):
    with pytest.raises(capture.BlockedURL):
        capture.assert_public_url(url)


def test_batch_and_image_size_limits(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOW_PRIVATE_URLS", "1")
    monkeypatch.setattr(capture, "MAX_BATCH_BYTES", 300_000)
    routes = {f"https://cdn.example/p{i}.jpg": (200, text_page(1415, 2400, 18, i), {"content-type": "image/jpeg"}) for i in range(8)}
    fetched, skipped = capture.download_all(list(routes), client=mock_client(routes))
    assert skipped and any("too large" in s["reason"] for s in skipped)
    huge = io.BytesIO()
    Image.new("L", (6000, 6000), 255).save(huge, format="PNG")
    with pytest.raises(ValueError, match="too large"):
        capture.inspect("https://cdn.example/huge.png", huge.getvalue())


# --- text pieces overlap and are de-duplicated ---------------------------------------------------


def test_products_split_across_pieces_are_whole_in_one():
    blocks = [f"Producto {i} Hacendado\n500 g\n{i % 9 + 1},{i % 100:02d} €\n{i % 7},50 €/kg" for i in range(600)]
    chunks = flyers.chunk_text("\n".join(blocks), size=2000)
    assert len(chunks) > 5
    for block in blocks:
        assert any(block in chunk for chunk in chunks), block


class TextEcho:
    """Returns the same offer for every text piece, as the overlap would make a model do."""

    def extract(self, page, *, store_name, total_pages, today):
        item = {
            "name_printed": "Leche entera", "generic_name_es": "leche entera", "brand": None, "is_food": True,
            "category": "Lácteos", "quantity_text": "1 L", "pack_count": 1, "unit_amount": 1, "unit": "l",
            "approx_weight": False, "price_basis": "item", "price": 0.95, "regular_price": None, "loyalty_price": None,
            "printed_unit_price": None, "printed_unit": None, "promo_text": None, "confidence": "high",
        }
        return PageResult({"language": "es", "valid_from": None, "valid_to": None, "items": [item]}, 10, 10)


def test_offers_repeated_by_the_overlap_are_stored_once(conn):
    source_id = flyers.create_flyer_source(conn, store="Dia", collecting=True)
    flyers.add_flyer_files(conn, source_id, blobs=[("page.txt", flyers.page_text_blob("Leche entera 1 L 0,95 €\n" * 3000, None))])
    flyers.close_draft(conn, source_id)
    flyers.process_flyer(conn, source_id, extractor=TextEcho())
    assert conn.execute("SELECT COUNT(*) FROM observations WHERE source_id = ?", (source_id,)).fetchone()[0] == 1
    assert conn.execute("SELECT pages_total FROM sources WHERE id = ?", (source_id,)).fetchone()[0] > 1


# --- a flyer is never read twice at once ---------------------------------------------------------


def test_stale_read_now_button_does_nothing(client, monkeypatch):
    calls = []
    real = flyers.process_flyer
    monkeypatch.setattr(flyers, "process_flyer", lambda *a, **k: (calls.append(a[1]), real(*a, **k))[1])
    draft = client.post("/api/flyers", json={"store": "Aldi"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})
    client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
    client.post(f"/sources/{draft['id']}/finish", follow_redirects=False)  # the page still showed "Read now"
    assert client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers).status_code == 409
    assert calls == [draft["id"]]


def test_reprocess_while_reading_does_nothing(client, conn, monkeypatch):
    draft = client.post("/api/flyers", json={"store": "Aldi"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})
    client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
    db_conn = __import__("pricetracker.db", fromlist=["connect"]).connect(web.app.state.db_path)
    db_conn.execute("UPDATE sources SET status = 'processing' WHERE id = ?", (draft["id"],))
    db_conn.commit()
    calls = []
    monkeypatch.setattr(web, "_process_in_background", lambda *a: calls.append(a))
    client.post(f"/sources/{draft['id']}/reprocess", follow_redirects=False)
    assert calls == []
    with pytest.raises(flyers.AlreadyProcessing):
        flyers.process_flyer(db_conn, draft["id"], extractor=FakeExtractor())
    assert flyers.reset_interrupted(db_conn) == 0  # still making progress (e.g. another terminal)
    db_conn.execute("UPDATE sources SET heartbeat_at = datetime('now', '-2 hours') WHERE id = ?", (draft["id"],))
    db_conn.commit()
    assert flyers.reset_interrupted(db_conn) == 1  # stalled: the app that read it has stopped
    db_conn.close()


def test_pages_arriving_after_finish_are_refused(client, monkeypatch):
    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    client.post(f"/api/flyers/{draft['id']}/pages", headers=client.ext_headers, files={"files": ("a.png", png(900, 1200), "image/png")})

    def slow_download(urls, **kwargs):  # the user presses "Read now" while this downloads
        client.post(f"/api/flyers/{draft['id']}/finish", headers=client.ext_headers)
        return [capture.inspect("https://cdn.example/p2.png", png(900, 1200, seed=9))], []

    monkeypatch.setattr(capture, "download_all", slow_download)
    r = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers, json={"urls": ["https://cdn.example/p2.png"]})
    assert r.status_code == 409
    assert client.get(f"/api/flyers/{draft['id']}", headers=client.ext_headers).json()["files"] == 1


def test_sharper_copy_replaces_a_page_sent_earlier(client, monkeypatch):
    big = text_page(1415, 2400, 18, 5)
    with Image.open(io.BytesIO(big)) as img:
        small = io.BytesIO()
        img.resize((708, 1200), Image.LANCZOS).save(small, format="JPEG", quality=85)
    batches = iter([
        [capture.inspect("https://cdn.example/w_708/p5.jpg", small.getvalue())],
        [capture.inspect("https://cdn.example/w_1415/p5.jpg", big)],
    ])
    monkeypatch.setattr(capture, "download_all", lambda urls, **kw: (next(batches), []))
    draft = client.post("/api/flyers", json={"store": "Lidl"}, headers=client.ext_headers).json()
    first = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers, json={"urls": ["x"]}).json()
    second = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers, json={"urls": ["y"]}).json()
    assert first["added"] == 1 and second["added"] == 0 and second["replaced"] == 1 and second["files"] == 1


# --- one-request sends and forgiving validation --------------------------------------------------


def test_one_request_send_of_a_pdf_and_of_text(client, monkeypatch):
    monkeypatch.setattr(capture, "download_all", lambda urls, **kw: ([capture.inspect(urls[0], pdf_bytes())], []))
    r = client.post("/api/flyers/send", headers=client.ext_headers,
                    json={"store": "Consum", "urls": ["https://www.consum.es/content/docs/f.pdf"], "page_url": "https://www.consum.es/" + "q" * 3000})
    assert r.status_code == 201, r.text
    body = r.json()
    assert not body["collecting"] and body["files"] == 1
    assert client.get(f"/api/flyers/{body['id']}", headers=client.ext_headers).json()["status"] == "done"

    # an emoji cut in half at the end of the page text (sent as the browser's JSON would be)
    raw = '{"store": "Carrefour", "text": "' + "Leche entera 1 L 0,95 EUR " * 20 + '\\ud83d"}'
    r = client.post("/api/flyers/send", headers={**client.ext_headers, "Content-Type": "application/json"}, content=raw)
    assert r.status_code == 201, r.text
    assert client.get(f"/api/flyers/{r.json()['id']}", headers=client.ext_headers).json()["status"] == "done"


def test_one_request_send_with_nothing_usable_creates_nothing(client, monkeypatch):
    monkeypatch.setattr(capture, "download_all", lambda urls, **kw: ([], [{"url": urls[0], "reason": "not a PDF or a readable image"}]))
    before = client.get("/flyers").text.count("/sources/")
    r = client.post("/api/flyers/send", headers=client.ext_headers, json={"store": "Dia", "urls": ["https://x.example/a"]})
    assert r.status_code == 422 and "not a PDF" in r.json()["detail"]
    assert client.get("/flyers").text.count("/sources/") == before
    assert client.post("/api/flyers/send", headers=client.ext_headers, json={"store": "Dia"}).status_code == 422


def test_validation_is_forgiving_and_never_500(client):
    r = client.post("/api/flyers", headers=client.ext_headers, json={"store": "   "})
    assert r.status_code == 422 and "store" in r.json()["detail"] and "input" not in r.text
    r = client.post("/api/flyers", headers=client.ext_headers, json={"store": "Lidl", "page_url": "https://x/" + "a" * 5000})
    assert r.status_code == 201
    draft = r.json()
    r = client.post(f"/api/flyers/{draft['id']}/urls", headers=client.ext_headers, json={"urls": ["https://x/" + "b" * 5000]})
    assert r.status_code == 200 and r.json()["skipped"][0]["reason"] == "link too long"
    raw = '{"text": "' + "Producto 1,99 EUR " * 5 + '\\ud83c"}'
    r = client.post(f"/api/flyers/{draft['id']}/text", headers={**client.ext_headers, "Content-Type": "application/json"}, content=raw)
    assert r.status_code == 200, r.text


# --- other devices and other sites ---------------------------------------------------------------


def test_other_devices_need_the_token_as_password(client, monkeypatch):
    monkeypatch.setenv("PRICETRACKER_TRUSTED_CLIENTS", "")  # TestClient now counts as another device
    r = client.get("/extension")
    assert r.status_code == 401 and "basic" in r.headers["www-authenticate"].lower() and client.token not in r.text
    assert client.post("/import/manual", data={"store": "Dia", "name": "x", "price": "1"}).status_code == 401
    auth = {"Authorization": "Basic " + base64.b64encode(f"me:{client.token}".encode()).decode()}
    assert client.get("/extension", headers=auth).status_code == 200
    wrong = {"Authorization": "Basic " + base64.b64encode(b"me:nope").decode()}
    assert client.get("/", headers=wrong).status_code == 401


def test_pages_cannot_be_framed(client):
    r = client.get("/sources/1")
    for path in ("/", "/flyers", "/extension"):
        r = client.get(path)
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
