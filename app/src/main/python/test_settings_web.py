"""settings_web: the server's admin page, over real HTTP on this machine, in
front of a real server and a real phone client."""

import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
import urllib.error
import urllib.request

import settings_net as sn
import settings_server as srv
import settings_web as sw
from test_settings_net import PIN, Phone, free_udp_port, wait_until


class SettingsWebTest(unittest.TestCase):
    def setUp(self):
        self._saved = sn.RECONNECT_S
        sn.RECONNECT_S = 0.05
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.state = os.path.join(self.d, "state")
        self.server = srv.Server(self.state, name="Team server", port=0, bind_addr="127.0.0.1",
                                 log=lambda m: None, web_port=0)
        self.server.start()
        self.base = "http://127.0.0.1:%d" % self.server.web.port
        self.token = self.server.web.token
        self.phones = []

    def tearDown(self):
        for p in self.phones:
            p.client.stop()
        self.server.stop()
        sn.RECONNECT_S = self._saved
        self._tmp.cleanup()

    def phone(self, name="Pixel 7"):
        p = Phone(self.d, name, free_udp_port(), listen=False)
        self.phones.append(p)
        p.client.start()
        p.client.add_host("127.0.0.1", self.server.net.port, self.server.host.keys.host_id)
        return p

    def req(self, path, body=None, token=True, headers=None, method=None):
        h = dict(headers or {})
        if token:
            h["X-Token"] = self.token if token is True else token
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            h.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data, headers=h, method=method)
        try:
            with urllib.request.urlopen(r, timeout=10) as resp:
                raw, code, hdrs = resp.read(), resp.status, resp.headers
        except urllib.error.HTTPError as e:
            raw, code, hdrs = e.read(), e.code, e.headers
        try:
            return code, json.loads(raw), hdrs
        except ValueError:
            return code, raw.decode(), hdrs

    def test_the_page_loads_and_its_data_needs_the_key(self):
        code, page, hdrs = self.req("/", token=False)
        self.assertEqual(code, 200)
        self.assertIn("Push to allowed phones", page)
        self.assertIn("default-src 'none'", hdrs["Content-Security-Policy"])
        for tok in (False, "wrong", self.token[:-1]):
            code, out, _ = self.req("/api/status", token=tok)
            self.assertEqual(code, 403, tok)
            self.assertNotIn("roster", out)
        code, out, _ = self.req("/api/status")
        self.assertEqual((code, out["name"]), (200, "Team server"))

    def test_the_key_file_is_owner_only_and_web_url_prints_it(self):
        path = os.path.join(self.state, sw.TOKEN_FILE)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(srv.main(["--state", self.state, "web-url"]), 0)
        self.assertEqual(out.getvalue().strip(),
                         "%s/#token=%s" % (self.base, self.token))
        self.assertIn("ssh -L", err.getvalue())

    def test_another_site_cannot_use_it(self):
        # DNS rebinding: a page from evil.example resolved to 127.0.0.1.
        code, _, _ = self.req("/api/status", headers={"Host": "evil.example:%d" % self.server.web.port})
        self.assertEqual(code, 403)
        # A cross-site form post: no custom header, not JSON.
        r = urllib.request.Request(self.base + "/api/push", data=b"settings=x",
                                   headers={"Content-Type": "application/x-www-form-urlencoded"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(r, timeout=10)
        self.assertEqual(e.exception.code, 403)
        # The browser's preflight for a header-carrying cross-site request: refused,
        # with no CORS header that would let it through.
        code, _, hdrs = self.req("/api/push", token=False, method="OPTIONS",
                                 headers={"Origin": "https://evil.example",
                                          "Access-Control-Request-Headers": "x-token"})
        self.assertEqual(code, 405)
        self.assertIsNone(hdrs.get("Access-Control-Allow-Origin"))
        self.assertIsNone(self.server.net.last_push())

    def test_allow_and_push_from_the_page_reach_the_phone(self):
        p = self.phone()
        self.assertTrue(wait_until(lambda: any(e["state"] == sn.WAITING_ADMIN
                                               for e in self.req("/api/status")[1]["roster"])))
        code, out, _ = self.req("/api/allow", {"phone": p.rx.phone_id})
        self.assertEqual(out["allowed"], p.rx.phone_id)
        code, out, _ = self.req("/api/push", {"settings": {"lora_tx_dbm": 17},
                                              "switch_in_s": None})
        self.assertEqual(out["rev"], 1)
        self.assertTrue(wait_until(lambda: p.rx.pending()))
        p.rx.set_pin(PIN)
        p.client.report(self.server.host.keys.host_id, 1,
                        p.rx.approve(self.server.host.keys.host_id, PIN))
        self.assertTrue(wait_until(lambda: self.req("/api/status")[1]["roster"][0]["state"]
                                   == "applied"))
        code, out, _ = self.req("/api/revoke", {"phone": p.rx.phone_id})
        self.assertEqual(out["revoked"], p.rx.phone_id)

    def test_a_bad_push_says_why_and_the_passphrase_is_not_kept_on_disk(self):
        code, out, _ = self.req("/api/push", {"settings": {"lora_preset": "Long Fats"}})
        self.assertIn("Unknown LoRa preset", out["error"])
        code, out, _ = self.req("/api/push", {"settings": {}, "passphrase": "team pass",
                                              "switch_in_s": 60})
        self.assertEqual(out["rev"], 1)
        for name in os.listdir(self.state):
            path = os.path.join(self.state, name)
            if os.path.isfile(path):
                with open(path, "rb") as f:
                    self.assertNotIn(b"team pass", f.read(), name)

    def test_the_card_and_odd_requests(self):
        code, out, _ = self.req("/api/card", {"addr": "settings.example.org"})
        self.assertEqual(sn.parse_card(out["card"])["addr"], "settings.example.org")
        if out["svg"] is not None:                      # qrencode installed here
            self.assertTrue(out["svg"].startswith("<svg"))
        self.assertIn("error", self.req("/api/card", {"addr": ""})[1])
        self.assertEqual(self.req("/api/nothing")[0], 404)
        code, _, _ = self.req("/api/push", body=None, headers={"Content-Type": "application/json",
                                                              "Content-Length": "999999"},
                              method="POST")
        self.assertIn(code, (400, 413))

    def test_without_web_port_there_is_no_page(self):
        out = srv.call(self.state, {"cmd": "status"})
        self.assertNotIn("error", out)
        other = srv.Server(os.path.join(self.d, "s2"), port=0, bind_addr="127.0.0.1",
                           log=lambda m: None)
        other.start()
        try:
            self.assertIn("admin page is off",
                          srv.call(os.path.join(self.d, "s2"), {"cmd": "web"})["error"])
        finally:
            other.stop()


if __name__ == "__main__":
    unittest.main()
