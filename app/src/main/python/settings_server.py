"""
The settings host as a server (the settings-sharing notes, step 4): the same
host code a phone runs (settings_net.HostServer, settings_share.Host), as a
Linux service with an admin command line.

    python3 settings_server.py --state DIR serve [--name N] [--port 47812]
                                [--bind ADDR] [--lan] [--open] [--ping-s 60]
    python3 settings_server.py --state DIR status
    python3 settings_server.py --state DIR push [--lora-preset P]
                                [--lora-freq-mhz F] [--lora-tx-dbm N]
                                [--fm-mhz F] [--fm-w W] [--tx-height-m H]
                                [--passphrase] [--switch-in-min M | --now]
    python3 settings_server.py --state DIR allow PHONE_ID
    python3 settings_server.py --state DIR revoke PHONE_ID
    python3 settings_server.py --state DIR card --addr ADDR [--port P]
    python3 settings_server.py --state DIR web-url   (with serve --web-port 8080)

The admin commands talk to the running server over a Unix socket in the state
directory (owner-only), so administering it needs a login on this machine --
there is no admin port on the network to secure.

WHAT IT KEEPS (in --state, mode 0700):
  host_key.json  the server's signing key. This file IS the server: lose it
                 and every phone has to add (and trust) it again. Back it up.
  host_rev.json  the last revision; phones refuse one they already decided.
  server.json    the name, the allow list, the roster and the last push --
                 WITHOUT the passphrase, which is held in memory only. After a
                 restart, phones that still lack a push that carried it are
                 marked "push again" rather than sent it without.

WHO GETS A PUSH: only phones on the allow list (`allow`), unless --open. A
phone that connects and is not allowed shows on the roster as "waiting for the
admin" with its id; the phone shows the same id on its own screen, which is how
the admin knows it is the right phone. Anyone who can reach the port can wait
there, so --open is for a server only reachable on the team's own network.

--lan also sends the WiFi beacon, so phones on the same network find the
server with nothing typed, exactly as they find a host phone. Without it,
phones add the server from its code (`card`), scanned or typed.
"""

import argparse
import contextlib
import getpass
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import settings_net as sn
import settings_share as ss

SERVER_PING_S = 60
# What a server copy holds: this file and what it imports (server/package.sh,
# and a test runs the server from these alone).
SERVER_FILES = ("settings_server.py", "settings_net.py", "settings_share.py", "settings_web.py",
                "radio.py", "coverage.py", "overture_tiles.py")


# -- checking a push before it reaches every phone -----------------------------

def check_settings(settings):
    """None, or what is wrong with `settings` in the operator's words. The
    phones check again on apply; this catches a typo before it is pushed to
    everyone at once."""
    import radio
    unknown = set(settings) - set(ss.SETTING_KEYS)
    if unknown:
        return "Unknown settings: %s" % ", ".join(sorted(unknown))
    preset = settings.get("lora_preset")
    if preset is not None and preset not in radio.PRESETS:
        return "Unknown LoRa preset %r (one of: %s)" % (preset, ", ".join(radio.PRESETS))
    if "lora_freq_mhz" in settings:
        try:
            freq_hz = int(round(float(settings["lora_freq_mhz"]) * 1e6))
        except (TypeError, ValueError):
            return "The LoRa frequency must be a number of MHz"
        if preset is not None:
            problem = radio.validate(preset, freq_hz, settings.get(
                "lora_tx_dbm", radio.TXPOWER_MIN_DBM))
            if problem:
                return problem
        elif not radio.FREQ_MIN_HZ <= freq_hz <= radio.FREQ_MAX_HZ:
            return ("The LoRa frequency must be between %.0f and %.0f MHz"
                    % (radio.FREQ_MIN_HZ / 1e6, radio.FREQ_MAX_HZ / 1e6))
    if "lora_tx_dbm" in settings:
        try:
            dbm = int(settings["lora_tx_dbm"])
        except (TypeError, ValueError):
            return "The LoRa power must be a whole number of dBm"
        if not radio.TXPOWER_MIN_DBM <= dbm <= radio.TXPOWER_MAX_DBM:
            return ("The LoRa power must be between %d and %d dBm"
                    % (radio.TXPOWER_MIN_DBM, radio.TXPOWER_MAX_DBM))
    if "coverage" in settings:
        import coverage
        problem = coverage.check_settings(json.dumps(settings["coverage"]))
        if problem:
            return "Coverage: " + problem
    return None


# -- the running server --------------------------------------------------------

class Server(object):
    """A HostServer with its state on disk and an admin socket."""

    def __init__(self, state_dir, name=None, port=sn.HOST_PORT, bind_addr="",
                 lan=False, open_=False, ping_s=SERVER_PING_S, log=None, web_port=None):
        self.dir = state_dir
        os.makedirs(state_dir, mode=0o700, exist_ok=True)
        os.chmod(state_dir, 0o700)
        self._log = log or _log
        self._save_lock = threading.Lock()
        self._path = os.path.join(state_dir, "server.json")
        saved = {}
        if os.path.exists(self._path):
            with open(self._path) as f:
                saved = json.load(f)
        self.name = name or saved.get("name") or socket.gethostname()
        self.allow = dict(saved.get("allow") or {})        # phone id -> name
        self.open = open_
        keys = ss.HostKeys.load_or_create(os.path.join(state_dir, "host_key.json"))
        os.chmod(os.path.join(state_dir, "host_key.json"), 0o600)
        self.host = ss.Host(keys, self.name, os.path.join(state_dir, "host_rev.json"))
        self.net = sn.HostServer(self.host, port=port, bind_addr=bind_addr, beacon=lan,
                                 admit=None if open_ else (lambda pid: pid in self.allow),
                                 ping_s=ping_s, on_change=self.save, log=self._log)
        self.net.restore(saved.get("snapshot"))
        self._control = None
        self._running = False
        self.web = None
        if web_port is not None:
            import settings_web
            self.web = settings_web.WebAdmin(state_dir, port=web_port, log=self._log)

    def save(self):
        """After every change (called from the phones' threads too). A failed
        write is logged, not raised: the roster in memory is still right, and
        the next change writes it again."""
        with self._save_lock:
            try:
                ss._write_json(self._path, {"name": self.name, "allow": self.allow,
                                            "snapshot": self.net.snapshot()})
            except OSError as e:
                self._log("could not save %s: %s" % (self._path, e))

    @property
    def control_path(self):
        return os.path.join(self.dir, "control.sock")

    def start(self):
        # The admin socket first: a failure there must not leave the phones'
        # port open with nobody able to administer it.
        if os.path.exists(self.control_path):
            os.unlink(self.control_path)
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            with _socket_path(self.control_path) as path:
                s.bind(path)
        finally:
            os.umask(old)
        # Listen as soon as the socket file exists: the file is how the command
        # line knows a server is running, and between bind() and listen() a
        # connection is refused -- "No server running" from a server that is.
        # Seen on a CI runner. Early callers wait in the backlog until the
        # admin thread below starts accepting.
        s.listen(4)
        s.settimeout(1.0)
        if self.web is not None:
            self.web.start()       # before the phones' port too: same reason
        self.net.start()
        self._control = s
        self._running = True
        threading.Thread(target=self._control_loop, name="settings-admin",
                         daemon=True).start()
        self.save()
        self._log("server %s (host %s) on port %d; %s"
                  % (self.name, self.host.keys.host_id, self.net.port,
                     "open to every phone" if self.open else
                     "allow list of %d phone(s)" % len(self.allow)))

    def stop(self):
        self._running = False
        if self.web is not None:
            self.web.stop()
        self.net.stop()
        if self._control:
            self._control.close()
            try:
                os.unlink(self.control_path)
            except OSError:
                pass
        self.save()

    def _control_loop(self):
        while self._running:
            try:
                c, _ = self._control.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._control_one, args=(c,), daemon=True).start()

    def _control_one(self, c):
        try:
            c.settimeout(10.0)
            buf = b""
            while b"\n" not in buf and len(buf) < sn.MAX_LINE:
                chunk = c.recv(4096)
                if not chunk:
                    break
                buf += chunk
            try:
                out = self.command(json.loads(buf.decode("utf-8")))
            except ss.Refused as e:
                out = {"error": str(e)}
            except Exception as e:  # noqa: BLE001 -- said to the admin, not fatal
                out = {"error": "%s: %s" % (type(e).__name__, e)}
            c.sendall(json.dumps(out).encode("utf-8") + b"\n")
        except OSError:
            pass
        finally:
            c.close()

    def _phone(self, prefix):
        """A phone id from the roster or the allow list by a unique prefix,
        as the admin types it off a phone's screen (spaces ignored)."""
        prefix = (prefix or "").replace(" ", "").lower()
        known = set(self.allow) | {p["phone"] for p in self.net.roster()}
        hits = sorted(p for p in known if p.startswith(prefix)) if prefix else []
        if len(hits) == 1:
            return hits[0]
        if len(prefix) == 16 and all(ch in "0123456789abcdef" for ch in prefix):
            return prefix          # a phone not seen yet: allowed ahead of time
        if not hits:
            raise ss.Refused("No phone %r on the roster (a phone not seen yet needs its "
                             "whole 16-character id)" % prefix)
        raise ss.Refused("%r matches %d phones: type more of the id" % (prefix, len(hits)))

    def command(self, req):
        cmd = req.get("cmd")
        if cmd == "status":
            return {"name": self.name, "host": self.host.keys.host_id,
                    "port": self.net.port, "open": self.open,
                    "allow": self.allow, "roster": self.net.roster(),
                    "last_push": self.net.last_push()}
        if cmd == "push":
            settings = req.get("settings") or {}
            problem = check_settings(settings)
            if problem:
                raise ss.Refused(problem)
            rev = self.net.push(settings, passphrase=req.get("passphrase") or None,
                                switch_in_s=req.get("switch_in_s"))
            # What an admin did, for whoever reads the log later. Never the
            # passphrase itself.
            self._log("admin: pushed revision %d: %s%s, switch %s" % (
                rev, ", ".join(sorted(settings)) or "-",
                " + passphrase" if req.get("passphrase") else "",
                "in %d s" % req["switch_in_s"] if req.get("switch_in_s") else "on accept"))
            return {"rev": rev, "roster": self.net.roster()}
        if cmd == "allow":
            pid = self._phone(req.get("phone"))
            name = next((p["name"] for p in self.net.roster() if p["phone"] == pid), "")
            self.allow[pid] = name or self.allow.get(pid, "")
            self.save()
            self.net.admitted(pid)
            self._log("admin: allowed %s (%s)" % (pid, self.allow[pid] or "not seen yet"))
            return {"allowed": pid, "name": self.allow[pid]}
        if cmd == "revoke":
            pid = self._phone(req.get("phone"))
            self.allow.pop(pid, None)
            self.net.drop(pid)
            self.save()
            self._log("admin: revoked %s" % pid)
            return {"revoked": pid}
        if cmd == "web":
            if self.web is None:
                raise ss.Refused("The admin page is off: start the server with --web-port 8080")
            return {"port": self.web.port}
        if cmd == "card":
            return {"card": sn.make_card(self.host.keys, self.name, req["addr"],
                                      req.get("port") or self.net.port),
                    "host": self.host.keys.host_id}
        raise ss.Refused("Unknown command %r" % cmd)


def _log(msg):
    print("%s %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


@contextlib.contextmanager
def _socket_path(path):
    """`path`, or a short way to the same file. A Unix socket's path must fit
    in 108 bytes, and a state directory need not (found 2026-10-22: a server
    crashed at start in a deeper directory than its tests used). On Linux the
    directory is reached through /proc/self/fd instead, which is short."""
    if len(path.encode("utf-8")) < 100:
        yield path
        return
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        yield "/proc/self/fd/%d/%s" % (fd, os.path.basename(path))
    finally:
        os.close(fd)


def call(state_dir, req, timeout=10.0):
    """One admin command to the running server."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        with _socket_path(os.path.join(state_dir, "control.sock")) as path:
            s.connect(path)
    except OSError:
        raise ss.Refused("No server running with state in %s" % state_dir)
    try:
        s.sendall(json.dumps(req).encode("utf-8") + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    return json.loads(buf.decode("utf-8"))


# -- the command line ------------------------------------------------------------

def _when(t):
    return time.strftime("%H:%M:%S", time.localtime(t)) if t else "-"


def roster_lines(status):
    lines = ["%s  host %s  port %d  %s" % (
        status["name"], " ".join(status["host"][i:i + 4] for i in range(0, 16, 4)),
        status["port"], "OPEN to every phone" if status["open"]
        else "allow list: %d phone(s)" % len(status["allow"]))]
    lp = status.get("last_push")
    if lp:
        lines.append("last push: revision %d (%s), switch at %s%s" % (
            lp["rev"], ", ".join(lp["keys"]), _when(lp["switch_at"]),
            "" if lp["passphrase_kept"] or "passphrase" not in lp["keys"]
            else "; passphrase NOT kept since a restart"))
    roster = status.get("roster") or []
    seen = {p["phone"] for p in roster}
    for p in roster:
        lines.append("  %-16s %-20s %-10s %s%s" % (
            p["phone"], p.get("name", "")[:20],
            "connected" if p.get("connected") else "away",
            p.get("state") or "nothing pushed yet",
            (" (rev %s)" % p["rev"]) if p.get("rev") else "")
            + ((" -- " + p["detail"]) if p.get("detail") else ""))
    for pid, name in sorted(status["allow"].items()):
        if pid not in seen:
            lines.append("  %-16s %-20s %-10s allowed, not connected yet" % (pid, name[:20], ""))
    if not roster and not status["allow"]:
        lines.append("  no phones yet")
    return lines


def _qr(text):
    """The code as a QR in the terminal, if qrencode is installed and this
    is a terminal (not a pipe or a log)."""
    if not sys.stdout.isatty():
        return
    if shutil.which("qrencode"):
        subprocess.run(["qrencode", "-t", "ansiutf8", text], check=False)
    else:
        print("(install qrencode to show this as a QR code here)")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="settings_server.py",
                                 description="Push radio settings to the team's phones.")
    ap.add_argument("--state", default=os.path.expanduser("~/.rtak-settings-server"),
                    help="state directory (default ~/.rtak-settings-server)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sv = sub.add_parser("serve", help="run the server")
    sv.add_argument("--name", help="the name phones show (default: the hostname)")
    sv.add_argument("--port", type=int, default=sn.HOST_PORT)
    sv.add_argument("--bind", default="", help="address to listen on (default: all)")
    sv.add_argument("--lan", action="store_true",
                    help="also send the WiFi beacon, so phones on this network find it")
    sv.add_argument("--open", action="store_true",
                    help="push to every phone that connects, no allow list")
    sv.add_argument("--web-port", type=int,
                    help="also serve the admin page on 127.0.0.1:PORT (see web-url)")
    sv.add_argument("--ping-s", type=int, default=SERVER_PING_S,
                    help="keep-alive interval asked of phones (default %d)" % SERVER_PING_S)
    sub.add_parser("status", help="the roster and the last push")
    sub.add_parser("web-url", help="the admin page's address, with its key")
    pu = sub.add_parser("push", help="push settings to the phones")
    pu.add_argument("--lora-preset")
    pu.add_argument("--lora-freq-mhz", type=float)
    pu.add_argument("--lora-tx-dbm", type=int)
    pu.add_argument("--fm-mhz")
    pu.add_argument("--fm-w")
    pu.add_argument("--tx-height-m")
    pu.add_argument("--passphrase", action="store_true",
                    help="also push the network passphrase (asked for; phones must opt in)")
    when = pu.add_mutually_exclusive_group()
    when.add_argument("--switch-in-min", type=float, default=ss.DEFAULT_SWITCH_DELAY_S / 60.0)
    when.add_argument("--now", action="store_true", help="each phone switches on accepting")
    for name in ("allow", "revoke"):
        p = sub.add_parser(name, help="%s a phone (its id, or enough of it)" % name)
        p.add_argument("phone")
    ca = sub.add_parser("card", help="the code phones scan to add this server")
    ca.add_argument("--addr", required=True,
                    help="the address phones reach this server at (public name or IP)")
    ca.add_argument("--port", type=int)
    a = ap.parse_args(argv)

    if a.cmd == "serve":
        server = Server(a.state, name=a.name, port=a.port, bind_addr=a.bind, lan=a.lan,
                        open_=a.open, ping_s=a.ping_s, web_port=a.web_port)
        server.start()
        done = threading.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: done.set())
        while not done.wait(1.0):
            pass
        server.stop()
        return 0

    try:
        if a.cmd == "status":
            st = call(a.state, {"cmd": "status"})
            if "error" not in st:
                print("\n".join(roster_lines(st)))
                return 0
        elif a.cmd == "web-url":
            st = call(a.state, {"cmd": "web"})
            if "error" not in st:
                import settings_web
                print("http://127.0.0.1:%d/#token=%s" % (st["port"], settings_web.load_token(a.state)))
                print("From another computer: ssh -L %d:localhost:%d <this server>, then open "
                      "the same address there." % (st["port"], st["port"]), file=sys.stderr)
                return 0
        elif a.cmd == "push":
            settings = {}
            if a.lora_preset is not None:
                settings["lora_preset"] = a.lora_preset
            if a.lora_freq_mhz is not None:
                settings["lora_freq_mhz"] = "%.3f" % a.lora_freq_mhz
            if a.lora_tx_dbm is not None:
                settings["lora_tx_dbm"] = a.lora_tx_dbm
            cov = {k: v for k, v in (("fm_mhz", a.fm_mhz), ("fm_w", a.fm_w),
                                     ("tx_height_m", a.tx_height_m)) if v is not None}
            if cov:
                settings["coverage"] = cov
            passphrase = None
            if a.passphrase:
                passphrase = getpass.getpass("Network passphrase: ")
                if passphrase != getpass.getpass("Again: "):
                    print("The two did not match; nothing pushed.", file=sys.stderr)
                    return 1
            st = call(a.state, {"cmd": "push", "settings": settings, "passphrase": passphrase,
                                "switch_in_s": None if a.now
                                else int(round(a.switch_in_min * 60))})
            if "error" not in st:
                print("pushed revision %d" % st["rev"])
                return 0
        elif a.cmd in ("allow", "revoke"):
            st = call(a.state, {"cmd": a.cmd, "phone": a.phone})
            if "error" not in st:
                print("%s %s" % ("allowed" if a.cmd == "allow" else "revoked",
                                 st.get("allowed") or st.get("revoked")))
                return 0
        elif a.cmd == "card":
            st = call(a.state, {"cmd": "card", "addr": a.addr, "port": a.port})
            if "error" not in st:
                print(st["card"])
                _qr(st["card"])
                print("host key %s -- phones show it when they add this server"
                      % " ".join(st["host"][i:i + 4] for i in range(0, 16, 4)))
                return 0
    except ss.Refused as e:
        st = {"error": str(e)}
    print(st["error"], file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
