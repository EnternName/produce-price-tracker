"""Protection for an app that listens on localhost.

Any website open in the same browser can send requests to http://127.0.0.1:8000. Without checks, a
malicious page could submit the app's forms (e.g. "download this flyer", which spends Claude credits)
or call its API. Two layers stop that:

* Form posts (the web UI) must come from the app's own pages: the browser's Origin header (or Referer)
  has to match the app's host. Requests without either header, such as curl or tests, are allowed.
* The JSON API under /api/ (used by the browser add-on) needs a secret token in the
  ``X-Pricetracker-Token`` header. CORS is only granted to browser-extension origins, and the custom
  header forces a preflight, so ordinary web pages cannot send it.

Both rely on the browser's notion of "same origin", which a DNS-rebinding attack subverts (a malicious
domain that resolves to 127.0.0.1). So requests must also be addressed to an allowed host name:
localhost, 127.0.0.1 or ::1, plus any listed in $PRICETRACKER_ALLOWED_HOSTS (comma-separated).
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
from pathlib import Path
from urllib.parse import urlsplit

TOKEN_HEADER = "x-pricetracker-token"
EXTENSION_SCHEMES = ("moz-extension://", "chrome-extension://")
_UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def load_token(directory: Path) -> str:
    """The API token: $PRICETRACKER_TOKEN, else data/api_token (created on first use)."""
    if env := os.environ.get("PRICETRACKER_TOKEN"):
        return env
    path = directory / "api_token"
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    directory.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token


LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


def allowed_hosts() -> frozenset[str]:
    extra = {h.strip().lower() for h in os.environ.get("PRICETRACKER_ALLOWED_HOSTS", "").split(",") if h.strip()}
    return LOCAL_HOSTS | extra


def host_name(host_header: str) -> str:
    """'127.0.0.1:8000' -> '127.0.0.1', '[::1]:8000' -> '::1'."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host[1:].split("]")[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def _origin_of(url: str) -> str | None:
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


def is_extension_origin(origin: str | None) -> bool:
    return bool(origin) and origin.lower().startswith(EXTENSION_SCHEMES)


class LocalAppGuard:
    """ASGI middleware enforcing the two rules above."""

    def __init__(self, app, token_provider, hosts_provider=allowed_hosts):
        self.app = app
        self.token_provider = token_provider  # callable -> current token
        self.hosts_provider = hosts_provider  # callable -> allowed Host names

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
        method = scope["method"].upper()
        path = scope["path"]
        origin = headers.get("origin")

        if host_name(headers.get("host", "")) not in self.hosts_provider():
            return await _respond(send, 421, {"detail": "Unknown host name (set PRICETRACKER_ALLOWED_HOSTS to allow it)"})
        if path.startswith("/api/"):
            return await self._api(scope, receive, send, method, headers, origin)

        if method in _UNSAFE_METHODS and not self._same_origin(scope, headers):
            return await _respond(send, 403, {"detail": "Cross-site request blocked"})
        return await self.app(scope, receive, send)

    async def _api(self, scope, receive, send, method, headers, origin):
        cors = _cors_headers(origin) if is_extension_origin(origin) else []
        if origin and not is_extension_origin(origin) and not self._same_origin(scope, headers):
            return await _respond(send, 403, {"detail": "This API only accepts requests from the browser add-on"})
        if method == "OPTIONS":
            return await _respond(send, 204, None, cors)
        token = headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(token.encode(), self.token_provider().encode()):
            return await _respond(send, 401, {"detail": "Missing or wrong token (see the app's Browser add-on page)"}, cors)

        async def send_with_cors(message):
            if message["type"] == "http.response.start" and cors:
                message = {**message, "headers": [*message.get("headers", []), *cors]}
            await send(message)

        return await self.app(scope, receive, send_with_cors)

    @staticmethod
    def _same_origin(scope, headers) -> bool:
        claimed = headers.get("origin") or (headers.get("referer") and _origin_of(headers["referer"]))
        if claimed is None:
            return True  # not a browser-initiated cross-site request (curl, tests, old clients)
        if claimed == "null":
            return False
        host = headers.get("host", "")
        scheme = scope.get("scheme", "http")
        return claimed.lower() == f"{scheme}://{host}".lower()


def _cors_headers(origin: str) -> list[tuple[bytes, bytes]]:
    return [
        (b"access-control-allow-origin", origin.encode("latin-1")),
        (b"access-control-allow-methods", b"GET, POST, DELETE, OPTIONS"),
        (b"access-control-allow-headers", b"content-type, x-pricetracker-token"),
        (b"access-control-max-age", b"600"),
        (b"vary", b"origin"),
    ]


async def _respond(send, status: int, body: dict | None, extra_headers=()) -> None:
    payload = b"" if body is None else json.dumps(body).encode()
    headers = [*extra_headers]
    if body is not None:
        headers.append((b"content-type", b"application/json"))
    headers.append((b"content-length", str(len(payload)).encode()))
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": payload})
