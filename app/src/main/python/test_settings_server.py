"""settings_server: the host as a Linux service, driven through its real
command line and admin socket, with a real phone client connecting to it."""

import contextlib
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import settings_net as sn
import settings_server as srv
import settings_share as ss
from test_settings_net import PIN, Phone, free_udp_port, wait_until

HERE = os.path.dirname(os.path.abspath(__file__))


class SettingsServerTest(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(sn, n) for n in ("RECONNECT_S",)}
        sn.RECONNECT_S = 0.05
        self._tmp = tempfile.TemporaryDirectory()
        self.d = self._tmp.name
        self.state = os.path.join(self.d, "state")
        self.server = None
        self.phones = []

    def tearDown(self):
        for p in self.phones:
            p.client.stop()
        if self.server:
            self.server.stop()
        for n, v in self._saved.items():
            setattr(sn, n, v)
        # stop() does not join the per-connection threads, and one of them can
        # still be saving into state/ for a moment after it returns -- harmless
        # in use, but a race with deleting the directory. Seen on a CI runner,
        # never locally. Retry briefly rather than fail the test on cleanup.
        for _ in range(40):
            try:
                self._tmp.cleanup()
                break
            except OSError:
                time.sleep(0.05)
        else:
            self._tmp.cleanup()

    def start(self, **kw):
        kw.setdefault("port", 0)
        kw.setdefault("bind_addr", "127.0.0.1")
        self.logged = []
        self.server = srv.Server(self.state, name="Team server", log=self.logged.append, **kw)
        self.server.start()
        return self.server

    def phone(self, name="Pixel 7"):
        p = Phone(self.d, name, free_udp_port(), listen=False)
        self.phones.append(p)
        p.client.start()
        p.client.add_host("127.0.0.1", self.server.net.port, self.server.host.keys.host_id)
        return p

    def cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = srv.main(["--state", self.state] + list(args))
        return code, out.getvalue(), err.getvalue()

    def entry(self, p):
        return next((e for e in self.server.net.roster() if e["phone"] == p.rx.phone_id), None)

    def test_allow_then_push_from_the_command_line_reaches_the_phone(self):
        self.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: (self.entry(p) or {}).get("state")
                                   == sn.WAITING_ADMIN))
        code, out, _ = self.cli("status")
        self.assertEqual(code, 0)
        self.assertIn(p.rx.phone_id, out)
        self.assertIn("waiting for the admin", out)
        code, out, err = self.cli("allow", p.rx.phone_id[:6])     # typed off the phone
        self.assertEqual(code, 0, err)
        code, out, err = self.cli("push", "--lora-preset", "Medium Slow",
                                  "--lora-freq-mhz", "906.875", "--switch-in-min", "2")
        self.assertEqual((code, out.strip()), (0, "pushed revision 1"), err)
        self.assertTrue(wait_until(lambda: (self.entry(p) or {}).get("state") == "pending"))
        offer = p.rx.pending()[0]
        self.assertEqual((offer["settings"]["lora_preset"], offer["settings"]["lora_freq_mhz"]),
                         ("Medium Slow", "906.875"))
        self.assertAlmostEqual(offer["switch_at"], time.time() + 120, delta=5)
        p.rx.set_pin(PIN)
        p.client.report(self.server.host.keys.host_id, 1,
                        p.rx.approve(self.server.host.keys.host_id, PIN))
        self.assertTrue(wait_until(lambda: self.entry(p)["state"] == "scheduled"))
        self.assertIn("scheduled (rev 1)", self.cli("status")[1])

    def test_the_passphrase_is_asked_for_twice_and_sealed_to_the_phone(self):
        self.start(open_=True)
        p = self.phone()
        p.rx.set_pin(PIN)
        p.rx.set_accept_passphrase_over_network(True, PIN)
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        with mock.patch("getpass.getpass", side_effect=["team pass", "typo"]):
            code, _, err = self.cli("push", "--passphrase", "--now")
        self.assertEqual(code, 1)
        self.assertIn("did not match", err)
        with mock.patch("getpass.getpass", side_effect=["team pass", "team pass"]):
            code, _, err = self.cli("push", "--passphrase", "--now")
        self.assertEqual(code, 0, err)
        self.assertTrue(wait_until(lambda: self.entry(p)["state"] == "pending"))
        self.assertEqual(p.rx.pending()[0]["settings"]["passphrase"], "team pass")
        self.assertIsNone(p.rx.pending()[0]["switch_at"])
        with open(os.path.join(self.state, "server.json")) as f:
            self.assertNotIn("team pass", f.read())
        self.assertIn("admin: pushed revision 1: - + passphrase, switch on accept", self.logged)
        self.assertNotIn("team pass", "\n".join(self.logged))

    def test_a_push_with_a_mistake_is_refused_before_any_phone_sees_it(self):
        self.start(open_=True)
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        for args, says in ((["--lora-preset", "Long Fats"], "Unknown LoRa preset"),
                           (["--lora-freq-mhz", "2400"], "between"),
                           (["--lora-tx-dbm", "60"], "between"),
                           (["--lora-preset", "Long Moderate", "--lora-freq-mhz", "902.05"],
                            "does not fit"),
                           (["--fm-w", "5000"], "Coverage"),
                           ([], "Nothing to push")):
            code, _, err = self.cli("push", *args)
            self.assertEqual(code, 1, args)
            self.assertIn(says, err, args)
        time.sleep(0.2)
        self.assertEqual((p.bundles, self.server.net.last_push()), ([], None))

    def test_a_restart_keeps_the_key_the_revisions_the_allow_list_and_the_roster(self):
        self.start()
        p = self.phone()
        self.assertTrue(wait_until(lambda: self.entry(p) is not None))
        self.assertEqual(self.cli("allow", p.rx.phone_id)[0], 0)
        self.assertEqual(self.cli("push", "--lora-tx-dbm", "17", "--now")[0], 0)
        self.assertTrue(wait_until(lambda: self.entry(p)["state"] == "pending"))
        host_id = self.server.host.keys.host_id
        p.client.stop()
        self.server.stop()
        self.server = None
        self.start()
        self.assertEqual(self.server.host.keys.host_id, host_id)
        e = self.entry(p)
        self.assertEqual((e["state"], e["rev"], e["connected"]), ("pending", 1, False))
        self.assertIn(p.rx.phone_id, self.server.allow)
        self.assertEqual(self.cli("push", "--lora-tx-dbm", "18", "--now")[1].strip(),
                         "pushed revision 2")

    def test_state_is_owner_only(self):
        self.start()
        for path, mode in ((self.state, 0o700),
                           (os.path.join(self.state, "host_key.json"), 0o600),
                           (os.path.join(self.state, "control.sock"), 0o600)):
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), mode, path)

    def test_allow_and_revoke_by_id(self):
        self.start()
        a, b = self.phone("A"), self.phone("B")
        self.assertTrue(wait_until(lambda: self.entry(a) and self.entry(b)))
        code, _, err = self.cli("allow", "")
        self.assertEqual(code, 1)
        code, _, err = self.cli("allow", "zz")
        self.assertIn("No phone", err)
        ahead = "0123456789abcdef"                      # a phone not seen yet
        self.assertEqual(self.cli("allow", ahead)[0], 0)
        self.assertIn(ahead, self.server.allow)
        self.assertEqual(self.cli("allow", a.rx.phone_id)[0], 0)
        self.assertTrue(wait_until(lambda: self.entry(a)["state"] is None))
        self.assertEqual(self.cli("revoke", a.rx.phone_id)[0], 0)
        self.assertIn("admin: revoked %s" % a.rx.phone_id, self.logged)
        self.assertIn("admin: allowed %s (A)" % a.rx.phone_id, self.logged)
        self.assertNotIn(a.rx.phone_id, self.server.allow)
        self.assertTrue(wait_until(lambda: not a.client.hosts()[0]["connected"]))

    def test_the_card_names_this_server(self):
        self.start()
        code, out, _ = self.cli("card", "--addr", "rtak.example.org")
        self.assertEqual(code, 0)
        c = sn.parse_card(out.splitlines()[0])
        self.assertEqual((c["host"], c["addr"], c["port"], c["name"]),
                         (self.server.host.keys.host_id, "rtak.example.org",
                          self.server.net.port, "Team server"))

    def test_a_state_directory_too_deep_for_a_socket_path_still_works(self):
        self.state = os.path.join(self.d, "a" * 60, "b" * 60, "state")
        self.assertGreater(len(os.path.join(self.state, "control.sock")), 108)
        self.start()
        code, out, err = self.cli("status")
        self.assertEqual(code, 0, err)
        self.assertIn("Team server", out)

    def test_no_server_running_is_said_plainly(self):
        code, _, err = self.cli("status")
        self.assertEqual(code, 1)
        self.assertIn("No server running", err)

    def test_kotlins_copy_of_the_server_code_prefix_matches(self):
        # The scanner hands Kotlin a string; Kotlin decides from this prefix
        # whether it is a server to add or settings to approve. A drift is
        # silent: a server's code would be refused as damaged settings.
        kt = os.path.join(HERE, "..", "java", "io", "github", "stress5094",
                          "customrnsbridge", "SettingsShareUi.kt")
        with open(kt) as f:
            self.assertIn('SERVER_CODE_PREFIX = "%s"' % sn.CARD_PREFIX, f.read())

    def test_it_runs_from_only_the_files_a_server_copy_holds(self):
        app = os.path.join(self.d, "app")
        os.mkdir(app)
        for f in srv.SERVER_FILES:
            shutil.copy(os.path.join(HERE, f), app)
        state = os.path.join(self.d, "s2")
        proc = subprocess.Popen([sys.executable, "settings_server.py", "--state", state,
                                 "serve", "--port", "0", "--bind", "127.0.0.1"],
                                cwd=app, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            self.assertTrue(wait_until(lambda: os.path.exists(
                os.path.join(state, "control.sock")), timeout=20))
            st = srv.call(state, {"cmd": "status"})
            self.assertEqual(st["roster"], [])
            self.assertEqual(srv.call(state, {"cmd": "push", "settings":
                                              {"coverage": {"fm_w": "5"}},
                                              "switch_in_s": None})["rev"], 1)
        finally:
            proc.send_signal(signal.SIGTERM)
            out = proc.communicate(timeout=20)[0].decode()
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("listening on port", out)


if __name__ == "__main__":
    unittest.main()
