"""JSON API used by the browser add-on (all routes need the X-Pricetracker-Token header; see security.py).

A flyer is sent in up to three steps, so screenshots can be added one page at a time:

    POST   /api/flyers                {store, title?, page_url?, valid_from?, valid_to?} -> draft flyer
    POST   /api/flyers/{id}/urls      {urls: [...], page_url?, user_agent?}  links the add-on found
    POST   /api/flyers/{id}/pages     multipart files (screenshots, PDFs, images)
    POST   /api/flyers/{id}/text      {text, page_url?}  visible text of a page listing offers
    POST   /api/flyers/{id}/finish    close the draft and start reading it with Claude
    DELETE /api/flyers/{id}           discard a draft
    GET    /api/flyers/{id}           progress
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field

from . import __version__, capture, db, flyers

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
UPLOAD_TYPES = flyers.IMAGE_SUFFIXES | {".pdf"}


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[object, Depends(get_conn)]


class NewFlyer(BaseModel):
    store: str = Field(min_length=1, max_length=80)
    title: str | None = Field(default=None, max_length=200)
    page_url: str | None = Field(default=None, max_length=2000)
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")


class PageText(BaseModel):
    text: str = Field(min_length=20, max_length=MAX_TEXT_CHARS)
    page_url: str | None = Field(default=None, max_length=2000)


class FlyerUrls(BaseModel):
    urls: list[Annotated[str, Field(max_length=4000)]] = Field(min_length=1, max_length=capture.MAX_URLS * 2)
    page_url: str | None = Field(default=None, max_length=2000)
    user_agent: str | None = Field(default=None, max_length=400)


def _flyer(conn, flyer_id: int):
    row = conn.execute(
        "SELECT src.*, s.name AS store_name FROM sources src JOIN stores s ON s.id = src.store_id WHERE src.id = ? AND kind = 'flyer'",
        (flyer_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(404, "No such flyer")
    return row


def _draft(conn, flyer_id: int):
    row = _flyer(conn, flyer_id)
    if not row["collecting"]:
        raise HTTPException(409, "This flyer is no longer accepting pages")
    return row


def _summary(conn, row, request: Request) -> dict:
    files = flyers.source_files(row["file_path"]) if row["file_path"] and Path(row["file_path"]).exists() else []
    counts = conn.execute(
        "SELECT COUNT(*) AS items, COALESCE(SUM(needs_review), 0) AS review FROM observations WHERE source_id = ?",
        (row["id"],),
    ).fetchone()
    return {
        "id": row["id"],
        "store": row["store_name"],
        "title": row["title"],
        "collecting": bool(row["collecting"]),
        "status": row["status"],
        "files": len(files),
        "pages_total": row["pages_total"],
        "pages_done": row["pages_done"],
        "items": counts["items"],
        "needs_review": counts["review"],
        "error": row["error"],
        "url": str(request.base_url).rstrip("/") + f"/sources/{row['id']}",
    }


@router.get("/ping")
def ping():
    return {"app": "pricetracker", "version": __version__}


@router.get("/stores")
def stores(conn: Conn):
    return [{"slug": r["slug"], "name": r["name"]} for r in conn.execute("SELECT slug, name FROM stores ORDER BY name")]


@router.post("/flyers", status_code=201)
def create_flyer(body: NewFlyer, conn: Conn, request: Request):
    flyer_id = flyers.create_flyer_source(
        conn, store=body.store.strip(), title=(body.title or "").strip() or None, origin=body.page_url,
        valid_from=body.valid_from, valid_to=body.valid_to, collecting=True,
    )
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.get("/flyers/{flyer_id}")
def flyer_status(flyer_id: int, conn: Conn, request: Request):
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.post("/flyers/{flyer_id}/urls")
def add_urls(flyer_id: int, body: FlyerUrls, conn: Conn, request: Request):
    row = _draft(conn, flyer_id)
    existing = []
    for path in flyers.source_files(row["file_path"]):
        try:
            existing.append(capture.inspect(str(path), path.read_bytes()))
        except ValueError:
            pass
    result = capture.collect(body.urls, page_url=body.page_url, user_agent=body.user_agent, existing=existing)
    flyers.add_flyer_files(
        conn, flyer_id,
        blobs=[(f"{Path(f.url.split('?')[0]).stem or 'page'}{capture.suffix_for(f)}", f.data) for f in result.files],
    )
    return {
        **_summary(conn, _flyer(conn, flyer_id), request),
        "added": len(result.files),
        "added_pdfs": sum(f.kind == "pdf" for f in result.files),
        "skipped": result.skipped[:50],
        "skipped_count": len(result.skipped),
    }


@router.post("/flyers/{flyer_id}/pages")
async def add_pages(flyer_id: int, conn: Conn, request: Request, files: Annotated[list[UploadFile], File()]):
    _draft(conn, flyer_id)
    blobs = []
    for upload in files:
        name = Path(upload.filename or "page.png").name
        if Path(name).suffix.lower() not in UPLOAD_TYPES:
            raise HTTPException(400, f"{name}: send PDF, PNG, JPEG, WebP or AVIF files")
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{name} is larger than 40 MB")
        blobs.append((name, data))
    flyers.add_flyer_files(conn, flyer_id, blobs=blobs)
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.post("/flyers/{flyer_id}/text")
def add_text(flyer_id: int, body: PageText, conn: Conn, request: Request):
    """The visible text of a store page that lists products and prices (cheaper and more exact than
    reading screenshots of it)."""
    _draft(conn, flyer_id)
    flyers.add_flyer_files(conn, flyer_id, blobs=[("page.txt", flyers.page_text_blob(body.text, body.page_url))])
    return {**_summary(conn, _flyer(conn, flyer_id), request), "chunks": len(flyers.chunk_text(body.text))}


def _process(db_path, flyer_id: int) -> None:
    conn = db.connect(db_path)
    try:
        flyers.process_flyer(conn, flyer_id)
    except Exception:
        log.exception("flyer %s failed", flyer_id)
    finally:
        conn.close()


@router.post("/flyers/{flyer_id}/finish")
def finish(flyer_id: int, conn: Conn, request: Request, background: BackgroundTasks):
    row = _draft(conn, flyer_id)
    if not flyers.source_files(row["file_path"]):
        raise HTTPException(422, "Add at least one page, PDF or image first")
    conn.execute("UPDATE sources SET collecting = 0 WHERE id = ?", (flyer_id,))
    conn.commit()
    background.add_task(_process, request.app.state.db_path, flyer_id)
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.delete("/flyers/{flyer_id}")
def discard(flyer_id: int, conn: Conn):
    row = _draft(conn, flyer_id)
    flyers.delete_flyer_files(conn, row)
    conn.execute("DELETE FROM sources WHERE id = ?", (flyer_id,))
    conn.commit()
    return {"deleted": flyer_id}
