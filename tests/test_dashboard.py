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
