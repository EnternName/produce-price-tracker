"""JSON API used by the browser add-on (all routes need the X-Pricetracker-Token header; see security.py).

A complete file (a PDF) or a page's text is sent in one request, so closing the add-on's popup in
the middle cannot leave a half-sent flyer behind:

    POST   /api/flyers/send           {store, urls: [...] | text, page_url?, ...} -> flyer being read

Screenshots and viewer page images can be collected over several sends into a draft:

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
import re
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel, Field, field_validator

from . import __version__, capture, db, flyers

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api")

MAX_UPLOAD_BYTES = 40 * 1024 * 1024
MAX_TEXT_CHARS = 400_000
MIN_TEXT_CHARS = 20
_LONE_SURROGATE = re.compile("[\ud800-\udfff]")


def get_conn(request: Request):
    conn = db.connect(request.app.state.db_path)
    try:
        yield conn
    finally:
        conn.close()


Conn = Annotated[object, Depends(get_conn)]


def clean(value, limit: int):
    """Text from web pages: replace broken UTF-16 halves (an emoji cut in two) and cap the length.
    Over-long informational fields are shortened rather than rejected."""
    if not isinstance(value, str):
        return value
    return _LONE_SURROGATE.sub("�", value)[:limit]


def _clean_urls(v):
    if not isinstance(v, list):
        return v
    return [clean(u, capture.MAX_URL_LENGTH + 1) for u in v[: capture.MAX_URLS * 2] if isinstance(u, str)]


class PageInfo(BaseModel):
    page_url: str | None = None
    user_agent: str | None = None

    @field_validator("page_url", mode="before")
    @classmethod
    def check_page_url(cls, v):
        return clean(v, 2000) or None

    @field_validator("user_agent", mode="before")
    @classmethod
    def check_user_agent(cls, v):
        return clean(v, 400) or None


class NewFlyer(PageInfo):
    store: str
    title: str | None = None
    valid_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    valid_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("store", mode="before")
    @classmethod
    def check_store(cls, v):
        v = clean(v, 80)
        if not isinstance(v, str) or not v.strip():
            raise ValueError("choose a store")
        return v.strip()

    @field_validator("title", mode="before")
    @classmethod
    def check_title(cls, v):
        v = clean(v, 200)
        if isinstance(v, str):
            return v.strip() or None
        return v


class SendFlyer(NewFlyer):
    urls: list[str] | None = None
    text: str | None = None

    @field_validator("urls", mode="before")
    @classmethod
    def check_urls(cls, v):
        return _clean_urls(v)

    @field_validator("text", mode="before")
    @classmethod
    def check_text(cls, v):
        return clean(v, MAX_TEXT_CHARS)


class FlyerUrls(PageInfo):
    urls: list[str] = Field(min_length=1)

    @field_validator("urls", mode="before")
    @classmethod
    def check_urls(cls, v):
        return _clean_urls(v)


class PageText(PageInfo):
    text: str = Field(min_length=MIN_TEXT_CHARS)

    @field_validator("text", mode="before")
    @classmethod
    def check_text(cls, v):
        return clean(v, MAX_TEXT_CHARS)


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
        raise HTTPException(409, "This flyer is no longer accepting pages (it was already sent to be read)")
    return row


def _summary(conn, row, request: Request) -> dict:
    files = flyers.source_files(row["file_path"]) if row["file_path"] else []
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


def _blob_name(item: capture.Fetched) -> str:
    stem = Path(urlsplit(item.url).path).stem or "page"
    return stem + capture.suffix_for(item)


def _saved_pages(file_path: str) -> list[capture.Fetched]:
    origins = flyers.page_origins(Path(file_path))
    pages = []
    for path in flyers.source_files(file_path):
        if path.suffix == flyers.TEXT_SUFFIX:
            continue
        try:
            item = capture.inspect(origins.get(path.name, path.name), path.read_bytes())
        except ValueError:
            continue
        item.path = str(path)
        pages.append(item)
    return pages


def _save_downloaded(conn, flyer_id: int, file_path: str, items: list[capture.Fetched]) -> None:
    written = flyers.add_flyer_files(conn, flyer_id, blobs=[(_blob_name(f), f.data) for f in items])
    flyers.record_origins(Path(file_path), {path.name: item.url for path, item in zip(written, items, strict=True)})


def _process(db_path, flyer_id: int) -> None:
    conn = db.connect(db_path)
    try:
        flyers.process_flyer(conn, flyer_id)
    except flyers.AlreadyProcessing:
        log.info("flyer %s is already being read", flyer_id)
    except Exception:
        log.exception("flyer %s failed", flyer_id)
    finally:
        conn.close()


@router.get("/ping")
def ping():
    return {"app": "pricetracker", "version": __version__}


@router.get("/stores")
def stores(conn: Conn):
    return [{"slug": r["slug"], "name": r["name"]} for r in conn.execute("SELECT slug, name FROM stores ORDER BY name")]


@router.post("/flyers/send", status_code=201)
def send(body: SendFlyer, conn: Conn, request: Request, background: BackgroundTasks):
    """Create a flyer from complete content and start reading it, all in one request."""
    if bool(body.urls) == bool(body.text):
        raise HTTPException(422, "Send either links (urls) or page text (text)")
    skipped: list[dict] = []
    if body.urls:
        fetched, skipped = capture.download_all(body.urls, page_url=body.page_url, user_agent=body.user_agent)
        picked = capture.select_pages(fetched)
        skipped += picked.skipped
        if not picked.files:
            why = skipped[0]["reason"] if skipped else "nothing usable"
            raise HTTPException(422, f"The app could not use that ({why}). Try a screenshot instead.")
        blobs = [(_blob_name(f), f.data) for f in picked.files]
    else:
        if len(body.text.strip()) < MIN_TEXT_CHARS:
            raise HTTPException(422, "There is hardly any text on this page")
        blobs = [("page.txt", flyers.page_text_blob(body.text, body.page_url))]
    flyer_id = flyers.create_flyer_source(
        conn, store=body.store, title=body.title, origin=body.page_url,
        valid_from=body.valid_from, valid_to=body.valid_to,
    )
    if body.urls:
        _save_downloaded(conn, flyer_id, _flyer(conn, flyer_id)["file_path"], picked.files)
    else:
        flyers.add_flyer_files(conn, flyer_id, blobs=blobs)
    background.add_task(_process, request.app.state.db_path, flyer_id)
    return {
        **_summary(conn, _flyer(conn, flyer_id), request),
        "added": len(blobs),
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
        "chunks": len(flyers.chunk_text(body.text)) if body.text else None,
    }


@router.post("/flyers", status_code=201)
def create_flyer(body: NewFlyer, conn: Conn, request: Request):
    flyer_id = flyers.create_flyer_source(
        conn, store=body.store, title=body.title, origin=body.page_url,
        valid_from=body.valid_from, valid_to=body.valid_to, collecting=True,
    )
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.get("/flyers/{flyer_id}")
def flyer_status(flyer_id: int, conn: Conn, request: Request):
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.post("/flyers/{flyer_id}/urls")
def add_urls(flyer_id: int, body: FlyerUrls, conn: Conn, request: Request):
    _draft(conn, flyer_id)
    # Downloading can take a while; the draft is re-checked (and compared with its pages as they are
    # by then) only when the results are written.
    fetched, skipped = capture.download_all(body.urls, page_url=body.page_url, user_agent=body.user_agent)
    with flyers.flyer_lock(flyer_id):
        row = _draft(conn, flyer_id)
        result = capture.select_pages(fetched, _saved_pages(row["file_path"]))
        for old, new in result.replaced:
            path = flyers.replace_flyer_file(Path(old.path), _blob_name(new), new.data)
            flyers.record_origins(path.parent, {path.name: new.url})
        _save_downloaded(conn, flyer_id, row["file_path"], result.files)
    skipped += result.skipped
    return {
        **_summary(conn, _flyer(conn, flyer_id), request),
        "added": len(result.files),
        "replaced": len(result.replaced),
        "added_pdfs": sum(f.kind == "pdf" for f in result.files),
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
    }


@router.post("/flyers/{flyer_id}/pages")
async def add_pages(flyer_id: int, conn: Conn, request: Request, files: Annotated[list[UploadFile], File()]):
    _draft(conn, flyer_id)
    blobs = []
    for upload in files:
        name = Path(upload.filename or "page.png").name
        data = await upload.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, f"{name} is larger than 40 MB")
        blobs.append((name, data))
    with flyers.flyer_lock(flyer_id):
        _draft(conn, flyer_id)
        try:
            flyers.add_flyer_files(conn, flyer_id, blobs=blobs)
        except flyers.UnsupportedFile as e:
            raise HTTPException(400, str(e)) from e
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.post("/flyers/{flyer_id}/text")
def add_text(flyer_id: int, body: PageText, conn: Conn, request: Request):
    """The visible text of a store page that lists products and prices (cheaper and more exact than
    reading screenshots of it)."""
    with flyers.flyer_lock(flyer_id):
        _draft(conn, flyer_id)
        flyers.add_flyer_files(conn, flyer_id, blobs=[("page.txt", flyers.page_text_blob(body.text, body.page_url))])
    return {**_summary(conn, _flyer(conn, flyer_id), request), "chunks": len(flyers.chunk_text(body.text))}


@router.post("/flyers/{flyer_id}/finish")
def finish(flyer_id: int, conn: Conn, request: Request, background: BackgroundTasks):
    with flyers.flyer_lock(flyer_id):
        row = _draft(conn, flyer_id)
        if not flyers.source_files(row["file_path"]):
            raise HTTPException(422, "Add at least one page, PDF or image first")
        if not flyers.close_draft(conn, flyer_id):
            raise HTTPException(409, "This flyer was already sent to be read")
    background.add_task(_process, request.app.state.db_path, flyer_id)
    return _summary(conn, _flyer(conn, flyer_id), request)


@router.delete("/flyers/{flyer_id}")
def discard(flyer_id: int, conn: Conn):
    with flyers.flyer_lock(flyer_id):
        row = _draft(conn, flyer_id)
        flyers.delete_flyer_files(conn, row)
        conn.execute("DELETE FROM sources WHERE id = ?", (flyer_id,))
        conn.commit()
    return {"deleted": flyer_id}
