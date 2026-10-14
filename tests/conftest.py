import pytest

from pricetracker import db


@pytest.fixture(autouse=True)
def _allow_testclient_host(monkeypatch):
    monkeypatch.setenv("PRICETRACKER_ALLOWED_HOSTS", "testserver")


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
