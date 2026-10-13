"""Download a flyer PDF or image from a URL so it can go through the normal flyer pipeline."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

from . import get_with_retry, http_client

_CONTENT_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def download(url: str, dest_dir: Path | None = None, *, client=None) -> Path:
    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="flyer-"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    own_client = client is None
    client = client or http_client()
    try:
        response = get_with_retry(client, url)
    finally:
        if own_client:
            client.close()
    content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
    name = Path(unquote(urlparse(url).path)).name or "flyer"
    name = re.sub(r"[^\w.\-]+", "_", name)[:120]
    suffix = Path(name).suffix.lower()
    if suffix not in (".pdf", ".jpg", ".jpeg", ".png", ".webp"):
        if content_type not in _CONTENT_TYPES:
            raise ValueError(f"{url} is not a PDF or image (content-type {content_type or 'unknown'})")
        name += _CONTENT_TYPES[content_type]
    path = dest_dir / name
    path.write_bytes(response.content)
    return path
