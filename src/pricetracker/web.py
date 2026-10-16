"""Web UI (FastAPI + Jinja): compare prices, browse history, upload and review flyers."""

from __future__ import annotations

import logging
import re
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Annotated

from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import api, db, flyers, queries
from .catalog import CATEGORIES
from .importers import CSV_TEMPLATE, add_manual, import_csv
from .pricing import reassign_product
from .security import LocalAppGuard, load_token
from .textnorm import fold

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
MAX_SERIES = 8  # categorical colour slots; further stores are drawn in a neutral "other" colour

EXTENSION_DIR = HERE / "browser_extension"


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = db.connect(app.state.db_path)
    db.init_db(conn)
    if n := flyers.reset_interrupted(conn):
        log.warning("%s flyer(s) were interrupted while being read; use 'Read again'", n)
    conn.close()
    app.state.token = load_token(Path(app.state.db_path).parent)
    yield


app = FastAPI(title="Price tracker", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """Say what is wrong without echoing the input back (it may not even be encodable)."""
    problems = "; ".join(
        f"{'.'.join(str(p) for p in e.get('loc', ()) if p != 'body') or 'request'}: {e.get('msg', 'invalid')}"
        for e in exc.errors()
    )
    return JSONResponse({"detail": problems or "invalid request"}, status_code=422)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
app.include_router(api.router)
app.add_middleware(LocalAppGuard, token_provider=lambda: app.state.token)
templates = Jinja2Templates(directory=HERE / "templates")
app.state.db_path = db.DEFAULT_DB_PATH
app.state.token = None


def _eur(value) -> str:
    if value is None:
        return "–"
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _unit_price(value, unit) -> str:
    if value is None:
        return "–"
    return f"{_eur(value)}/{unit or '?'}"


templates.env.filters["eur"] = _eur
templates.env.filters["unit_price"] = _unit_price
templates.env.globals["CATEGORIES"] = CATEGORIES


def get_conn():
    conn = db.connect(app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[object, Depends(get_conn)]


def render(request: Request, name: str, **context) -> HTMLResponse:
    return templates.TemplateResponse(request, name, context)


def _stores(conn) -> list:
    return conn.execute("SELECT * FROM stores ORDER BY name").fetchall()


def _store_slots(conn, store_names) -> dict[str, int | None]:
    """Stable colour slot per store: the first 8 stores with data keep slots 1-8 everywhere."""
    rows = conn.execute(
        """SELECT s.name, MIN(o.id) AS first_seen FROM stores s JOIN observations o ON o.store_id = s.id
           WHERE o.product_id IS NOT NULL GROUP BY s.id ORDER BY first_seen"""
    ).fetchall()
    slots = {r["name"]: i + 1 for i, r in enumerate(rows[:MAX_SERIES])}
    return {name: slots.get(name) for name in store_names}


# --- compare ---------------------------------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def compare(request: Request, conn: Conn, q: str = "", category: str = "", days: int = 30, expired: int = 0):
    rows = queries.best_prices(conn, days=days, category=category or None, search=q or None, include_expired=bool(expired))
    groups: dict[str, list] = {}
    for row in rows:
        groups.setdefault(row["category"], []).append(row)
    return render(
        request, "compare.html", groups=groups, stats=queries.stats(conn), q=q, category=category,
        days=days, expired=expired, n_products=len(rows),
    )


# --- products --------------------------------------------------------------------------------------


@app.get("/products", response_class=HTMLResponse)
def products(request: Request, conn: Conn, name: str = "", obs: int | None = None):
    rows = conn.execute(
        """SELECT p.*, COUNT(o.id) AS n_obs, MAX(o.observed_on) AS last_seen
           FROM products p LEFT JOIN observations o ON o.product_id = p.id
           GROUP BY p.id ORDER BY p.category, p.name_es"""
    ).fetchall()
    return render(request, "products.html", products=rows, prefill=name, obs=obs)


@app.post("/products")
def create_product(
    conn: Conn,
    name_es: Annotated[str, Form()],
    category: Annotated[str, Form()],
    base_unit: Annotated[str, Form()],
    name_ca: Annotated[str, Form()] = "",
    synonyms: Annotated[str, Form()] = "",
    obs: Annotated[int | None, Form()] = None,
):
    if base_unit not in ("kg", "L", "ud"):
        raise HTTPException(400, "base unit must be kg, L or ud")
    slug = re.sub(r"[^a-z0-9]+", "-", fold(name_es)).strip("-")
    if conn.execute("SELECT 1 FROM products WHERE slug = ?", (slug,)).fetchone():
        raise HTTPException(400, f"A product called {name_es!r} already exists")
    cur = conn.execute(
        "INSERT INTO products (slug, name_es, name_ca, category, base_unit, user_created) VALUES (?, ?, ?, ?, ?, 1)",
        (slug, name_es.strip(), name_ca.strip() or None, category, base_unit),
    )
    product_id = cur.lastrowid
    aliases = [name_es, name_ca, *synonyms.split(",")]
    conn.executemany(
        "INSERT OR IGNORE INTO product_aliases (product_id, alias, source) VALUES (?, ?, 'seed')",
        [(product_id, a.strip()) for a in aliases if a and a.strip()],
    )
    conn.commit()
    if obs:
        reassign_product(conn, obs, product_id)
        source = conn.execute("SELECT source_id FROM observations WHERE id = ?", (obs,)).fetchone()
        return RedirectResponse(f"/sources/{source['source_id']}#obs-{obs}", status_code=303)
    return RedirectResponse(f"/products/{product_id}", status_code=303)


@app.get("/products/{product_id}", response_class=HTMLResponse)
def product_detail(request: Request, conn: Conn, product_id: int, days: int = 365):
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        raise HTTPException(404)
    current = next((p for p in queries.best_prices(conn, days=days, include_expired=True) if p["id"] == product_id), None)
    history = queries.product_history(conn, product_id, days=days)
    slots = _store_slots(conn, history.keys())
    series = [
        {"store": store, "slot": slots[store], "points": [{"x": p["day"], "y": p["unit_price"]} for p in points]}
        for store, points in sorted(history.items(), key=lambda kv: (slots[kv[0]] is None, slots[kv[0]] or 0, kv[0]))
    ]
    return render(
        request, "product.html", product=product, current=current, series=series, days=days,
        observations=queries.product_observations(conn, product_id),
    )


# --- flyers & sources ------------------------------------------------------------------------------


def _process_in_background(source_id: int, include_non_food: bool) -> None:
    conn = db.connect(app.state.db_path)
    try:
        flyers.process_flyer(conn, source_id, include_non_food=include_non_food)
    except (flyers.AlreadyProcessing, flyers.FlyerGone) as e:
        log.info("%s", e)
    except Exception as e:  # status/error are stored on the source row
        log.exception("flyer %s failed", source_id)
        flyers.mark_failed(conn, source_id, e)
    finally:
        conn.close()


@app.get("/flyers", response_class=HTMLResponse)
def flyer_list(request: Request, conn: Conn):
    return render(request, "flyers.html", sources=queries.sources(conn), stores=_stores(conn), today=date.today().isoformat())


@app.post("/flyers")
async def upload_flyer(
    background: BackgroundTasks,
    conn: Conn,
    store: Annotated[str, Form()],
    files: Annotated[list[UploadFile], File()],
    title: Annotated[str, Form()] = "",
    observed_on: Annotated[str, Form()] = "",
    valid_from: Annotated[str, Form()] = "",
    valid_to: Annotated[str, Form()] = "",
    include_non_food: Annotated[bool, Form()] = False,
):
    tmp = Path(tempfile.mkdtemp(prefix="upload-"))
    try:
        paths = []
        for i, upload in enumerate(f for f in files if f.filename):
            name = flyers._safe_name(upload.filename)
            if Path(name).suffix.lower() not in flyers.IMAGE_SUFFIXES | {".pdf"}:
                raise HTTPException(400, f"{upload.filename}: upload PDF, PNG, JPEG or WebP files")
            path = tmp / f"{i:03d}_{name}"
            path.write_bytes(await upload.read())
            paths.append(path)
        if not paths:
            raise HTTPException(400, "No files uploaded")
        try:
            source_id = flyers.create_flyer_source(
                conn, store=store, files=paths, title=title or None, observed_on=observed_on or None,
                valid_from=valid_from or None, valid_to=valid_to or None,
            )
        except (flyers.UnsupportedFile, ValueError) as e:
            raise HTTPException(400, str(e)) from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    background.add_task(_process_in_background, source_id, include_non_food)
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


@app.post("/flyers/url")
def flyer_from_url(
    background: BackgroundTasks,
    conn: Conn,
    store: Annotated[str, Form()],
    url: Annotated[str, Form()],
    title: Annotated[str, Form()] = "",
    valid_from: Annotated[str, Form()] = "",
    valid_to: Annotated[str, Form()] = "",
):
    from .connectors.download import download

    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "Enter an http(s) link to a PDF or image")
    tmp = Path(tempfile.mkdtemp(prefix="download-"))
    try:
        path = download(url, tmp)
        source_id = flyers.create_flyer_source(
            conn, store=store, files=[path], title=title or None, origin=url,
            valid_from=valid_from or None, valid_to=valid_to or None,
        )
    except Exception as e:
        raise HTTPException(400, f"Could not download {url}: {e}") from e
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    background.add_task(_process_in_background, source_id, False)
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


@app.get("/flyers/lidl", response_class=HTMLResponse)
def lidl_leaflets(request: Request):
    from .connectors import lidl

    try:
        leaflets, error = lidl.list_leaflets(), None
    except Exception as e:
        leaflets, error = [], str(e)
    return render(request, "lidl.html", leaflets=leaflets, error=error)


def _fetch_mercadona(warehouse: str) -> None:
    from .connectors import mercadona

    conn = db.connect(app.state.db_path)
    try:
        mercadona.fetch(conn, warehouse)
    except Exception:
        log.exception("mercadona fetch failed")
    finally:
        conn.close()


@app.post("/connectors/mercadona")
def fetch_mercadona(background: BackgroundTasks, warehouse: Annotated[str, Form()] = ""):
    from .connectors import mercadona

    background.add_task(_fetch_mercadona, warehouse.strip() or mercadona.DEFAULT_WAREHOUSE)
    return RedirectResponse("/flyers", status_code=303)


@app.get("/sources/{source_id}", response_class=HTMLResponse)
def source_detail(request: Request, conn: Conn, source_id: int, review: int = 0):
    source = conn.execute(
        "SELECT src.*, s.name AS store_name FROM sources src JOIN stores s ON s.id = src.store_id WHERE src.id = ?",
        (source_id,),
    ).fetchone()
    if source is None:
        raise HTTPException(404)
    rows = queries.source_observations(conn, source_id, only_review=bool(review))
    total = conn.execute(
        f"SELECT COUNT(*) FROM observations WHERE source_id = ? {'AND needs_review' if review else ''}", (source_id,)
    ).fetchone()[0]
    files_count = len(flyers.source_files(source["file_path"])) if source["file_path"] else 0
    return render(request, "source.html", source=source, rows=rows, total=total, review=review,
                  files_count=files_count, products=_product_options(conn))


@app.post("/sources/{source_id}/reprocess")
def reprocess(background: BackgroundTasks, conn: Conn, source_id: int):
    source = conn.execute("SELECT kind FROM sources WHERE id = ?", (source_id,)).fetchone()
    if source is None or source["kind"] != "flyer":
        raise HTTPException(400, "Only flyers can be re-read")
    queued = conn.execute(
        "UPDATE sources SET status = 'pending' WHERE id = ? AND status != 'processing' AND collecting = 0",
        (source_id,),
    ).rowcount
    conn.commit()
    if queued:  # while it is being read nothing is queued; a second click finds it already claimed
        background.add_task(_process_in_background, source_id, False)
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


@app.post("/sources/{source_id}/delete")
def delete_source(conn: Conn, source_id: int):
    with flyers.flyer_lock(source_id):
        source = conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone()
        if source is None:
            raise HTTPException(404)
        if source["status"] == "processing":
            raise HTTPException(409, "This flyer is being read right now; delete it once it has finished")
        flyers.delete_flyer_files(conn, source)
        conn.execute("DELETE FROM sources WHERE id = ?", (source_id,))
        conn.commit()
    return RedirectResponse("/flyers", status_code=303)


@app.post("/sources/{source_id}/finish")
def finish_draft(background: BackgroundTasks, conn: Conn, source_id: int):
    """Read a flyer the browser add-on was still adding pages to."""
    with flyers.flyer_lock(source_id):
        source = conn.execute("SELECT * FROM sources WHERE id = ? AND kind = 'flyer'", (source_id,)).fetchone()
        if source is None:
            raise HTTPException(404)
        if not flyers.source_files(source["file_path"] or ""):
            raise HTTPException(400, "This flyer has no pages yet")
        # A stale "Read now" button (the add-on already sent it) must not start a second reading.
        if flyers.close_draft(conn, source_id):
            background.add_task(_process_in_background, source_id, False)
    return RedirectResponse(f"/sources/{source_id}", status_code=303)


# --- review ----------------------------------------------------------------------------------------


def _product_options(conn) -> list:
    return conn.execute("SELECT id, slug, name_es, name_ca, base_unit FROM products ORDER BY name_es").fetchall()


@app.get("/review", response_class=HTMLResponse)
def review(request: Request, conn: Conn):
    return render(request, "review.html", rows=queries.review_queue(conn), products=_product_options(conn))


_SLUG_IN_BRACKETS = re.compile(r"\[([a-z0-9-]+)\]\s*$")


@app.post("/observations/{obs_id}/assign")
def assign(conn: Conn, obs_id: int, product: Annotated[str, Form()], back: Annotated[str, Form()] = "/review"):
    product = product.strip()
    if not product:
        product_id = None
    else:
        m = _SLUG_IN_BRACKETS.search(product)
        row = conn.execute(
            "SELECT id FROM products WHERE slug = ? OR lower(name_es) = lower(?) OR lower(name_ca) = lower(?)",
            (m.group(1) if m else product, product, product),
        ).fetchone()
        if row is None:
            raise HTTPException(400, f"Unknown product {product!r}; create it on the Products page first")
        product_id = row["id"]
    reassign_product(conn, obs_id, product_id)
    return RedirectResponse(_local(back) + f"#obs-{obs_id}", status_code=303)


@app.post("/observations/{obs_id}/ok")
def mark_ok(conn: Conn, obs_id: int, back: Annotated[str, Form()] = "/review"):
    conn.execute("UPDATE observations SET needs_review = 0 WHERE id = ?", (obs_id,))
    conn.commit()
    return RedirectResponse(_local(back), status_code=303)


@app.post("/observations/{obs_id}/delete")
def delete_observation(conn: Conn, obs_id: int, back: Annotated[str, Form()] = "/review"):
    conn.execute("DELETE FROM observations WHERE id = ?", (obs_id,))
    conn.commit()
    return RedirectResponse(_local(back), status_code=303)


def _local(path: str) -> str:
    """Only redirect within this app."""
    return path if path.startswith("/") and not path.startswith("//") else "/review"


# --- browser add-on --------------------------------------------------------------------------------


@app.get("/extension", response_class=HTMLResponse)
def extension_page(request: Request):
    return render(request, "extension.html", token=request.app.state.token, app_url=str(request.base_url).rstrip("/"))


@app.get("/extension/pricetracker-addon.zip")
def extension_zip():
    """The add-on folder as a zip, ready for about:debugging or `web-ext sign`."""
    import io
    import zipfile

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(EXTENSION_DIR.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, path.relative_to(EXTENSION_DIR).as_posix())
    return Response(buf.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": "attachment; filename=pricetracker-addon.zip"})


# --- import ----------------------------------------------------------------------------------------


@app.get("/import", response_class=HTMLResponse)
def import_page(request: Request, conn: Conn, result: str = ""):
    return render(request, "import.html", stores=_stores(conn), result=result, today=date.today().isoformat())


@app.get("/import/template.csv", response_class=PlainTextResponse)
def csv_template():
    return PlainTextResponse(CSV_TEMPLATE, headers={"Content-Disposition": "attachment; filename=prices-template.csv"})


@app.post("/import/csv", response_class=HTMLResponse)
async def upload_csv(request: Request, conn: Conn, file: Annotated[UploadFile, File()], store: Annotated[str, Form()] = ""):
    raw = await file.read()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = raw.decode("latin-1")
    counts = import_csv(conn, text, default_store=store or None, origin=file.filename or "csv")
    return render(request, "import.html", stores=_stores(conn), counts=counts, today=date.today().isoformat())


@app.post("/import/manual")
def manual_entry(
    conn: Conn,
    store: Annotated[str, Form()],
    name: Annotated[str, Form()],
    price: Annotated[str, Form()],
    quantity: Annotated[str, Form()] = "",
    promo: Annotated[str, Form()] = "",
    observed_on: Annotated[str, Form()] = "",
):
    from .units import parse_price

    value = parse_price(price)
    if not value:
        raise HTTPException(400, "Enter a price, e.g. 1,99")
    obs_id = add_manual(conn, store=store, raw_name=name, price=value, quantity_text=quantity or None,
                        promo_text=promo or None, observed_on=observed_on or None)
    product = conn.execute("SELECT product_id FROM observations WHERE id = ?", (obs_id,)).fetchone()["product_id"]
    if product:
        return RedirectResponse(f"/products/{product}", status_code=303)
    return RedirectResponse("/review", status_code=303)
