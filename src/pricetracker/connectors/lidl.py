"""Lidl Spain weekly leaflets ("folletos").

Lidl's leaflet viewer is served by the Schwarz group leaflet service, which lists the current leaflets
for a locale together with a PDF download link:

    GET https://endpoints.leaflets.schwarz/v4/overview?client_locale=lidl/es-ES

The response layout is not documented, so this walks the JSON for anything that carries a PDF URL.
The PDFs then go through the normal flyer pipeline (Claude reads each page).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import get_with_retry, http_client

OVERVIEW_URL = "https://endpoints.leaflets.schwarz/v4/overview"
LOCALE = "lidl/es-ES"

_PDF_KEY = re.compile(r"pdf", re.I)
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


@dataclass(frozen=True)
class Leaflet:
    title: str
    pdf_url: str
    valid_from: str | None = None
    valid_to: str | None = None
    identifier: str | None = None


def _date(value) -> str | None:
    if isinstance(value, str) and (m := _DATE.search(value)):
        return m.group(0)
    return None


def _first(d: dict, *keys):
    for key in keys:
        if d.get(key):
            return d[key]
    return None


def find_leaflets(data) -> list[Leaflet]:
    """Every object in `data` that has a PDF link, de-duplicated by URL."""
    found: dict[str, Leaflet] = {}

    def walk(node):
        if isinstance(node, dict):
            pdf = next(
                (v for k, v in node.items() if _PDF_KEY.search(k) and isinstance(v, str) and v.startswith("http")),
                None,
            )
            if pdf and pdf not in found:
                found[pdf] = Leaflet(
                    title=str(_first(node, "title", "name", "fullTitle", "slug") or "Folleto Lidl"),
                    pdf_url=pdf,
                    valid_from=_date(_first(node, "startDate", "offerStartDate", "validFrom", "start")),
                    valid_to=_date(_first(node, "endDate", "offerEndDate", "validTo", "end")),
                    identifier=_first(node, "flyer_identifier", "identifier", "slug", "id"),
                )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(data)
    return list(found.values())


def list_leaflets(locale: str = LOCALE, *, client=None) -> list[Leaflet]:
    own_client = client is None
    client = client or http_client()
    try:
        data = get_with_retry(client, OVERVIEW_URL, params={"client_locale": locale}).json()
    finally:
        if own_client:
            client.close()
    return find_leaflets(data)
