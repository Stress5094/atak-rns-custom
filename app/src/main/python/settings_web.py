"""
The settings server's admin page (the settings-sharing notes, step 4): the
roster live, allow / revoke, a push form, and the server's code for phones to
scan. Started by `settings_server.py serve --web-port 8080`.

WHO CAN USE IT. It answers on 127.0.0.1 only; from another computer it is
reached through an SSH tunnel (`ssh -L 8080:localhost:8080 server`), so it
opens no new way in from the network. On the machine itself two more things
could reach a localhost port, and both are shut out:

- another user of the machine: every /api/ request must carry the secret in
  `web_token` (state directory, owner-only). `settings_server.py web-url`
  prints the page's address with it, after `#` so it never reaches a log or
  another site as a Referer;
- a web page open in the admin's browser (another site posting to localhost):
  the secret travels in a header, which a page from another site cannot set
  without asking first (CORS), and nothing here ever says yes. The Host
  header must also name this machine, against DNS rebinding.

Everything goes through settings_server.call, the admin socket the command
line uses, so the page and the command line cannot disagree.
"""

import hmac
import json
import os
import secrets
import shutil
import subprocess
import threading

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import settings_share as ss

TOKEN_FILE = "web_token"
MAX_BODY = 16 * 1024


def load_token(state_dir):
    """The page's secret, created on first use (owner-only)."""
    path = os.path.join(state_dir, TOKEN_FILE)
    if not os.path.exists(path):
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(24))
    with open(path) as f:
        return f.read().strip()


def qr_svg(text):
    """The code as an SVG, if qrencode is installed; else None."""
    if not shutil.which("qrencode"):
        return None
    r = subprocess.run(["qrencode", "-t", "SVG", "-m", "2", "-o", "-", text],
                       capture_output=True, timeout=10)
    if r.returncode != 0:
        return None
    svg = r.stdout.decode("utf-8")
    return svg[svg.find("<svg"):] if "<svg" in svg else None   # no XML prolog in a page


class WebAdmin(object):
    def __init__(self, state_dir, port=8080, bind="127.0.0.1", log=None):
        self.dir = state_dir
        self.token = load_token(state_dir)
        self._bind = bind
        self._port = port
        self._log = log or (lambda m: None)
        self._httpd = None

    @property
    def port(self):
        return self._httpd.server_address[1] if self._httpd else self._port

    def url(self):
        return "http://127.0.0.1:%d/#token=%s" % (self.port, self.token)

    def start(self):
        admin = self

        class Handler(_Handler):
            pass
        Handler.admin = admin
        self._httpd = ThreadingHTTPServer((self._bind, self._port), Handler)
        self._httpd.daemon_threads = True
        threading.Thread(target=self._httpd.serve_forever, name="settings-web",
                         daemon=True).start()
        self._log("admin page on http://127.0.0.1:%d/ (settings_server.py web-url "
                  "prints it with its key)" % self.port)

    def stop(self):
        if self._httpd:
            self._httpd.shutdown()
            self._httpd.server_close()

    def api(self, method, path, body):
        """One API request -> (status, object). Uses the admin socket."""
        import settings_server as srv
        if method == "GET" and path == "/api/status":
            return 200, srv.call(self.dir, {"cmd": "status"})
        if method == "POST" and path == "/api/push":
            return 200, srv.call(self.dir, {"cmd": "push",
                                            "settings": body.get("settings") or {},
                                            "passphrase": body.get("passphrase") or None,
                                            "switch_in_s": body.get("switch_in_s")})
        if method == "POST" and path in ("/api/allow", "/api/revoke"):
            return 200, srv.call(self.dir, {"cmd": path[5:], "phone": body.get("phone", "")})
        if method == "POST" and path == "/api/card":
            out = srv.call(self.dir, {"cmd": "card", "addr": str(body.get("addr", "")).strip(),
                                      "port": body.get("port") or None})
            if "card" in out:
                out["svg"] = qr_svg(out["card"])
            return 200, out
        return 404, {"error": "No such page"}


class _Handler(BaseHTTPRequestHandler):
    admin = None
    server_version = "rtak-settings"
    sys_version = ""

    def log_message(self, fmt, *args):      # requests are not worth a log line each
        pass

    def _send(self, status, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; "
                         "style-src 'unsafe-inline'; img-src data:; connect-src 'self'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(data)

    def _host_ok(self):
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
        return host in ("127.0.0.1", "localhost", "::1")

    def _handle(self, method):
        if not self._host_ok():
            return self._send(403, {"error": "Open this page as http://127.0.0.1:PORT/"})
        path = self.path.split("?", 1)[0].split("#", 1)[0]
        if method == "GET" and path == "/":
            return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
        if not path.startswith("/api/"):
            return self._send(404, {"error": "No such page"})
        given = self.headers.get("X-Token") or ""
        if not hmac.compare_digest(given.encode("utf-8"), self.admin.token.encode("utf-8")):
            return self._send(403, {"error": "This page needs its key: open the address "
                                             "`settings_server.py web-url` prints"})
        body = {}
        if method == "POST":
            if (self.headers.get("Content-Type") or "").split(";")[0] != "application/json":
                return self._send(415, {"error": "JSON only"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                return self._send(413, {"error": "Too large"})
            try:
                body = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"error": "Not JSON"})
            if not isinstance(body, dict):
                return self._send(400, {"error": "Not a JSON object"})
        try:
            status, out = self.admin.api(method, path, body)
        except ss.Refused as e:
            status, out = 200, {"error": str(e)}
        except Exception as e:  # noqa: BLE001 -- said on the page, not fatal
            status, out = 500, {"error": "%s: %s" % (type(e).__name__, e)}
        self._send(status, out)

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_OPTIONS(self):
        # Never agree to a cross-site request: no CORS headers, ever.
        self._send(405, {"error": "No"})


PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Settings server</title>
<style>
:root { --bg:#f6f7f9; --card:#fff; --ink:#1d2330; --muted:#667085; --line:#e3e6eb;
        --accent:#1f6feb; --good:#1a7f37; --warn:#9a6700; --bad:#cf222e; --chip:#eef1f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#0f1216; --card:#171b21; --ink:#e6e9ef; --muted:#97a0ae; --line:#2a3039;
          --accent:#4c8dff; --good:#3fb950; --warn:#d29922; --bad:#f85149; --chip:#222831; }
}
* { box-sizing:border-box }
body { margin:0; background:var(--bg); color:var(--ink);
       font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif }
main { max-width:980px; margin:0 auto; padding:20px 16px 48px }
header { display:flex; flex-wrap:wrap; align-items:baseline; gap:8px 16px; margin-bottom:16px }
h1 { font-size:22px; margin:0 }
h2 { font-size:16px; margin:0 0 12px }
.muted { color:var(--muted) }
.key { font-family:ui-monospace,SFMono-Regular,Menlo,monospace; letter-spacing:.02em }
section { background:var(--card); border:1px solid var(--line); border-radius:10px;
          padding:16px; margin-bottom:16px }
table { width:100%; border-collapse:collapse }
th, td { text-align:left; padding:8px 6px; border-top:1px solid var(--line); vertical-align:top }
th { color:var(--muted); font-weight:500; font-size:13px; border-top:none }
.chip { display:inline-block; padding:1px 8px; border-radius:99px; background:var(--chip);
        font-size:13px; white-space:nowrap }
.good { color:var(--good) } .warn { color:var(--warn) } .bad { color:var(--bad) }
button { font:inherit; border:1px solid var(--line); background:var(--card); color:var(--ink);
         border-radius:7px; padding:6px 12px; cursor:pointer }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff }
button.danger { color:var(--bad) }
button:disabled { opacity:.5; cursor:default }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); gap:12px }
label { display:block; font-size:13px; color:var(--muted) }
input, select { width:100%; font:inherit; color:var(--ink); background:var(--bg);
                border:1px solid var(--line); border-radius:7px; padding:7px 9px; margin-top:4px }
fieldset { border:1px solid var(--line); border-radius:8px; padding:10px 12px 12px; margin:0 0 12px }
legend { font-size:13px; color:var(--muted); padding:0 4px }
.row { display:flex; flex-wrap:wrap; gap:10px; align-items:center }
.note { font-size:13px; color:var(--muted); margin-top:8px }
#msg { min-height:1.4em; margin-top:10px }
.qr svg { width:220px; height:220px; background:#fff; padding:6px; border-radius:6px }
.code { word-break:break-all; font-family:ui-monospace,monospace; font-size:12px;
        background:var(--bg); border:1px solid var(--line); border-radius:6px; padding:8px }
.wrap { overflow-x:auto }
#locked { display:none }
</style>
</head>
<body>
<main>
  <header>
    <h1 id="name">Settings server</h1>
    <span class="muted">key <span class="key" id="hostkey">…</span></span>
    <span class="chip" id="mode">…</span>
    <span class="muted" id="updated"></span>
  </header>

  <section id="locked">
    <h2>This page needs its key</h2>
    <p>On the server, run <span class="key">settings_server.py --state DIR web-url</span>
       and open the address it prints.</p>
  </section>

  <div id="app">
  <section>
    <h2>Phones</h2>
    <div class="wrap"><table>
      <thead><tr><th>Phone</th><th>Id</th><th>Connection</th><th>Settings</th><th></th></tr></thead>
      <tbody id="phones"><tr><td colspan="5" class="muted">Loading…</td></tr></tbody>
    </table></div>
    <p class="note">A phone that connects and is not allowed waits here. Check its id against
      the phone's own screen (Share settings → Servers) before allowing it.</p>
    <div class="row" style="margin-top:8px">
      <input id="aheadId" placeholder="Allow a phone not seen yet: its 16-character id"
             style="max-width:360px">
      <button id="aheadBtn">Allow</button>
    </div>
  </section>

  <section>
    <h2>Push settings</h2>
    <p class="muted" id="lastpush">No push yet.</p>
    <fieldset>
      <legend>LoRa radio (leave blank to leave unchanged)</legend>
      <div class="grid">
        <label>Preset<select id="preset"><option value="">(unchanged)</option>
          <option>Short Turbo</option><option>Short Fast</option><option>Short Slow</option>
          <option>Medium Fast</option><option>Medium Slow</option><option>Long Fast</option>
          <option>Long Moderate</option><option>Long Slow</option></select></label>
        <label>Frequency (MHz)<input id="freq" inputmode="decimal" placeholder="e.g. 915.000"></label>
        <label>Power (dBm)<input id="dbm" inputmode="numeric" placeholder="e.g. 22"></label>
      </div>
    </fieldset>
    <fieldset>
      <legend>Coverage model</legend>
      <div class="grid">
        <label>FM frequency (MHz)<input id="fmmhz" inputmode="decimal" placeholder="e.g. 146.520"></label>
        <label>FM power (W)<input id="fmw" inputmode="decimal" placeholder="e.g. 5"></label>
        <label>Antenna height (m)<input id="height" inputmode="decimal" placeholder="e.g. 1.5"></label>
      </div>
    </fieldset>
    <fieldset>
      <legend>Network passphrase</legend>
      <div class="grid">
        <label>New passphrase<input id="pass" type="password" autocomplete="off"></label>
        <label>Again<input id="pass2" type="password" autocomplete="off"></label>
      </div>
      <p class="note bad">Only phones that chose to accept the passphrase over the network take
        it; it is encrypted to each phone separately and never written to the server's disk.
        A phone that switches alone stops hearing the others.</p>
    </fieldset>
    <div class="row">
      <label style="max-width:220px">Switch in (minutes; 0 = each phone when it accepts)
        <input id="mins" inputmode="decimal" value="2"></label>
      <button class="primary" id="pushBtn">Push to allowed phones</button>
    </div>
    <div id="msg"></div>
  </section>

  <section>
    <h2>Add this server on a phone</h2>
    <div class="row">
      <input id="addr" placeholder="The address phones reach this server at" style="max-width:360px">
      <button id="cardBtn">Show code</button>
    </div>
    <p class="note">On the phone: Share settings → Servers → Scan a server code.</p>
    <div id="card"></div>
  </section>
  </div>
</main>
<script>
"use strict";
const token = new URLSearchParams(location.hash.slice(1)).get("token") || "";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g,
  (c) => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const groups = (id) => (id || "").replace(/(.{4})(?=.)/g, "$1 ");
const hhmm = (t) => t ? new Date(t * 1000).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "";

async function api(path, body) {
  const opts = {headers: {"X-Token": token}};
  if (body !== undefined) {
    opts.method = "POST";
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(path, opts);
  const out = await r.json().catch(() => ({error: "Bad answer (" + r.status + ")"}));
  if (r.status === 403) { $("locked").style.display = "block"; $("app").style.display = "none"; }
  return out;
}

const WORDS = {
  "waiting for the admin": ["waiting for you to allow it", "warn"],
  "sent": ["sent, no answer yet", "muted"],
  "not reached": ["not reached", "warn"],
  "pending": ["waiting for its PIN", "warn"],
  "scheduled": ["accepted, switches at the set time", "good"],
  "applied": ["switched", "good"], "switched": ["switched", "good"],
  "rejected": ["rejected", "bad"], "refused": ["refused", "bad"], "failed": ["failed", "bad"],
  "reverted": ["switched back by itself (heard nobody)", "bad"],
  "undone": ["undone by hand", "warn"],
  "push again": ["push again: the passphrase is not kept across a restart", "bad"],
};

function render(st) {
  $("name").textContent = st.name;
  $("hostkey").textContent = groups(st.host);
  $("mode").textContent = st.open ? "open to every phone" : "allow list: " + Object.keys(st.allow).length;
  $("updated").textContent = "updated " + hhmm(Date.now() / 1000);
  const lp = st.last_push;
  $("lastpush").textContent = lp ? "Last push: revision " + lp.rev + " (" + lp.keys.join(", ") + ")" +
      (lp.switch_at ? ", switch at " + hhmm(lp.switch_at) : ", each phone when it accepts") +
      (lp.keys.includes("passphrase") && !lp.passphrase_kept ? " — passphrase not kept since a restart" : "")
    : "No push yet.";
  const rows = [];
  const seen = new Set();
  for (const p of st.roster) {
    seen.add(p.phone);
    const waiting = p.state === "waiting for the admin";
    const [w, cls] = WORDS[p.state] || [p.state || "nothing pushed yet", "muted"];
    rows.push("<tr><td>" + esc(p.name) + "</td><td class='key'>" + esc(groups(p.phone)) + "</td><td>" +
      (p.connected ? "<span class='good'>connected</span>" : "<span class='muted'>away</span>") +
      "</td><td><span class='" + cls + "'>" + esc(w) + "</span>" + (p.rev ? " <span class='muted'>(rev " + p.rev + ")</span>" : "") +
      // The phone's own note, unless it only repeats the state.
      (p.detail && !p.detail.startsWith(w) && !w.startsWith(p.detail) ?
        "<div class='note'>" + esc(p.detail) + "</div>" : "") + "</td><td>" +
      (waiting ? "<button class='primary' data-allow='" + esc(p.phone) + "'>Allow</button>"
               : "<button class='danger' data-revoke='" + esc(p.phone) + "'>Revoke</button>") + "</td></tr>");
  }
  for (const [id, name] of Object.entries(st.allow)) {
    if (seen.has(id)) continue;
    rows.push("<tr><td>" + esc(name || "—") + "</td><td class='key'>" + esc(groups(id)) +
      "</td><td class='muted'>not connected yet</td><td class='muted'>allowed</td><td>" +
      "<button class='danger' data-revoke='" + esc(id) + "'>Revoke</button></td></tr>");
  }
  $("phones").innerHTML = rows.length ? rows.join("") :
    "<tr><td colspan='5' class='muted'>No phones yet. Phones add this server from its code (below).</td></tr>";
}

async function refresh() {
  const st = await api("/api/status");
  if (!st.error) render(st);
}

function say(text, cls) { $("msg").className = cls || ""; $("msg").textContent = text; }

$("phones").addEventListener("click", async (e) => {
  const allow = e.target.dataset.allow, revoke = e.target.dataset.revoke;
  if (revoke && !confirm("Revoke " + groups(revoke) + "? It is disconnected and sent nothing more.")) return;
  if (allow || revoke) {
    e.target.disabled = true;
    const r = await api(allow ? "/api/allow" : "/api/revoke", {phone: allow || revoke});
    if (r.error) alert(r.error);
    refresh();
  }
});

$("aheadBtn").addEventListener("click", async () => {
  const r = await api("/api/allow", {phone: $("aheadId").value});
  if (r.error) alert(r.error); else $("aheadId").value = "";
  refresh();
});

$("pushBtn").addEventListener("click", async () => {
  const s = {};
  if ($("preset").value) s.lora_preset = $("preset").value;
  if ($("freq").value.trim()) {
    const f = parseFloat($("freq").value);
    if (isNaN(f)) return say("The frequency must be a number of MHz", "bad");
    s.lora_freq_mhz = f.toFixed(3);
  }
  if ($("dbm").value.trim()) {
    const d = Number($("dbm").value);
    if (!Number.isInteger(d)) return say("The power must be a whole number of dBm", "bad");
    s.lora_tx_dbm = d;
  }
  const cov = {};
  if ($("fmmhz").value.trim()) cov.fm_mhz = $("fmmhz").value.trim();
  if ($("fmw").value.trim()) cov.fm_w = $("fmw").value.trim();
  if ($("height").value.trim()) cov.tx_height_m = $("height").value.trim();
  if (Object.keys(cov).length) s.coverage = cov;
  const pass = $("pass").value;
  if (pass !== $("pass2").value) return say("The two passphrases do not match; nothing pushed.", "bad");
  const mins = parseFloat($("mins").value || "0");
  if (isNaN(mins) || mins < 0) return say("Minutes must be 0 or more", "bad");
  const what = Object.keys(s).concat(pass ? ["passphrase"] : []);
  if (!what.length) return say("Fill in at least one setting to push.", "bad");
  if (!confirm("Push " + what.join(", ") + " to every allowed phone" +
      (mins > 0 ? ", switching in " + mins + " min?" : ", each switching when it accepts?"))) return;
  $("pushBtn").disabled = true;
  const r = await api("/api/push", {settings: s, passphrase: pass || null,
                                    switch_in_s: mins > 0 ? Math.round(mins * 60) : null});
  $("pushBtn").disabled = false;
  if (r.error) return say(r.error, "bad");
  $("pass").value = $("pass2").value = "";
  say("Pushed revision " + r.rev + ".", "good");
  refresh();
});

$("cardBtn").addEventListener("click", async () => {
  const r = await api("/api/card", {addr: $("addr").value});
  if (r.error) { $("card").innerHTML = "<p class='bad'>" + esc(r.error) + "</p>"; return; }
  $("card").innerHTML = (r.svg ? "<div class='qr'>" + r.svg + "</div>"
      : "<p class='note'>Install qrencode on the server to show this as a QR code.</p>") +
    "<p class='code'>" + esc(r.card) + "</p>";
});

if (!token) { $("locked").style.display = "block"; $("app").style.display = "none"; }
else { refresh(); setInterval(refresh, 2000); }
</script>
</body>
</html>
"""
