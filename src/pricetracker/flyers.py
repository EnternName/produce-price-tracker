"""Flyer ingestion: a PDF or a set of screenshots/photos -> page images -> offers -> observations."""

from __future__ import annotations

import io
import json
import logging
import re
import secrets
import shutil
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
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
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".avif", ".gif", ".bmp", ".tif", ".tiff"}
TEXT_SUFFIX = ".txt"  # visible text of a store web page listing offers (from the browser add-on)
FLYER_SUFFIXES = IMAGE_SUFFIXES | {".pdf", TEXT_SUFFIX}
TEXT_CHUNK = 12000  # characters of page text per request
TEXT_OVERLAP = 400  # characters repeated between consecutive text pieces


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
    """PDF pages (with their text layer), images and saved page text, in order, as model-ready pages."""
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
        elif path.suffix.lower() == TEXT_SUFFIX:
            url, text = _read_page_text(path)
            for chunk in chunk_text(text):
                pages.append(PageInput(len(pages) + 1, [], chunk, extra={"url": url, "group": path.name}))
        else:
            raise ValueError(f"Unsupported file type: {path.name} (use PDF, PNG, JPEG or WebP)")
    return pages


def chunk_text(text: str, size: int = TEXT_CHUNK, overlap: int = TEXT_OVERLAP) -> list[str]:
    """Split page text at line breaks into pieces of at most `size` characters. Each piece repeats
    the last ~`overlap` characters of the previous one, so a product listed over several lines (name,
    size, price) is whole in at least one piece; offers repeated by the overlap are dropped later."""
    lines = []
    for line in text.splitlines():
        line = line.strip()
        lines += [line[i : i + size] for i in range(0, len(line), size)]  # enormous lines are cut
    chunks, current, length = [], [], 0
    for line in lines:
        if current and length + len(line) + 1 > size:
            chunks.append("\n".join(current))
            carry, carried = [], 0
            for previous in reversed(current):
                if carried + len(previous) + 1 > overlap or carried + len(previous) + len(line) + 2 > size:
                    break
                carry.insert(0, previous)
                carried += len(previous) + 1
            current, length = carry, carried
        current.append(line)
        length += len(line) + 1
    if current:
        chunks.append("\n".join(current))
    return chunks


PAGE_TEXT_HEADER = "# page: "


def page_text_blob(text: str, url: str | None) -> bytes:
    header = f"{PAGE_TEXT_HEADER}{url}\n" if url else ""
    return (header + text).encode("utf-8")


def _read_page_text(path: Path) -> tuple[str | None, str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    if text.startswith(PAGE_TEXT_HEADER):
        first, _, rest = text.partition("\n")
        return first[len(PAGE_TEXT_HEADER):].strip() or None, rest
    return None, text


def upload_dir(conn: sqlite3.Connection) -> Path:
    """data/flyers/ next to the database file (UPLOAD_DIR for in-memory databases)."""
    row = conn.execute("PRAGMA database_list").fetchone()
    return Path(row["file"]).parent / "flyers" if row and row["file"] else UPLOAD_DIR


def _safe_name(name: str) -> str:
    """A short file-system-safe name that keeps the extension."""
    path = Path(Path(name).name)
    suffix = path.suffix.lower()[:8]
    stem = re.sub(r"[^\w.\-]+", "_", path.stem)[:80].strip("._") or "page"
    return stem + suffix


class UnsupportedFile(ValueError):
    pass


def create_flyer_source(
    conn: sqlite3.Connection,
    *,
    store: str,
    files: list[Path] = (),
    title: str | None = None,
    observed_on: str | None = None,
    valid_from: str | None = None,
    valid_to: str | None = None,
    origin: str | None = None,
    collecting: bool = False,
) -> int:
    """Register a flyer (copying its files under data/flyers/<id>/) and return the source id.

    `collecting=True` opens a draft that more pages can be added to (add_flyer_files) before it is
    read; the browser add-on uses this to send a flyer one screenshot at a time."""
    files = list(files)
    sid = db.store_id(conn, store)
    cur = conn.execute(
        """INSERT INTO sources (store_id, kind, title, origin, observed_on, valid_from, valid_to, status, collecting)
           VALUES (?, 'flyer', ?, ?, ?, ?, ?, 'pending', ?)""",
        (sid, title or (files[0].name if files else "Folleto"), origin or ", ".join(f.name for f in files) or None,
         observed_on or valid_from or date.today().isoformat(), valid_from, valid_to, int(collecting)),
    )
    source_id = cur.lastrowid
    # Unique folder: ids are reused if the database is reset while old flyer files stay on disk.
    folder = upload_dir(conn) / f"{source_id}-{secrets.token_hex(3)}"
    folder.mkdir(parents=True, exist_ok=False)
    conn.execute("UPDATE sources SET file_path = ? WHERE id = ?", (str(folder), source_id))
    conn.commit()
    add_flyer_files(conn, source_id, files)
    return source_id


_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


@contextmanager
def flyer_lock(source_id: int):
    """Serialize changes to one flyer (adding pages, finishing, discarding) within this process."""
    with _locks_guard:
        lock = _locks.setdefault(source_id, threading.Lock())
    with lock:
        yield


def close_draft(conn: sqlite3.Connection, source_id: int) -> bool:
    """Stop a draft from taking more pages. True only for the one caller that actually closed it."""
    closed = conn.execute("UPDATE sources SET collecting = 0 WHERE id = ? AND collecting = 1", (source_id,)).rowcount
    conn.commit()
    return bool(closed)


def add_flyer_files(conn: sqlite3.Connection, source_id: int, files: list[Path] = (), blobs: list[tuple[str, bytes]] = ()) -> int:
    """Append files (paths, or (name, bytes) pairs) to a flyer, after the pages it already has.
    Returns the paths written."""
    row = conn.execute("SELECT file_path FROM sources WHERE id = ?", (source_id,)).fetchone()
    if row is None or not row["file_path"] or not Path(row["file_path"]).is_dir():
        raise KeyError(source_id)
    folder = Path(row["file_path"])
    names = [_safe_name(f.name) for f in files] + [_safe_name(name) for name, _ in blobs]
    if bad := [n for n in names if Path(n).suffix not in FLYER_SUFFIXES]:
        raise UnsupportedFile(f"{bad[0]}: send PDF, PNG, JPEG, WebP or AVIF files")
    index = _next_index(folder)
    written = []
    for f, name in zip(files, names, strict=False):
        written.append(folder / f"{index:04d}_{name}")
        shutil.copy(f, written[-1])
        index += 1
    for (_, data), name in zip(blobs, names[len(files):], strict=True):
        written.append(folder / f"{index:04d}_{name}")
        written[-1].write_bytes(data)
        index += 1
    return written


ORIGINS_FILE = "origins.json"  # page file name -> URL it was downloaded from


def page_origins(folder: Path) -> dict[str, str]:
    try:
        return json.loads((folder / ORIGINS_FILE).read_text())
    except (OSError, ValueError):
        return {}


def record_origins(folder: Path, origins: dict[str, str]) -> None:
    current = page_origins(folder)
    current.update(origins)
    (folder / ORIGINS_FILE).write_text(json.dumps(current, indent=1))


def _next_index(folder: Path) -> int:
    """One past the highest page number used in `folder` (counting replaced and odd files too)."""
    numbers = [int(p.name[:4]) for p in folder.iterdir() if p.name[:4].isdigit()]
    return max(numbers, default=-1) + 1


def replace_flyer_file(path: Path, name: str, data: bytes) -> Path:
    """Swap a saved page for a better copy, keeping its position in the flyer."""
    new_path = path.with_name(f"{path.name[:4]}_{_safe_name(name)}")
    if new_path.suffix not in FLYER_SUFFIXES:
        raise UnsupportedFile(name)
    new_path.write_bytes(data)
    if new_path != path:
        path.unlink(missing_ok=True)
    return new_path


def source_files(file_path: str) -> list[Path]:
    path = Path(file_path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        return []
    return sorted(p for p in path.iterdir() if p.suffix.lower() in FLYER_SUFFIXES)


def delete_flyer_files(conn: sqlite3.Connection, source) -> None:
    """Remove a flyer's stored files, but only from inside the app's upload folder."""
    if not source["file_path"]:
        return
    folder = Path(source["file_path"]).resolve()
    root = upload_dir(conn).resolve()
    if folder.is_dir() and folder.is_relative_to(root) and folder != root:
        shutil.rmtree(folder, ignore_errors=True)


def _offers_from_page(data: dict, page: int, include_non_food: bool, seen: set | None = None) -> list[RawOffer]:
    offers = []
    seen = set() if seen is None else seen
    for item in data.get("items", []):
        if not include_non_food and not item.get("is_food", True):
            continue
        price = item.get("price")
        if price is None or price <= 0:
            continue
        key = (fold(item.get("name_printed") or ""), round(price, 2), fold(item.get("quantity_text") or ""))
        if key in seen:  # overlapping image slices or text pieces can repeat an offer
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


class AlreadyProcessing(RuntimeError):
    pass


def reset_interrupted(conn: sqlite3.Connection) -> int:
    """Flyers left 'processing' when the app stopped can be read again (call at startup)."""
    n = conn.execute(
        "UPDATE sources SET status = 'error', error = 'Interrupted: the app stopped while reading this flyer' "
        "WHERE status = 'processing'"
    ).rowcount
    conn.commit()
    return n


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
    claimed = conn.execute(
        """UPDATE sources SET status = 'processing', collecting = 0, error = NULL, pages_done = 0
           WHERE id = ? AND status != 'processing'""",
        (source_id,),
    ).rowcount
    conn.commit()
    if not claimed:
        raise AlreadyProcessing(f"flyer {source_id} is already being read")
    conn.execute("DELETE FROM observations WHERE source_id = ?", (source_id,))
    conn.commit()

    try:
        pages = load_pages(source_files(src["file_path"]))
        if not pages:
            raise ValueError("This flyer has no pages yet")
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
    seen_by_group: dict[str, set] = {}

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
            group = page.extra.get("group")
            seen = seen_by_group.setdefault(group, set()) if group else None
            for offer in _offers_from_page(data, page.number, include_non_food, seen):
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
