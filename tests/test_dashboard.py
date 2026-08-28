import base64
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from polytrade_esports.dashboard import DashboardServer
from polytrade_esports.storage import Database
from polytrade_esports.types import Match

PASSWORD = "correct horse"


class StylesheetTests(unittest.TestCase):
    """Guards for layout rules that are easy to delete and hard to notice.

    Team and tournament names come from a third party and can be long. Grid and
    flex children default to min-width:auto, which refuses to shrink below the
    content width, so without an explicit min-width:0 those names push the card
    past the viewport on a phone. It looks fine on a desktop, which is exactly
    why it needs a test.
    """

    @classmethod
    def setUpClass(cls):
        from polytrade_esports.dashboard import WEB_ROOT

        cls.web = WEB_ROOT
        cls.css = (WEB_ROOT / "app.css").read_text()

    def test_text_bearing_containers_can_shrink(self):
        for selector in (".meta > *", ".board > *", ".subline > *"):
            self.assertIn(
                selector,
                self.css,
                "%s needs an explicit min-width:0 or long names overflow" % selector,
            )
        self.assertIn("min-width: 0", self.css)

    def test_flexible_track_uses_minmax_not_bare_fr(self):
        # `1fr` is shorthand for minmax(auto, 1fr); the auto minimum is the
        # overflow. The board must use minmax(0, 1fr).
        self.assertIn("grid-template-columns: minmax(0, 1fr) auto minmax(0, 1fr)", self.css)

    def test_long_names_are_truncated_rather_than_wrapped(self):
        self.assertIn("text-overflow: ellipsis", self.css)

    def test_a_phone_breakpoint_exists_for_the_card_grid(self):
        self.assertIn("@media (max-width: 620px)", self.css)
        self.assertIn("grid-template-columns: 1fr;", self.css)

    def test_the_board_filter_is_styled_and_reachable(self):
        # The default hides most of the slate, so the control that reverses it
        # has to be visible rather than a hidden keyboard trick.
        self.assertIn(".toggle", self.css)
        self.assertIn('aria-pressed="true"', (self.web / "index.html").read_text())
        self.assertIn('id="filter-toggle"', (self.web / "index.html").read_text())

    def test_numeric_headers_align_with_their_values(self):
        # A left-aligned header over a right-aligned column reads as a whole
        # column of offset: SHARES looked empty and its values looked like
        # prices.
        self.assertIn("th.num, td.num { text-align: right; }", self.css)

    def test_the_observation_log_starts_collapsed(self):
        # <details> without the open attribute; the audit trail stays
        # reachable without opening the page on a hundred rows of ticks.
        html = (self.web / "detail.html").read_text()
        self.assertIn('<details class="fold">', html)
        self.assertNotIn('<details class="fold" open', html)
        self.assertIn('id="d-obs"', html)

    def test_motion_can_be_turned_off(self):
        self.assertIn("prefers-reduced-motion", self.css)


class DashboardAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        database = Database(str(Path(cls.temp.name) / "d.sqlite3"))
        database.initialize()
        database.add_match(Match("m1", "NAVI", "M80", 3, 0.6))
        cls.server = DashboardServer(
            ("127.0.0.1", 0),
            database=database,
            account_name="live-paper",
            auth_username="viewer",
            password_sha256=hashlib.sha256(PASSWORD.encode("utf-8")).hexdigest(),
        )
        cls.base = "http://127.0.0.1:%d" % cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.temp.cleanup()

    def _get(self, path, user=None, password=None):
        request = urllib.request.Request(self.base + path)
        if user is not None:
            token = base64.b64encode(("%s:%s" % (user, password)).encode()).decode()
            request.add_header("Authorization", "Basic " + token)
        return urllib.request.urlopen(request, timeout=5)

    def test_status_requires_credentials(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/status")
        self.assertEqual(caught.exception.code, 401)
        self.assertIn("Basic", caught.exception.headers.get("WWW-Authenticate", ""))

    def test_wrong_password_is_rejected(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/status", "viewer", "wrong")
        self.assertEqual(caught.exception.code, 401)

    def test_correct_credentials_return_the_payload(self):
        response = self._get("/api/status", "viewer", PASSWORD)
        payload = json.loads(response.read().decode())
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["counts"]["matches"], 1)
        self.assertIn("collector", payload)

    def test_healthz_stays_open_for_container_probes(self):
        self.assertEqual(self._get("/healthz").status, 200)

    def test_index_and_assets_are_served_with_a_strict_policy(self):
        for path, expected in (
            ("/", "text/html"),
            ("/app.css", "text/css"),
            ("/app.js", "application/javascript"),
        ):
            response = self._get(path, "viewer", PASSWORD)
            self.assertEqual(response.status, 200)
            self.assertIn(expected, response.headers["Content-Type"])
            self.assertIn("frame-ancestors 'none'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    def test_match_page_is_served_for_any_slug(self):
        response = self._get("/match/m1", "viewer", PASSWORD)
        self.assertEqual(response.status, 200)
        self.assertIn("text/html", response.headers["Content-Type"])

    def test_match_api_returns_the_detail_payload(self):
        response = self._get("/api/match?id=m1", "viewer", PASSWORD)
        payload = json.loads(response.read().decode())
        self.assertEqual(payload["match_id"], "m1")
        self.assertEqual(payload["team_a"], "NAVI")
        self.assertIn("history", payload)
        self.assertIn("positions", payload)

    def test_unknown_match_is_a_404_not_an_empty_page(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/match?id=does-not-exist", "viewer", PASSWORD)
        self.assertEqual(caught.exception.code, 404)

    def test_match_api_rejects_ids_that_are_not_slugs(self):
        for bad in ("../../etc/passwd", "a%20b", "'; DROP TABLE matches;--", "", "a" * 200):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._get("/api/match?id=" + urllib.parse.quote(bad, safe=""), "viewer", PASSWORD)
            self.assertIn(caught.exception.code, (400, 404), "accepted: %r" % bad)

    def test_match_api_requires_credentials(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self._get("/api/match?id=m1")
        self.assertEqual(caught.exception.code, 401)

    def test_detail_assets_are_served(self):
        response = self._get("/detail.js", "viewer", PASSWORD)
        self.assertEqual(response.status, 200)
        self.assertIn("application/javascript", response.headers["Content-Type"])

    def test_path_traversal_is_refused(self):
        for path in ("/..%2f..%2fetc%2fpasswd", "/../../storage.py", "/web/../storage.py"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self._get(path, "viewer", PASSWORD)
            self.assertEqual(caught.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
