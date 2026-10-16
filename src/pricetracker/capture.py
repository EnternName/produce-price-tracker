"""Turn what the browser add-on found on a flyer page (PDF links, page-image URLs) into flyer files.

Online flyer viewers load each page as a large image, alongside thumbnails, logos and zoom tiles, and
sometimes the same page at two resolutions. This downloads the candidates, keeps PDFs and images big
enough to be a flyer page, and drops copies of the same page (keeping the sharpest one).

The URLs come from whatever web page the user scanned, so downloads are treated as untrusted: they
may only reach public internet addresses (checked on the address actually connected to, redirects
included), and per-file, per-batch and per-image-size limits keep a hostile page from exhausting memory.
"""

from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
import re
import socket
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image

MAX_URLS = 150
MAX_URL_LENGTH = 4000
MAX_BYTES = 40 * 1024 * 1024  # per file
MAX_BATCH_BYTES = 300 * 1024 * 1024  # per request
MAX_PIXELS = 30_000_000  # larger images are refused before decoding (a 1415×2400 flyer page is 3.4 Mpx)
MIN_PAGE_SIDE = 500  # px; smaller images are thumbnails, icons or zoom tiles
MAX_REDIRECTS = 5
MAX_SECONDS = 120  # per download, redirects included
MAX_PDF_PAGES = 150  # a weekly flyer has 10-60 pages; more is not a flyer (and would cost a lot to read)
IMAGE_FORMATS = ["JPEG", "PNG", "WEBP", "GIF", "AVIF", "BMP", "TIFF"]  # decoders allowed on downloads (JPEG covers MPO)
BROWSER_UA = "Mozilla/5.0 (X11; Linux x86_64; rv:140.0) Gecko/20100101 Firefox/140.0"
STORED_FORMATS = {"JPEG": ".jpg", "MPO": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif", "AVIF": ".avif"}


class BlockedURL(ValueError):
    pass


def _allow_private() -> bool:
    return os.environ.get("PRICETRACKER_ALLOW_PRIVATE_URLS") == "1"


def _blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    # is_global excludes private ranges, CGNAT/Tailscale 100.64/10, documentation and benchmark ranges
    return not ip.is_global or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved


def resolve_public_all(host: str) -> list[str]:
    """Every address of `host`, all public; BlockedURL if any is local/private or none resolves."""
    host = host.strip("[]")
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _blocked(literal):
            raise BlockedURL(f"{host} is a local/private address")
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as e:
        raise BlockedURL(f"can't resolve {host}") from e
    addresses = list(dict.fromkeys(ipaddress.ip_address(info[4][0]) for info in infos))
    if not addresses or any(_blocked(a) for a in addresses):
        raise BlockedURL(f"{host} is a local/private address")
    return [str(a) for a in addresses]


def resolve_public(host: str) -> str:
    return resolve_public_all(host)[0]


def assert_public_url(url: str) -> None:
    """Refuse non-http(s) URLs and hosts on this machine or the local network (the app downloads
    whatever the add-on sends, so it must not be usable to reach a router or another local service).
    Set PRICETRACKER_ALLOW_PRIVATE_URLS=1 to allow them."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise BlockedURL(f"not an http(s) link: {url[:80]}")
    if not _allow_private():
        resolve_public(parts.hostname)


class _Budget:
    """Bytes left for one batch of downloads, shared by the download threads."""

    def __init__(self, total: int):
        self.left = total
        self._lock = threading.Lock()

    def take(self, n: int) -> None:
        with self._lock:
            if n > self.left:
                raise ValueError("this batch is too large; send fewer pages at a time")
            self.left -= n


def new_client(user_agent: str | None = None) -> httpx.Client:
    """No proxies from the environment (they would resolve names themselves), no automatic redirects
    (each hop is checked by fetch_public) and no compression (see _read_body)."""
    return httpx.Client(
        headers={
            "User-Agent": user_agent or BROWSER_UA,
            "Accept": "image/avif,image/webp,image/*,application/pdf,*/*",
            "Accept-Encoding": "identity",
        },
        timeout=30, follow_redirects=False, trust_env=False,
    )


def _read_body(response: httpx.Response, max_bytes: int, budget: _Budget | None, deadline: float) -> bytes:
    """Read the raw body with every limit applied to the *decoded* size as it grows, so a few KB of
    gzip cannot expand into gigabytes (httpx would decode each chunk in full before any check)."""
    encoding = response.headers.get("content-encoding", "").strip().lower()
    if response.is_stream_consumed:  # an in-memory response (mock transports): already complete
        raw_chunks, encoding = [response.content], ""
    else:
        raw_chunks = response.iter_raw()
    if encoding in ("", "identity"):
        decoder = None
    elif encoding in ("gzip", "x-gzip"):
        decoder = zlib.decompressobj(16 + zlib.MAX_WBITS)
    elif encoding == "deflate":
        decoder = zlib.decompressobj()
    else:
        raise ValueError(f"unsupported content encoding ({encoding[:40]})")
    chunks, size = [], 0

    def keep(piece: bytes) -> None:
        nonlocal size
        size += len(piece)
        if size > max_bytes:
            raise ValueError("file too large")
        if budget:
            budget.take(len(piece))
        chunks.append(piece)

    for raw in raw_chunks:
        if time.monotonic() > deadline:
            raise ValueError("download took too long")
        if decoder is None:
            keep(raw)
            continue
        data = raw
        while data:
            try:
                keep(decoder.decompress(data, 64 * 1024))
            except zlib.error as e:
                raise ValueError("corrupt compressed download") from e
            data = decoder.unconsumed_tail
    return b"".join(chunks)


def fetch_public(
    url: str,
    *,
    client: httpx.Client,
    referer: str | None = None,
    max_bytes: int = MAX_BYTES,
    budget: _Budget | None = None,
    max_seconds: float = MAX_SECONDS,
) -> tuple[bytes, str, str]:
    """GET `url` from the public internet only: the host is resolved once, every address checked, and
    the connection is made to exactly those addresses (TLS still verifies the real host name), so a
    DNS answer that changes between check and connect cannot redirect it to the local network.
    Redirects are followed by hand with the same check. Returns (body, content type, final URL)."""
    deadline = time.monotonic() + max_seconds
    for _ in range(MAX_REDIRECTS + 1):
        try:
            parsed = httpx.URL(url)
        except httpx.InvalidURL as e:
            raise BlockedURL(f"invalid link: {e}") from e
        if parsed.scheme not in ("http", "https") or not parsed.raw_host:
            raise BlockedURL(f"not an http(s) link: {url[:80]}")
        host = parsed.raw_host.decode("ascii")  # punycode for international names
        headers = {"Referer": referer} if referer else {}
        targets: list[str | httpx.URL] = [url]
        extensions = {}
        if not _allow_private():
            targets = [parsed.copy_with(host=ip) for ip in resolve_public_all(host)]
            headers["Host"] = parsed.netloc.decode("ascii")
            # A pooled connection is keyed by IP and was verified for whichever host opened it:
            # never let a request for another host name reuse it.
            headers["Connection"] = "close"
            if parsed.scheme == "https":
                extensions["sni_hostname"] = host
        for i, target in enumerate(targets):
            try:
                with client.stream("GET", target, headers=headers, extensions=extensions) as response:
                    if response.is_redirect and "location" in response.headers:
                        url = urljoin(url, response.headers["location"])
                        break
                    response.raise_for_status()
                    body = _read_body(response, max_bytes, budget, deadline)
                    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    return body, content_type, url
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if i == len(targets) - 1:
                    raise  # every address failed
        else:
            continue
        if time.monotonic() > deadline:
            raise ValueError("download took too long")
    raise ValueError("too many redirects")


@dataclass
class Fetched:
    url: str
    data: bytes
    kind: str  # "pdf" | "image"
    width: int = 0
    height: int = 0
    signature: bytes | None = None  # see signature(), for spotting the same page at another size
    digest: str = ""
    suffix: str = ""
    path: str | None = None  # set for pages already saved in the flyer


@dataclass
class CaptureResult:
    files: list[Fetched] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)
    replaced: list[tuple[Fetched, Fetched]] = field(default_factory=list)  # (saved page, sharper copy)


SIGNATURE_SIDE = 96
ALIKE_MAX_MEAN_DIFF = 8.0  # grey levels (0-255), averaged over the thumbnail
_SIZE_PARAMS = {"w", "h", "width", "height", "size", "s", "q", "quality", "dpr", "scale", "format", "fm", "fit", "resize", "zoom"}
# Path segments that only say which size/quality to serve: "1415x2400", "w_1600", "w_600,q_auto",
# "rs:fit:1415:2400:1", "zoom". A bare number ("/10/") is often the page number, so it never matches.
_SIZE_SEGMENT = re.compile(
    r"^(\d{2,5}x\d{2,5}|(w|h|s|q|c|dpr|width|height|size)[_:-]\d+([,][a-z_]+[_:-]?\w*)*|[^/]*[:,][^/]*"
    r"|(thumb|thumbnail|small|medium|large|xlarge|zoom|hires|lowres|original|full|preview)s?)$",
    re.I,
)
_TOKEN_SEGMENT = re.compile(r"^[A-Za-z0-9_-]{20,}$")
_TOKEN_FILE = re.compile(r"^[A-Za-z0-9_=-]{24,}\.[a-z]{3,4}$", re.I)


def signature(img: Image.Image) -> bytes:
    """Small greyscale thumbnail (area-averaged), to check two images show the same picture."""
    return img.convert("L").resize((SIGNATURE_SIDE, SIGNATURE_SIDE), Image.BOX).tobytes()


def asset_key(url: str) -> str:
    """The URL with its size/quality parts blanked out, so `.../w_600/p1.jpg` and `.../w_1600/p1.jpg`,
    or `p1.jpg?width=1000` and `p1.jpg?width=2400`, share a key while `p1.jpg` and `p2.jpg` don't."""
    parts = urlsplit(url)
    segments = [seg for seg in parts.path.split("/") if seg]
    # A signature folder that changes with the size (imgproxy: /<signature>/rs:fit:../<base64 source>.jpg)
    # is only ignored when the file name itself identifies the source image.
    signed = bool(segments) and bool(_TOKEN_FILE.match(segments[-1]))
    keep = []
    for i, seg in enumerate(segments):
        last = i == len(segments) - 1
        if not last and (_SIZE_SEGMENT.match(seg) or (signed and _TOKEN_SEGMENT.match(seg))):
            keep.append("*")
        else:
            keep.append(seg)
    query = sorted(kv for kv in parts.query.split("&") if kv and kv.split("=")[0].lower() not in _SIZE_PARAMS)
    return f"{parts.netloc.lower()}/{'/'.join(keep)}?{'&'.join(query)}"


def _looks_alike(a: Fetched, b: Fetched) -> bool:
    ratio_a, ratio_b = a.width / a.height, b.width / b.height
    if abs(ratio_a - ratio_b) > 0.03 * max(ratio_a, ratio_b):
        return False
    diff = sum(abs(x - y) for x, y in zip(a.signature, b.signature, strict=True)) / len(a.signature)
    return diff <= ALIKE_MAX_MEAN_DIFF


def same_page(a: Fetched, b: Fetched) -> bool:
    """Identical files, or two sizes of the same image file.

    Viewers load each page as an overview image and again as a zoom image. Two *different* pages that
    share a template (header, colours, layout) can look nearly the same once shrunk, so pictures are
    only compared when the addresses say they are size variants of one file; losing a page would lose
    its prices, while keeping a duplicate only costs a little."""
    if a.digest and a.digest == b.digest:
        return True
    if a.signature is None or b.signature is None or not (a.width and a.height and b.width and b.height):
        return False
    if a.width == b.width and a.height == b.height:
        return False  # same size but different bytes: two pages
    if asset_key(a.url) != asset_key(b.url):
        return False
    return _looks_alike(a, b)


def inspect(url: str, data: bytes) -> Fetched:
    """Classify downloaded bytes; raises ValueError with a short reason if they are not usable.
    Images in formats the flyer reader doesn't take (BMP, TIFF...) are converted to PNG."""
    digest = hashlib.sha256(data).hexdigest()
    if data[:5] == b"%PDF-":
        import pypdfium2 as pdfium

        try:
            doc = pdfium.PdfDocument(data)
            pages = len(doc)
            doc.close()
        except Exception as e:
            raise ValueError("not a readable PDF") from e
        if pages > MAX_PDF_PAGES:
            raise ValueError(f"PDF has {pages} pages; a flyer has at most {MAX_PDF_PAGES}")
        return Fetched(url, data, "pdf", digest=digest, suffix=".pdf")
    try:
        img = Image.open(io.BytesIO(data), formats=IMAGE_FORMATS)
    except Exception as e:  # PIL raises many types for non-images
        raise ValueError("not a PDF or a readable image") from e
    with img:
        width, height = img.size
        if min(width, height) < MIN_PAGE_SIDE:
            raise ValueError(f"too small ({width}×{height})")
        if width * height > MAX_PIXELS:
            raise ValueError(f"image too large ({width}×{height})")
        fmt = img.format or ""
        try:
            img.load()
        except Exception as e:
            raise ValueError("not a readable image") from e
        suffix = STORED_FORMATS.get(fmt)
        if suffix is None:
            buf = io.BytesIO()
            img.convert("RGB").save(buf, format="PNG")
            data, suffix = buf.getvalue(), ".png"
        return Fetched(url, data, "image", width, height, signature(img), digest, suffix)


def pdf_link_in_json(data: bytes) -> str | None:
    """The first PDF link inside a JSON document (flyer viewers often fetch one describing the flyer)."""
    if data[:1] not in (b"{", b"["):
        return None
    from .connectors.lidl import find_leaflets

    try:
        leaflets = find_leaflets(json.loads(data))
    except (ValueError, RecursionError):  # not JSON, or nested absurdly deep
        return None
    url = leaflets[0].pdf_url if leaflets else None
    return url if isinstance(url, str) else None


def download_all(
    urls: list[str],
    *,
    page_url: str | None = None,
    user_agent: str | None = None,
    client: httpx.Client | None = None,
) -> tuple[list[Fetched], list[dict]]:
    """Download candidate URLs (in order). Returns usable files and the skipped URLs with reasons."""
    skipped: list[dict] = []
    unique = []
    for u in dict.fromkeys(u.strip() for u in urls if u and u.strip()):
        if len(u) > MAX_URL_LENGTH:
            skipped.append({"url": u[:200] + "…", "reason": "link too long"})
        else:
            unique.append(u)
    if len(unique) > MAX_URLS:
        skipped += [{"url": u, "reason": f"over the {MAX_URLS}-link limit"} for u in unique[MAX_URLS:]]
        unique = unique[:MAX_URLS]

    own_client = client is None
    client = client or new_client(user_agent)
    budget = _Budget(MAX_BATCH_BYTES)

    def fetch(url):
        try:
            data, _, final_url = fetch_public(url, client=client, referer=page_url, budget=budget)
            if pdf_url := pdf_link_in_json(data):
                # e.g. Lidl's viewer data (endpoints.leaflets.schwarz/v4/flyer) names the flyer's PDF
                pdf_url = urljoin(final_url, pdf_url)
                data, _, final_url = fetch_public(pdf_url, client=client, referer=page_url, budget=budget)
            return inspect(final_url, data)
        except (ValueError, httpx.HTTPError) as e:
            return {"url": url, "reason": str(e) or type(e).__name__}
        except Exception as e:  # whatever a hostile page serves must not fail the whole send
            return {"url": url, "reason": f"unusable ({type(e).__name__})"}

    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(fetch, unique))
    finally:
        if own_client:
            client.close()
    fetched = [o for o in outcomes if isinstance(o, Fetched)]
    skipped += [o for o in outcomes if isinstance(o, dict)]
    return fetched, skipped


def _larger(a: Fetched, b: Fetched) -> bool:
    return a.width * a.height > b.width * b.height


def select_pages(fetched: list[Fetched], existing: list[Fetched] = ()) -> CaptureResult:
    """Keep distinct pages, in order. A sharper copy of a page replaces the smaller one, also when
    the smaller one was saved by an earlier send (reported in `replaced`)."""
    result = CaptureResult()
    kept: list[Fetched] = []
    upgrades: dict[int, Fetched] = {}  # id(saved page) -> best new copy
    for item in fetched:
        twin = next((k for k in [*existing, *kept] if k.kind == item.kind and same_page(k, item)), None)
        if twin is None:
            kept.append(item)
        elif item.kind != "image":
            result.skipped.append({"url": item.url, "reason": "duplicate of another file"})
        elif twin in kept:
            if _larger(item, twin):
                kept[kept.index(twin)] = item
                result.skipped.append({"url": twin.url, "reason": "lower-resolution copy of another page"})
            else:
                result.skipped.append({"url": item.url, "reason": "duplicate of another page"})
        else:  # a page already saved in this flyer
            best = upgrades.get(id(twin), twin)
            if _larger(item, best):
                if best is not twin:
                    result.skipped.append({"url": best.url, "reason": "lower-resolution copy of another page"})
                upgrades[id(twin)] = item
            else:
                result.skipped.append({"url": item.url, "reason": "duplicate of a page already added"})
    result.files = kept
    result.replaced = [(page, upgrades[id(page)]) for page in existing if id(page) in upgrades]
    return result


def collect(
    urls: list[str],
    *,
    page_url: str | None = None,
    user_agent: str | None = None,
    existing: list[Fetched] = (),
    client: httpx.Client | None = None,
) -> CaptureResult:
    """download_all + select_pages."""
    fetched, skipped = download_all(urls, page_url=page_url, user_agent=user_agent, client=client)
    result = select_pages(fetched, existing)
    result.skipped = skipped + result.skipped
    return result


def suffix_for(item: Fetched) -> str:
    return item.suffix or (".pdf" if item.kind == "pdf" else ".png")
