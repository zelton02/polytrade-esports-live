"""Read-only live dashboard.

Serves the match board, the model-vs-market comparison, and the paper ledger.
Auth and security headers mirror the sibling polymarket-research dashboard so
both boards behave the same behind the same tunnel setup.
"""

import base64
import hashlib
import hmac
import json
import os
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from .storage import Database

WEB_ROOT = Path(__file__).with_name("web")
DEPLOYMENT_ASSETS = ("index.html", "app.js", "app.css", "detail.html", "detail.js")
# Match ids are Polymarket event slugs. Anything outside this shape is not a
# slug we ever issued, so it is refused before it reaches a query.
SLUG_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}$")
USERNAME_ENV = "POLYTRADE_DASHBOARD_USERNAME"
PASSWORD_ENV = "POLYTRADE_DASHBOARD_PASSWORD_SHA256"
STATIC_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def deployment_asset_manifest() -> Dict[str, str]:
    """Hashes used to prove the public tunnel reached the current container."""
    return {
        name: hashlib.sha256((WEB_ROOT / name).read_bytes()).hexdigest()
        for name in DEPLOYMENT_ASSETS
    }


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        database: Database,
        account_name: str,
        auth_username: str = "",
        password_sha256: str = "",
    ) -> None:
        self.database = database
        self.account_name = account_name
        self.auth_username = auth_username
        self.password_sha256 = password_sha256
        super().__init__(address, DashboardHandler)


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "polytrade-esports-live"
    sys_version = ""

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int,
        extra: Optional[Dict[str, str]] = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()

    def _authorized(self) -> bool:
        username = self.server.auth_username  # type: ignore[attr-defined]
        password_sha256 = self.server.password_sha256  # type: ignore[attr-defined]
        if not username and not password_sha256:
            return True
        header = self.headers.get("Authorization", "")
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            supplied_username, supplied_password = decoded.split(":", 1)
        except (ValueError, UnicodeDecodeError):
            return False
        supplied_hash = hashlib.sha256(supplied_password.encode("utf-8")).hexdigest()
        return hmac.compare_digest(supplied_username, username) and hmac.compare_digest(
            supplied_hash, password_sha256
        )

    def _unauthorized(self) -> None:
        body = b"Authentication required."
        self._headers(
            HTTPStatus.UNAUTHORIZED,
            "text/plain; charset=utf-8",
            len(body),
            {"WWW-Authenticate": 'Basic realm="Polytrade Esports Live", charset="UTF-8"'},
        )
        self.wfile.write(body)

    def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def _static(self, name: str) -> None:
        target = (WEB_ROOT / name).resolve()
        try:
            target.relative_to(WEB_ROOT.resolve())
        except ValueError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not target.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        body = target.read_bytes()
        content_type = STATIC_TYPES.get(target.suffix, "application/octet-stream")
        self._headers(HTTPStatus.OK, content_type, len(body))
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/healthz":
            body = b"ok\n"
            self._headers(HTTPStatus.OK, "text/plain; charset=utf-8", len(body))
            self.wfile.write(body)
            return
        if path == "/healthz/assets":
            # The dashboard is intentionally password protected, so CI cannot
            # download the five assets themselves. Publishing only their
            # hashes gives the deployment an end-to-end freshness proof
            # through Cloudflare without weakening authentication.
            self._json({"status": "ok", "assets": deployment_asset_manifest()})
            return
        if not self._authorized():
            self._unauthorized()
            return
        database: Database = self.server.database  # type: ignore[attr-defined]
        account: str = self.server.account_name  # type: ignore[attr-defined]

        if path in ("/", "/index.html"):
            self._static("index.html")
            return
        if path in ("/app.js", "/app.css", "/detail.js"):
            self._static(path.lstrip("/"))
            return
        # One page per match. The slug lives in the URL so a view is linkable;
        # the page fetches its own data from the API below.
        if path.startswith("/match/"):
            self._static("detail.html")
            return
        if path == "/api/status":
            self._json(database.dashboard_payload(account))
            return
        if path == "/api/match":
            match_id = (parse_qs(parsed.query).get("id") or [""])[0]
            if not SLUG_PATTERN.match(match_id):
                self._json({"error": "invalid match id"}, HTTPStatus.BAD_REQUEST)
                return
            detail = database.match_detail(match_id, account_name=account)
            if detail is None:
                self._json({"error": "unknown match"}, HTTPStatus.NOT_FOUND)
                return
            self._json(detail)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, format: str, *args: object) -> None:
        return


def serve(
    database: Database,
    host: str = "127.0.0.1",
    port: int = 8788,
    account_name: str = "live-paper",
) -> None:
    database.initialize()
    auth_username = os.environ.get(USERNAME_ENV, "").strip()
    password_sha256 = os.environ.get(PASSWORD_ENV, "").strip()
    server = DashboardServer(
        (host, int(port)),
        database=database,
        account_name=account_name,
        auth_username=auth_username,
        password_sha256=password_sha256,
    )
    state = "password protected" if auth_username else "local access only"
    print(
        "Polytrade Esports Live dashboard (%s): http://%s:%d" % (state, host, port),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
