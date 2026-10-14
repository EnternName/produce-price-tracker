"""Turn what the browser add-on found on a flyer page (PDF links, page-image URLs) into flyer files.

Online flyer viewers load each page as a large image, alongside thumbnails, logos and zoom tiles, and
sometimes the same page at two resolutions. This downloads the candidates, keeps PDFs and images big
enough to be a flyer page, and drops near-duplicates (keeping the sharpest copy).
"""

from __future__ import annotations

import io
import ipaddress
import json
import os
import socket
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx
from PIL import Image

MAX_URLS = 150
MAX_BYTES = 40 * 1024 * 1024  # per file
MIN_PAGE_SIDE = 500  # px; smaller images are thumbnails, icons or zoom tiles
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"


class BlockedURL(ValueError):
    pass


def assert_public_url(url: str) -> None:
    """Refuse non-http(s) URLs and hosts on this machine or the local network (the app downloads
    whatever the add-on sends, so it must not be usable to reach a router or another local service).
    Set PRICETRACKER_ALLOW_PRIVATE_URLS=1 to allow them."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"not an http(s) link: {url[:80]}")
    if os.environ.get("PRICETRACKER_ALLOW_PRIVATE_URLS") == "1":
        return
    try:
        infos = socket.getaddrinfo(parts.hostname, None)
    except socket.gaierror:
        return  # can't resolve here (e.g. behind a resolving proxy); the download itself will tell
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise BlockedURL(f"{parts.hostname} is a local/private address")


@dataclass
class Fetched:
    url: str
    data: bytes
    kind: str  # "pdf" | "image"
    width: int = 0
    height: int = 0
    signature: bytes | None = None  # see signature(), for near-duplicate detection


@dataclass
class CaptureResult:
    files: list[Fetched] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)


SIGNATURE_SIDE = 48
DUPLICATE_MAX_DIFF = 4.0  # mean absolute grey-level difference (0-255) for "same page, other resolution"


def signature(img: Image.Image) -> bytes:
    """Tiny greyscale thumbnail used to spot the same page downloaded at two resolutions."""
    return img.convert("L").resize((SIGNATURE_SIDE, SIGNATURE_SIDE), Image.BILINEAR).tobytes()


def _similar(a: Fetched, b: Fetched) -> bool:
    """True only for near-identical pixels: pages that merely share a layout (same header, colours)
    still differ in their product photos and prices, so they are kept."""
    if a.signature is None or b.signature is None:
        return False
    ratio_a, ratio_b = a.width / a.height, b.width / b.height
    if abs(ratio_a - ratio_b) > 0.03 * max(ratio_a, ratio_b):
        return False
    diff = sum(abs(x - y) for x, y in zip(a.signature, b.signature, strict=True)) / len(a.signature)
    return diff <= DUPLICATE_MAX_DIFF


def inspect(url: str, data: bytes) -> Fetched:
    """Classify downloaded bytes; raises ValueError with a short reason if they are not usable."""
    if data[:5] == b"%PDF-":
        return Fetched(url, data, "pdf")
    try:
        with Image.open(io.BytesIO(data)) as img:
            img.load()
            width, height = img.size
            if min(width, height) < MIN_PAGE_SIDE:
                raise ValueError(f"too small ({width}×{height})")
            return Fetched(url, data, "image", width, height, signature(img))
    except ValueError:
        raise
    except Exception as e:  # PIL raises many types for non-images
        raise ValueError("not a PDF or a readable image") from e


def pdf_link_in_json(data: bytes) -> str | None:
    """The first PDF link inside a JSON document (flyer viewers often fetch one describing the flyer)."""
    if data[:1] not in (b"{", b"["):
        return None
    try:
        parsed = json.loads(data)
    except ValueError:
        return None
    from .connectors.lidl import find_leaflets

    leaflets = find_leaflets(parsed)
    return leaflets[0].pdf_url if leaflets else None


def _download(client: httpx.Client, url: str, referer: str | None) -> bytes:
    assert_public_url(url)
    headers = {"Referer": referer} if referer else {}
    with client.stream("GET", url, headers=headers) as response:
        response.raise_for_status()
        chunks, size = [], 0
        for chunk in response.iter_bytes():
            size += len(chunk)
            if size > MAX_BYTES:
                raise ValueError("file too large")
            chunks.append(chunk)
    return b"".join(chunks)


def collect(
    urls: list[str],
    *,
    page_url: str | None = None,
    user_agent: str | None = None,
    existing: list[Fetched] = (),
    client: httpx.Client | None = None,
) -> CaptureResult:
    """Download candidate URLs (in order) and keep the usable, distinct flyer pages."""
    result = CaptureResult()
    unique = list(dict.fromkeys(u.strip() for u in urls if u and u.strip()))
    if len(unique) > MAX_URLS:
        result.skipped += [{"url": u, "reason": f"over the {MAX_URLS}-link limit"} for u in unique[MAX_URLS:]]
        unique = unique[:MAX_URLS]

    own_client = client is None
    client = client or httpx.Client(
        headers={"User-Agent": user_agent or BROWSER_UA, "Accept": "image/avif,image/webp,image/*,application/pdf,*/*"},
        timeout=30, follow_redirects=True,
        event_hooks={"request": [lambda request: assert_public_url(str(request.url))]},  # redirects too
    )

    def fetch(url):
        try:
            data = _download(client, url, page_url)
            if pdf_url := pdf_link_in_json(data):
                # e.g. Lidl's viewer data (endpoints.leaflets.schwarz/v4/flyer) names the flyer's PDF
                return inspect(pdf_url, _download(client, pdf_url, page_url))
            return inspect(url, data)
        except (ValueError, httpx.HTTPError) as e:
            return {"url": url, "reason": str(e) or type(e).__name__}

    try:
        with ThreadPoolExecutor(max_workers=6) as pool:
            outcomes = list(pool.map(fetch, unique))
    finally:
        if own_client:
            client.close()

    kept: list[Fetched] = []
    known = list(existing)
    for outcome in outcomes:
        if isinstance(outcome, dict):
            result.skipped.append(outcome)
            continue
        if outcome.kind == "pdf":
            if any(k.kind == "pdf" and k.data == outcome.data for k in known + kept):
                result.skipped.append({"url": outcome.url, "reason": "duplicate PDF"})
            else:
                kept.append(outcome)
            continue
        twin = next((k for k in known + kept if k.kind == "image" and _similar(k, outcome)), None)
        if twin is None:
            kept.append(outcome)
        elif twin in kept and outcome.width * outcome.height > twin.width * twin.height:
            kept[kept.index(twin)] = outcome  # same page, sharper copy
            result.skipped.append({"url": twin.url, "reason": "lower-resolution copy of another page"})
        else:
            result.skipped.append({"url": outcome.url, "reason": "duplicate of another page"})
    result.files = kept
    return result


def suffix_for(item: Fetched) -> str:
    if item.kind == "pdf":
        return ".pdf"
    with Image.open(io.BytesIO(item.data)) as img:
        fmt = (img.format or "").lower()
    return {"jpeg": ".jpg", "png": ".png", "webp": ".webp", "gif": ".gif", "avif": ".avif"}.get(fmt, ".img")
