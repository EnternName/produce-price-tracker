"""Data-source connectors: online-shop APIs, leaflet services and plain file downloads.

None of the Spanish chains publishes an official API. These connectors use the endpoints their own
websites call, as documented by community projects; they can change without notice, and the
network sandbox this code was written in could not reach them, so check them on first use.
Be polite: they fetch slowly and identify themselves.
"""

from __future__ import annotations

import time

import httpx

USER_AGENT = "pricetracker/0.1 (personal grocery price comparison)"


def http_client(**kwargs) -> httpx.Client:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, */*", "Accept-Language": "es-ES,es;q=0.9,ca;q=0.8"}
    headers.update(kwargs.pop("headers", {}))
    return httpx.Client(headers=headers, timeout=30, follow_redirects=True, **kwargs)


def get_with_retry(client: httpx.Client, url: str, *, params=None, attempts: int = 4) -> httpx.Response:
    delay = 2.0
    for attempt in range(attempts):
        try:
            response = client.get(url, params=params)
        except httpx.TransportError:
            if attempt == attempts - 1:
                raise
        else:
            if response.status_code not in (429, 500, 502, 503, 504) or attempt == attempts - 1:
                response.raise_for_status()
                return response
        time.sleep(delay)
        delay *= 2
    raise RuntimeError("unreachable")
