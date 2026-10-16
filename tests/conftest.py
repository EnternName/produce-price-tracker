import socket

import pytest
from fastapi.testclient import TestClient

from pricetracker import capture, db, flyers, web


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    # TestClient talks to host "testserver" from client address "testclient".
    monkeypatch.setenv("PRICETRACKER_ALLOWED_HOSTS", "testserver")
    monkeypatch.setenv("PRICETRACKER_TRUSTED_CLIENTS", "testclient")


@pytest.fixture
def conn():
    connection = db.connect(":memory:")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def matcher(conn):
    return db.load_matcher(conn)


@pytest.fixture
def slug_of(conn):
    def lookup(product_id):
        if product_id is None:
            return None
        return conn.execute("SELECT slug FROM products WHERE id = ?", (product_id,)).fetchone()["slug"]

    return lookup


EXT_ORIGIN = "moz-extension://5b6c6f0e-0000-4000-8000-000000000001"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(web.app.state, "db_path", tmp_path / "web.db")
    from tests.test_flyers import FakeExtractor

    monkeypatch.setattr(flyers, "ClaudeExtractor", lambda **kw: FakeExtractor())
    monkeypatch.delenv("PRICETRACKER_TOKEN", raising=False)
    with TestClient(web.app) as c:
        c.token = (tmp_path / "api_token").read_text().strip()
        c.ext_headers = {"X-Pricetracker-Token": c.token, "Origin": EXT_ORIGIN}
        yield c


@pytest.fixture
def dns(monkeypatch):
    monkeypatch.delenv("PRICETRACKER_ALLOW_PRIVATE_URLS", raising=False)
    answers = {}

    def fake_getaddrinfo(host, *args, **kwargs):
        if host not in answers:
            raise socket.gaierror("no such host")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0)) for ip in answers[host]]

    monkeypatch.setattr(capture.socket, "getaddrinfo", fake_getaddrinfo)
    return answers
