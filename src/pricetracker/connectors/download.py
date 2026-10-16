"""Download a flyer PDF or image from a URL so it can go through the normal flyer pipeline."""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from urllib.parse import unquote, urlparse

_CONTENT_TYPES = {"application/pdf": ".pdf", "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def download(url: str, dest_dir: Path | None = None, *, client=None) -> Path:
    """Fetch `url` (public internet only, see capture.fetch_public) into `dest_dir`."""
    from ..capture import fetch_public, new_client

    own_client = client is None
    client = client or new_client()
    try:
        data, content_type, final_url = fetch_public(url, client=client)
    finally:
        if own_client:
            client.close()
    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="flyer-"))
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = Path(unquote(urlparse(final_url).path)).name or "flyer"
    stem, suffix = Path(name).stem, Path(name).suffix.lower()
    if suffix not in (".pdf", ".jpg", ".jpeg", ".png", ".webp"):
        if content_type not in _CONTENT_TYPES:
            raise ValueError(f"{url} is not a PDF or image (content-type {content_type or 'unknown'})")
        stem, suffix = name, _CONTENT_TYPES[content_type]
    path = dest_dir / (re.sub(r"[^\w.\-]+", "_", stem)[:80] + suffix)
    path.write_bytes(data)
    return path
