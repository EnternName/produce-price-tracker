"""Flyer ingestion: a PDF or a set of screenshots/photos -> page images -> offers -> observations."""

from __future__ import annotations

import io
import logging
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from PIL import Image

from . import db
from .extract import ClaudeExtractor, ExtractionError, Extractor, PageInput
from .pricing import RawOffer, build_observation, insert_observation
from .textnorm import fold
from .units import parse_promo

log = logging.getLogger(__name__)

UPLOAD_DIR = db.DEFAULT_DB_PATH.parent / "flyers"
MAX_SIDE = 2000  # px; long edge sent to the model
MAX_ASPECT = 1.6  # taller images (long phone screenshots) are sliced into overlapping tiles
TILE_OVERLAP = 0.08
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}


def _jpeg(img: Image.Image) -> bytes:
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85, optimize=True)
    return buf.getvalue()


def _fit(img: Image.Image, max_side: int = MAX_SIDE) -> Image.Image:
    scale = max_side / max(img.size)
    if scale < 1:
        img = img.resize((round(img.width * scale), round(img.height * scale)), Image.LANCZOS)
    return img


def slice_image(img: Image.Image) -> list[bytes]:
    """Fit an image for the model; slice very tall ones (scrolling screenshots) so text stays legible."""
    if img.height <= img.width * MAX_ASPECT:
        return [_jpeg(_fit(img))]
    tile_h = int(img.width * MAX_ASPECT)
    step = int(tile_h * (1 - TILE_OVERLAP))
    tiles, top = [], 0
    while True:
        bottom = min(top + tile_h, img.height)
        tiles.append(_jpeg(_fit(img.crop((0, top, img.width, bottom)))))
        if bottom >= img.height:
            return tiles
        top += step


def load_pages(paths: list[Path]) -> list[PageInput]:
    """PDF pages (with their text layer) and images, in order, as model-ready pages."""
    import pypdfium2 as pdfium

    pages: list[PageInput] = []
    for path in paths:
        if path.suffix.lower() == ".pdf":
            pdf = pdfium.PdfDocument(str(path))
            try:
                for i in range(len(pdf)):
                    page = pdf[i]
                    width, height = page.get_size()
                    scale = min(MAX_SIDE / max(width, height), 4.0)
                    img = page.render(scale=scale).to_pil()
                    try:
                        text = page.get_textpage().get_text_range()
                    except Exception:  # some PDFs have no usable text layer
                        text = ""
                    pages.append(PageInput(len(pages) + 1, slice_image(img), text))
            finally:
                pdf.close()
        elif path.suffix.lower() in IMAGE_SUFFIXES:
            with Image.open(path) as img:
                img.load()
                pages.append(PageInput(len(pages) + 1, slice_image(img)))
        else:
            raise ValueError(f"Unsupported file type: {path.name} (use PDF, PNG, JPEG or WebP)")
    return pages


def create_flyer_source(
    conn: sqlite3.Connection,
    *,
    store: str,
    files: list[Path],
    title: str | None = None,
    observed_on: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    origin: str | None = None,
    copy_files: bool = True,
) -> int:
    """Register a flyer (copying its files under data/flyers/<id>/) and return the source id."""
    sid = db.store_id(conn, store)
    cur = conn.execute(
        """INSERT INTO sources (store_id, kind, title, origin, observed_on, valid_from, valid_to, status)
           VALUES (?, 'flyer', ?, ?, ?, ?, ?, 'pending')""",
        (sid, title or files[0].name, origin or ", ".join(f.name for f in files),
         observed_on or valid_from or date.today().isoformat(), valid_from, valid_to),
    )
    source_id = cur.lastrowid
    folder = UPLOAD_DIR / str(source_id)
    if copy_files:
        folder.mkdir(parents=True, exist_ok=True)
        for i, f in enumerate(files):
            shutil.copy(f, folder / f"{i:03d}_{f.name}")
    else:
        folder = files[0].parent
    conn.execute("UPDATE sources SET file_path = ? WHERE id = ?", (str(folder), source_id))
    conn.commit()
    return source_id


def _source_files(file_path: str) -> list[Path]:
    path = Path(file_path)
    if path.is_file():
        return [path]
    return sorted(p for p in path.iterdir() if p.suffix.lower() in IMAGE_SUFFIXES | {".pdf"})


def _offers_from_page(data: dict, page: int, include_non_food: bool) -> list[RawOffer]:
    offers, seen = [], set()
    for item in data.get("items", []):
        if not include_non_food and not item.get("is_food", True):
            continue
        price = item.get("price")
        if price is None or price <= 0:
            continue
        key = (fold(item.get("name_printed") or ""), round(price, 2), fold(item.get("quantity_text") or ""))
        if key in seen:  # overlapping slices can repeat an offer
            continue
        seen.add(key)
        base = RawOffer(
            raw_name=item.get("name_printed") or item.get("generic_name_es") or "?",
            price=float(price),
            generic_name=item.get("generic_name_es"),
            brand=item.get("brand"),
            category_hint=item.get("category"),
            quantity_text=item.get("quantity_text"),
            pack_count=item.get("pack_count"),
            unit_amount=item.get("unit_amount"),
            unit=item.get("unit"),
            price_basis=item.get("price_basis") or "item",
            approx=bool(item.get("approx_weight")),
            regular_price=item.get("regular_price"),
            printed_unit_price=item.get("printed_unit_price"),
            printed_unit=item.get("printed_unit"),
            promo_text=item.get("promo_text"),
            page=page,
        )
        loyalty_price = item.get("loyalty_price")
        if loyalty_price and loyalty_price < price:
            # The promo label describes the card/app offer; keep it off the price everyone pays.
            label = base.promo_text or "precio con tarjeta/app"
            promo = parse_promo(base.promo_text)
            if promo and promo.loyalty_only:
                base.promo_text = None
            offers.append(base)
            offers.append(RawOffer(**{**base.__dict__, "price": float(loyalty_price),
                                      "regular_price": float(price), "promo_text": f"{label} (socio)"}))
        else:
            offers.append(base)
    return offers


def process_flyer(
    conn: sqlite3.Connection,
    source_id: int,
    *,
    extractor: Extractor | None = None,
    include_non_food: bool = False,
    workers: int = 3,
) -> dict:
    """Extract every page of a registered flyer and store its offers. Safe to re-run (replaces items)."""
    src = conn.execute(
        "SELECT src.*, s.name AS store_name FROM sources src JOIN stores s ON s.id = src.store_id WHERE src.id = ?",
        (source_id,),
    ).fetchone()
    if src is None:
        raise KeyError(source_id)
    conn.execute("UPDATE sources SET status = 'processing', error = NULL, pages_done = 0 WHERE id = ?", (source_id,))
    conn.execute("DELETE FROM observations WHERE source_id = ?", (source_id,))
    conn.commit()

    try:
        pages = load_pages(_source_files(src["file_path"]))
        conn.execute("UPDATE sources SET pages_total = ? WHERE id = ?", (len(pages), source_id))
        conn.commit()
        extractor = extractor or ClaudeExtractor(include_non_food=include_non_food)
    except Exception as e:  # unreadable file, missing API credentials...
        conn.execute("UPDATE sources SET status = 'error', error = ? WHERE id = ?", (str(e), source_id))
        conn.commit()
        raise

    matcher = db.load_matcher(conn)
    units = db.product_units(conn)
    today = date.today().isoformat()
    totals = {"items": 0, "pages_failed": 0, "input_tokens": 0, "output_tokens": 0}
    languages: dict[str, int] = {}
    valid_from, valid_to = src["valid_from"], src["valid_to"]
    errors: list[str] = []

    def run(page: PageInput):
        return page, extractor.extract(page, store_name=src["store_name"], total_pages=len(pages), today=today)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(run, p) for p in pages]
        for future in futures:
            try:
                page, result = future.result()
            except Exception as e:  # one bad page must not stop the rest
                totals["pages_failed"] += 1
                errors.append(str(e) if isinstance(e, ExtractionError) else f"{type(e).__name__}: {e}")
                log.warning("flyer %s: %s", source_id, e, exc_info=not isinstance(e, ExtractionError))
                continue
            data = result.data
            totals["input_tokens"] += result.input_tokens
            totals["output_tokens"] += result.output_tokens
            languages[data.get("language", "other")] = languages.get(data.get("language", "other"), 0) + 1
            valid_from = valid_from or data.get("valid_from")
            valid_to = valid_to or data.get("valid_to")
            for offer in _offers_from_page(data, page.number, include_non_food):
                offer.valid_from = offer.valid_from or valid_from
                offer.valid_to = offer.valid_to or valid_to
                row = build_observation(
                    offer, matcher=matcher, product_units=units, source_id=source_id,
                    store_id=src["store_id"], observed_on=src["observed_on"],
                )
                insert_observation(conn, row)
                totals["items"] += 1
            conn.execute("UPDATE sources SET pages_done = pages_done + 1 WHERE id = ?", (source_id,))
            conn.commit()

    language = max(languages, key=languages.get) if languages else None
    status = "error" if pages and totals["pages_failed"] == len(pages) else "done"
    error = None
    if errors:
        error = f"{totals['pages_failed']} of {len(pages)} pages failed: {errors[0]}"
    conn.execute(
        """UPDATE sources SET status = ?, error = ?, language = ?, valid_from = ?, valid_to = ?,
           input_tokens = ?, output_tokens = ? WHERE id = ?""",
        (status, error, language, valid_from, valid_to, totals["input_tokens"], totals["output_tokens"], source_id),
    )
    conn.execute(
        "UPDATE observations SET valid_from = COALESCE(valid_from, ?), valid_to = COALESCE(valid_to, ?) WHERE source_id = ?",
        (valid_from, valid_to, source_id),
    )
    conn.commit()
    return totals
