"""
Settings sharing, wired to this app: the Kotlin entry points for the share /
scan / approve screens, and the bridge's radio as settings_share.Receiver's
injected radio. The rules are settings_share's; this file only says where
each setting lives. Plan: docs/settings-sharing.md.

WHERE EACH SETTING LIVES

  lora_preset / lora_freq_mhz / lora_tx_dbm   rns_bridge.set_radio_config
        (persisted; a Meshtastic board takes them through its own confirmed
        "Use the app's settings", as for a hand edit)
  passphrase                                  rns_bridge.set_mesh_passphrase
  coverage.*                                  KOTLIN's SharedPreferences
        (CoverageSettings). Kotlin passes its current values in with every
        call (`kotlin_json`) and saves the "coverage" a result hands back.
  fm_profile / fm_custom                      KOTLIN's SharedPreferences
        (RadioLink), passed in with every call (`kotlin_json` "fm") like
        coverage, and handed back as "fm" for Kotlin to save. APPLIED LIVE
        through rns_bridge.set_fm_profile, not by a restart: a share can land
        from the service at a scheduled switch with ATAK in front, where
        Android refuses the restart trampoline, and live is what lets every
        phone change at the same moment. Only the NET settings travel (the
        net code); this phone's keying, lead-in, filter and copies stay.

Every entry point takes and returns JSON strings and never raises: the
screens show {"error": ...} instead.

OVER WIFI (step 3, settings_net): this phone listens for settings hosts
(on by default; "listen" in _NET_PATH) and can be one ("hosting"). A
bundle that arrives over WiFi lands here on a network thread, outside any
call from Kotlin, so what Kotlin must hear about it -- an offer waiting, a
trusted change applied, coverage to save -- is queued and handed over on the
service's next tick. Every outcome is reported back to the host's roster.

HOSTS HEARD ON WIFI are connected to only once this phone's operator says so
("approved_hosts" in _NET_PATH, by host key); a new one is handed to Kotlin
as a "host_found" event, and one the operator ignored ("declined_hosts") is
not asked about again.

SERVERS (step 4): hosts added by address, from a server's code ("servers" in
_NET_PATH). Connected to whether or not this phone listens on WiFi -- a
server on the internet is reached over mobile data too.
"""

import json
import os
import threading
import time

import settings_share as ss

_RECEIVER_PATH = os.path.expanduser("~/.atakrns_settings_share.json")
_HOST_KEY_PATH = os.path.expanduser("~/.atakrns_host_key.json")
_HOST_REV_PATH = os.path.expanduser("~/.atakrns_host_rev.json")
_NET_PATH = os.path.expanduser("~/.atakrns_settings_net.json")

UNSUPPORTED = ()

# RLock because _call() holds _lock across fn(), and fn() reaches
# _get_receiver()/_get_host(), which now take the lock themselves.
_lock = threading.RLock()
_receiver = None
_host = None
# The Coverage settings as Kotlin last passed them, and any it must still
# save. Not cleared per call: a change applied from the network waits here
# until Kotlin's next call picks it up.
_coverage = {}
_coverage_out = None
# The FM settings as Kotlin last passed them ({"profile", "custom_spec",
# "vox"}), and any it must still save -- the same arrangement as coverage.
_fm = {}
_fm_out = None
# What Kotlin must hear about that happened outside a call from it.
_events = []
_net_client = None
_net_server = None
_multicast_lock = None


def _bridge():
    import rns_bridge
    return rns_bridge


# -- this phone's settings, in bundle form -----------------------------------

def _current():
    rb = _bridge()
    cfg = rb._load_radio_config()
    passphrase, _ = rb._load_passphrase()
    return {"lora_preset": cfg["preset"],
            "lora_freq_mhz": "%.3f" % (cfg["frequency_hz"] / 1e6),
            "lora_tx_dbm": int(cfg["txpower_dbm"]),
            "coverage": dict(_coverage),
            "passphrase": passphrase,
            **_fm_current()}


def _fm_current():
    """fm_profile, and fm_custom when it is "custom" -- only once Kotlin has
    said what they are. A phone whose Kotlin predates this sends no "fm", and
    then FM is neither shared from nor applied to it."""
    if not _fm.get("profile"):
        return {}
    # fm_custom always present ("" on a preset), so a before/after list and
    # the undo snapshot read "" rather than None.
    custom = _fm["profile"] == _fm_custom_mod().CUSTOM_NAME
    return {"fm_profile": _fm["profile"],
            "fm_custom": _stored_net_code() if custom else ""}


def _fm_custom_mod():
    import fm_custom
    return fm_custom


def _stored_net_code():
    fc = _fm_custom_mod()
    try:
        return fc.net_code(fc.link_config(fc.CustomSpec.from_json(
            _fm.get("custom_spec") or "")))
    except Exception:
        return ""


def _apply_fm(settings):
    """One problem string, or None. Validates first and changes nothing on a
    refusal; then switches the running link and leaves the new preferences in
    _fm_out for Kotlin."""
    global _fm_out
    import fm_config
    fc = _fm_custom_mod()
    if not _fm.get("profile"):
        return "this phone's app is too old to change its FM settings from a share"
    name = settings["fm_profile"]
    spec_json = _fm.get("custom_spec") or ""
    if name == fc.CUSTOM_NAME:
        code = settings.get("fm_custom") or ""
        try:
            net = fc.parse_net_code(code)
            # The NET settings from the code, this phone's own for the rest
            # (or the default's, if it has never set any up).
            try:
                spec = fc.CustomSpec.from_json(spec_json)
            except fc.SpecError:
                spec = fc.CustomSpec.from_preset(fm_config.DEFAULT_PROFILE)
            for k in ("tones", "first_tone_hz", "tone_spacing_hz",
                      "symbol_rate_bd", "codename"):
                setattr(spec, k, getattr(net, k))
            spec.evidence = {}
            fc.link_config(spec)
        except fm_config.ProfileError as e:
            return "FM: %s cannot be used here: %s" % (code or "(no net code)", e)
        spec_json = spec.to_json()
    elif name not in fm_config.PROFILES:
        return "FM: %r is not a profile this app has" % name
    if name == _fm.get("profile") and (
            name != fc.CUSTOM_NAME or spec_json == (_fm.get("custom_spec") or "")):
        # Already running it. Re-applying would reset the receiver for
        # nothing -- the put-back after a failed group lands here.
        return None
    vox = bool(_fm.get("vox")) and name != fc.CUSTOM_NAME
    got = _bridge().set_fm_profile(name, vox, spec_json)
    if got != name:
        return "FM: the link did not take %s (it is on %r)" % (name, got)
    _fm.update(profile=name, custom_spec=spec_json)
    _fm_out = {"profile": name, "custom_spec": spec_json}
    return None


def _apply(settings):
    """[] or the problems. Each group is applied whole or not at all."""
    global _coverage_out
    rb = _bridge()
    problems = []
    if any(k.startswith("lora_") for k in settings):
        cur = _current()
        preset = settings.get("lora_preset", cur["lora_preset"])
        freq = settings.get("lora_freq_mhz", cur["lora_freq_mhz"])
        power = int(settings.get("lora_tx_dbm", cur["lora_tx_dbm"]))
        # A pushed power is a REQUEST: this radio may top out lower.
        power = min(power, rb._txpower_max_dbm())
        problem = rb.set_radio_config(preset, freq, power)
        if problem:
            problems.append("Radio: " + problem)
    if "passphrase" in settings:
        if not rb.set_mesh_passphrase(settings["passphrase"]):
            problems.append("The network passphrase was rejected")
    if "fm_profile" in settings:
        problem = _apply_fm(settings)
        if problem:
            problems.append(problem)
    if "coverage" in settings:
        import coverage
        merged = dict(_coverage)
        merged.update({k: str(v) for k, v in settings["coverage"].items()})
        problem = coverage.check_settings(json.dumps(merged))
        if problem:
            problems.append("Coverage: " + problem)
        else:
            _coverage.clear()
            _coverage.update(merged)
            _coverage_out = dict(merged)
    return problems


def _get_receiver():
    # The singleton is created under _lock so the check-then-act is atomic
    # against a network thread's own _get_receiver()/get_host() call and
    # against a test fixture that swaps _RECEIVER_PATH out from under it. See
    # _Fixture.setUp/tearDown in test_settings_link, which hold the same lock.
    global _receiver
    with _lock:
        if _receiver is None:
            _receiver = ss.Receiver(_RECEIVER_PATH, _current, _apply,
                                    unsupported=UNSUPPORTED)
        return _receiver


def _get_host():
    global _host
    with _lock:
        if _host is None:
            rb = _bridge()
            _host = ss.Host(ss.HostKeys.load_or_create(_HOST_KEY_PATH),
                            rb._node_name or rb._NODE_ID, _HOST_REV_PATH)
        return _host


# -- plumbing ----------------------------------------------------------------

def _call(kotlin_json, fn):
    """Run `fn` under the lock with Kotlin's coverage values in place, and
    hand back any coverage Kotlin must save."""
    global _coverage_out, _fm_out
    try:
        with _lock:
            k = json.loads(kotlin_json) if kotlin_json else {}
            if isinstance(k.get("fm"), dict) and _fm_out is None:
                _fm.clear()
                _fm.update(k["fm"])
            # Unless a change Kotlin has not saved yet is waiting: then
            # Kotlin's values are the stale ones.
            if isinstance(k.get("coverage"), dict) and _coverage_out is None:
                _coverage.clear()
                _coverage.update({a: str(b) for a, b in k["coverage"].items()})
            out = fn()
            if _coverage_out is not None and kotlin_json:
                out["coverage"] = _coverage_out
                _coverage_out = None
            if _fm_out is not None and kotlin_json:
                out["fm"] = _fm_out
                _fm_out = None
            return json.dumps(out)
    except (ss.Refused, ss.PinError) as e:
        return json.dumps({"error": str(e)})
    except Exception as e:  # noqa: BLE001 -- the screen needs a reason
        return json.dumps({"error": "%s: %s" % (type(e).__name__, e)})


def _offer_view(offer):
    """An offer for the screen: never the passphrase itself."""
    rb = _bridge()
    notes = list(offer["notes"])
    want = offer["settings"].get("lora_tx_dbm")
    if want is not None and int(want) > rb._txpower_max_dbm():
        notes.append("This radio can do at most %d dBm, so it will use %d"
                     % (rb._txpower_max_dbm(), rb._txpower_max_dbm()))
    return {"host": offer["host"], "host_name": offer["host_name"],
            "host_ed": offer["host_ed"], "rev": offer["rev"],
            "route": offer.get("route"),
            "switch_at": offer["switch_at"], "trusted": offer["trusted"],
            "outcome": offer.get("outcome"), "changes": offer["changes"],
            "notes": notes}


# -- Kotlin entry points ------------------------------------------------------

def status(kotlin_json=""):
    def run():
        r = _get_receiver()
        h = _get_host()
        u = r._s["undo"]
        return {"has_pin": r.has_pin(), "pin_locked_s": r.pin_locked_for(),
                "phone_id": r.phone_id,
                "trusted": [{"host": k, "name": v["name"]}
                            for k, v in sorted(r.trusted_hosts().items())],
                "accept_passphrase_over_network": r.accepts_passphrase_over_network(),
                "pending": [_offer_view(o) for o in r.pending()],
                "undo": None if u is None else {"host_name": u["host_name"], "rev": u["rev"]},
                "host": {"host_id": h.keys.host_id, "name": h.name},
                "listening": _net_prefs()["listen"] and _net_client is not None,
                "hosts": _net_client.hosts() if _net_client else [],
                "servers": _servers_view(),
                "found": _found_view(),
                "wifi_hosts": _wifi_hosts_view(),
                "ignored": _ignored_view(),
                "hosting": _net_server is not None}
    return _call(kotlin_json, run)


def set_pin(new, old=""):
    return _call("", lambda: (_get_receiver().set_pin(new, old or None), {"ok": True})[1])


def reset_pin():
    return _call("", lambda: (_get_receiver().reset_pin(), {"ok": True})[1])


def untrust_host(host, pin):
    return _call("", lambda: (_get_receiver().untrust_host(host, pin), {"ok": True})[1])


def set_accept_passphrase_over_network(on, pin):
    on = on in (True, "true", "1", 1)
    return _call("", lambda: (_get_receiver().set_accept_passphrase_over_network(on, pin),
                              {"ok": True})[1])


def share_qr(options_json, kotlin_json=""):
    """This phone as a host, for a QR code: the groups ticked in
    {"lora", "coverage", "fm", "passphrase"}. Applies on approval (no switch time):
    phones scan one at a time."""
    def run():
        opts = json.loads(options_json or "{}")
        cur = _current()
        settings = {}
        if opts.get("lora"):
            for k in ("lora_preset", "lora_freq_mhz", "lora_tx_dbm"):
                settings[k] = cur[k]
        if opts.get("coverage") and cur["coverage"]:
            settings["coverage"] = cur["coverage"]
        settings.update(_fm_to_share(opts, cur))
        if not settings and not opts.get("passphrase"):
            raise ss.Refused("Tick at least one group of settings to share")
        host = _get_host()
        rev, bundles = host.bundles(settings, passphrase=cur["passphrase"]
                                    if opts.get("passphrase") else None, switch_in_s=None)
        shown = ss.diff({}, dict(settings, **({"passphrase": "x"}
                                              if opts.get("passphrase") else {})))
        return {"text": bundles["qr"], "rev": rev, "host_name": host.name,
                "host_id": host.keys.host_id, "items": shown}
    return _call(kotlin_json, run)


def _fm_to_share(opts, cur):
    """The FM group of a share, if ticked. Refuses rather than sending a
    custom profile with no usable net code: the far phones would be told to
    switch to settings nobody can build."""
    if not opts.get("fm"):
        return {}
    if "fm_profile" not in cur:
        raise ss.Refused("This phone's FM settings are not known yet")
    out = {"fm_profile": cur["fm_profile"]}
    if cur["fm_profile"] == _fm_custom_mod().CUSTOM_NAME:
        if not cur.get("fm_custom"):
            raise ss.Refused("This phone's custom FM settings cannot be used, "
                             "so they cannot be shared")
        out["fm_custom"] = cur["fm_custom"]
    return out


def receive(text, route, kotlin_json=""):
    """A scanned (route "qr") or received ("network") bundle -> the offer.
    From a trusted host it is already applied: see "outcome"."""
    return _call(kotlin_json, lambda: _offer_view(_get_receiver().receive(text, route)))


def approve(host, pin, trust=False, kotlin_json=""):
    """Accept with this phone's PIN; `trust` also trusts that host from now
    on, on the same PIN."""
    def run():
        r = _get_receiver()
        waiting = {o["host"]: o for o in r.pending()}.get(host)
        if waiting is None:
            raise ss.Refused("Nothing is waiting from that host")
        if trust in (True, "true", "1", 1):
            r.trust_host(host, waiting["host_ed"], waiting["host_name"], pin)
        outcome = r.approve(host, pin)
        _report(host, waiting["rev"], outcome)
        return {"outcome": outcome}
    return _call(kotlin_json, run)


def reject(host):
    def run():
        r = _get_receiver()
        waiting = {o["host"]: o for o in r.pending()}.get(host)
        r.reject(host)
        if waiting is not None:
            _report(host, waiting["rev"], "rejected")
        return {"ok": True}
    return _call("", run)


def undo(kotlin_json=""):
    def run():
        r = _get_receiver()
        u = r._s["undo"]
        problems = r.undo()
        if u is not None and not problems:
            _report(u.get("host"), u["rev"], "undone", "undone by hand on the phone")
        return {"problems": problems}
    return _call(kotlin_json, run)


def tick(kotlin_json=""):
    """Every few seconds from the service: scheduled switches, and whatever arrived over WiFi since the last tick.
    Returns {"events": [[event, detail, host, rev]...]}."""
    def run():
        global _events
        events = [list(e) for e in _get_receiver().tick()]
        for e in events:
            _report(e[2], e[3], e[0], e[1])
        events, _events = _events + events, []
        return {"events": events}
    return _call(kotlin_json, run)


# -- over WiFi (settings_net) -------------------------------------------------

def _log(msg):
    print("[settings] " + msg)


def _report(host, rev, state, detail=""):
    if _net_client is not None and host and rev is not None:
        _net_client.report(host, rev, state, detail)


def _net_prefs():
    try:
        with open(_NET_PATH) as f:
            p = json.load(f)
    except (OSError, ValueError):
        p = {}
    servers = [s for s in p.get("servers") or [] if isinstance(s, dict) and s.get("addr")]
    return {"listen": bool(p.get("listen", True)), "hosting": bool(p.get("hosting", False)),
            "servers": servers,
            "approved_hosts": dict(p.get("approved_hosts") or {}),
            "declined_hosts": dict(p.get("declined_hosts") or {})}


def _save_net_prefs(**changes):
    p = _net_prefs()
    p.update(changes)
    ss._write_json(_NET_PATH, p)


def _wifi_prepare(sock):
    """Pin a socket to the WiFi network: with WiFi and mobile data both up,
    Android sends an unpinned socket wherever it thinks best, which for an
    off-grid router with no internet is mobile data. Through a DUPLICATE of
    the descriptor, so Android never takes ownership of ours (see the fdsan
    note in rns_bridge). False off Android or with no WiFi network (this
    phone may be the hotspot itself, whose clients need no pinning)."""
    try:
        from android.os import ParcelFileDescriptor
        network = _bridge()._wifi_network()
        if network is None:
            return False
        pfd = ParcelFileDescriptor.fromFd(sock.fileno())
        try:
            network.bindSocket(pfd.getFileDescriptor())
        finally:
            pfd.close()
        return True
    except Exception:
        return False


def _broadcast_addrs():
    """Every interface's broadcast address, so a beacon reaches both an
    ordinary WiFi network and this phone's own hotspot."""
    out = ["255.255.255.255"]
    try:
        from java.net import NetworkInterface
        nis = NetworkInterface.getNetworkInterfaces()
        while nis is not None and nis.hasMoreElements():
            ni = nis.nextElement()
            if not ni.isUp() or ni.isLoopback():
                continue
            for ia in ni.getInterfaceAddresses().toArray():
                b = ia.getBroadcast()
                if b is not None and b.getHostAddress() not in out:
                    out.append(b.getHostAddress())
    except Exception:
        pass
    return out


def _hold_multicast_lock(on):
    """Android's WiFi drops broadcasts to save power unless an app holds this
    lock -- the same reason PythonRnsService holds one for the WiFi link."""
    global _multicast_lock
    try:
        if on and _multicast_lock is None:
            from android.content import Context
            from com.chaquo.python import Python
            app = Python.getPlatform().getApplication()
            wm = app.getSystemService(Context.WIFI_SERVICE)
            _multicast_lock = wm.createMulticastLock("rtak-settings")
            _multicast_lock.setReferenceCounted(False)
            _multicast_lock.acquire()
        elif not on and _multicast_lock is not None:
            _multicast_lock.release()
            _multicast_lock = None
    except Exception:
        pass


def _hello(host_id):
    r = _get_receiver()
    rb = _bridge()
    return {"phone": r.phone_id, "x_pub": ss._b64(r.x_pub),
            "name": rb._node_name or rb._NODE_ID, "last_rev": r.last_rev(host_id)}


def _on_network_bundle(host_id, text):
    """A bundle from a host over WiFi -> (rev, state, detail) for its roster."""
    with _lock:
        try:
            rev = ss.decode(text)["body"].get("rev")
        except ss.Refused as e:
            return None, "refused", str(e)
        try:
            offer = _get_receiver().receive(text, ss.ROUTE_NETWORK)
        except ss.Refused as e:
            _log("refused a bundle from %s: %s" % (host_id, e))
            return rev, "refused", str(e)
        except Exception as e:  # noqa: BLE001 -- the host's roster needs a reason
            return rev, "failed", "%s: %s" % (type(e).__name__, e)
        view = _offer_view(offer)
        _events.append(["offer" if offer["outcome"] == "pending" else offer["outcome"],
                        json.dumps(view), offer["host"], offer["rev"]])
        _log("bundle rev %s from %s: %s" % (rev, offer["host_name"], offer["outcome"]))
        return rev, offer["outcome"], "; ".join(offer["notes"])


def _start_client():
    """The client as the prefs say: listening on WiFi, connected to the
    servers added, both, or neither (no client at all). Restarted when that
    changes, since listening is fixed when a client starts."""
    global _net_client
    import settings_net as sn
    prefs = _net_prefs()
    if _net_client is not None:
        _net_client.stop()
        _net_client = None
    if not prefs["listen"] and not prefs["servers"]:
        return
    client = sn.PhoneClient(_hello, _on_network_bundle,
                            lambda epk: _get_receiver().exchange(epk),
                            listen=prefs["listen"], prepare_socket=_wifi_prepare,
                            log=_log, own_hosts=lambda: (_get_host().keys.host_id,),
                            approved=lambda h: h in _net_prefs()["approved_hosts"],
                            on_found=_found_host, on_first_key=_server_first_key)
    client.start()
    for srv in prefs["servers"]:
        client.add_host(srv["addr"], srv["port"], srv.get("host"), srv.get("name", ""))
    _net_client = client


def _server_first_key(addr, port, host_id, name):
    """A server added by typed address answered for the first time: keep its
    key, so a different machine at that address is refused from now on."""
    servers = _net_prefs()["servers"]
    for srv in servers:
        if (srv["addr"], int(srv["port"])) == (addr, int(port)) and not srv.get("host"):
            srv.update({"host": host_id, "name": srv.get("name") or name, "first": True})
            _save_net_prefs(servers=servers)
            _log("server %s:%d answered as %s; that key is now required there"
                 % (addr, port, host_id))
            return


def _found_host(host_id, name, addr):
    """A host heard on WiFi that this phone has not approved: ask, unless
    the operator already said no to it."""
    if host_id in _net_prefs()["declined_hosts"]:
        return
    _log("host found on WiFi, not connected until approved: %s (%s) at %s"
         % (name, host_id, addr))
    _events.append(["host_found", json.dumps({"host": host_id, "name": name, "addr": addr}),
                    host_id, None])


def _found_view():
    if _net_client is None:
        return []
    declined = _net_prefs()["declined_hosts"]
    return [f for f in _net_client.found() if f["host"] not in declined]


def _wifi_hosts_view():
    """Hosts heard on WiFi that this phone approved, connected or not."""
    live = {}
    if _net_client is not None:
        for h in _net_client.hosts():
            if not h["manual"]:
                live[h["host"]] = h
    return [{"host": hid, "name": live.get(hid, {}).get("name") or name,
             "connected": bool(live.get(hid, {}).get("connected")),
             "heard": hid in live, "problem": live.get(hid, {}).get("problem", "")}
            for hid, name in sorted(_net_prefs()["approved_hosts"].items())]


def host_connect(host_id):
    """The operator approved a host heard on WiFi: connect to it, now and
    whenever it is heard again."""
    def run():
        prefs = _net_prefs()
        name = next((f["name"] for f in (_net_client.found() if _net_client else [])
                     if f["host"] == host_id), "") or prefs["declined_hosts"].get(host_id, "")
        approved = dict(prefs["approved_hosts"], **{host_id: name})
        declined = {k: v for k, v in prefs["declined_hosts"].items() if k != host_id}
        _save_net_prefs(approved_hosts=approved, declined_hosts=declined)
        if _net_client is not None:
            _net_client.approve_host(host_id)
        return {"approved": host_id, "name": name, "wifi_hosts": _wifi_hosts_view()}
    return _call("", run)


def host_ignore(host_id):
    """The operator said no to a host heard on WiFi: not asked again."""
    def run():
        prefs = _net_prefs()
        name = next((f["name"] for f in (_net_client.found() if _net_client else [])
                     if f["host"] == host_id), "")
        _save_net_prefs(declined_hosts=dict(prefs["declined_hosts"], **{host_id: name}))
        return {"ignored": host_id}
    return _call("", run)


def host_unignore(host_id):
    """Undo Ignore: the host is asked about again the next time it is heard,
    at once if it is being heard now. Connects to nothing by itself -- the
    operator still answers Connect or Ignore."""
    def run():
        prefs = _net_prefs()
        if host_id not in prefs["declined_hosts"]:
            raise ss.Refused("That host is not ignored")
        name = prefs["declined_hosts"][host_id]
        _save_net_prefs(declined_hosts={k: v for k, v in prefs["declined_hosts"].items()
                                        if k != host_id})
        heard = next((f for f in (_net_client.found() if _net_client else [])
                      if f["host"] == host_id), None)
        if heard is not None:
            _found_host(host_id, heard.get("name") or name, heard.get("addr", ""))
        return {"unignored": host_id, "name": name, "heard": heard is not None,
                "ignored": _ignored_view()}
    return _call("", run)


def _ignored_view():
    """Hosts the operator ignored, so the screen can offer to undo it."""
    return [{"host": hid, "name": name}
            for hid, name in sorted(_net_prefs()["declined_hosts"].items())]


def host_forget(host_id):
    """Stop connecting to an approved WiFi host. Heard again, it asks again."""
    def run():
        prefs = _net_prefs()
        _save_net_prefs(approved_hosts={k: v for k, v in prefs["approved_hosts"].items()
                                        if k != host_id})
        if _net_client is not None:
            _net_client.forget_host(host_id)
        return {"forgotten": host_id, "wifi_hosts": _wifi_hosts_view()}
    return _call("", run)


def _servers_view():
    live = {}
    if _net_client is not None:
        for h in _net_client.hosts():
            if h["manual"]:
                live["%s:%d" % (h["addr"], h["port"])] = h
    out = []
    for srv in _net_prefs()["servers"]:
        h = live.get("%s:%d" % (srv["addr"], srv["port"]), {})
        out.append({"addr": srv["addr"], "port": srv["port"], "host": h.get("host") or srv.get("host"),
                    "name": h.get("name") or srv.get("name", ""),
                    "pinned": bool(srv.get("host")),
                    "first_use": bool(srv.get("first")),
                    "connected": bool(h.get("connected")), "problem": h.get("problem", "")})
    return out


def server_add(text):
    """Add a server from its code (scanned) or an address typed as
    "address" or "address:port"."""
    def run():
        import settings_net as sn
        card = sn.parse_card(text)
        servers = [s for s in _net_prefs()["servers"]
                   if (s["addr"], s["port"]) != (card["addr"], card["port"])]
        servers.append({"addr": card["addr"], "port": card["port"], "host": card["host"],
                        "name": card["name"]})
        _save_net_prefs(servers=servers)
        _start_client()
        _hold_multicast_lock(_net_client is not None or _net_server is not None)
        return {"server": card, "servers": _servers_view()}
    return _call("", run)


def server_remove(addr, port):
    def run():
        servers = [s for s in _net_prefs()["servers"]
                   if (s["addr"], int(s["port"])) != (addr, int(port))]
        _save_net_prefs(servers=servers)
        _start_client()
        _hold_multicast_lock(_net_client is not None or _net_server is not None)
        return {"servers": _servers_view()}
    return _call("", run)


def _start_server():
    global _net_server
    import settings_net as sn
    if _net_server is None:
        server = sn.HostServer(_get_host(), prepare_socket=_wifi_prepare,
                               broadcast_addrs=_broadcast_addrs, log=_log)
        server.start()
        _net_server = server


def net_start(kotlin_json=""):
    """From the service at startup: listen and/or host, as last chosen."""
    def run():
        prefs = _net_prefs()
        problems = []
        for on, start in ((prefs["listen"] or prefs["servers"], _start_client),
                          (prefs["hosting"], _start_server)):
            if on:
                try:
                    start()
                except OSError as e:
                    problems.append(str(e))
        _hold_multicast_lock(_net_client is not None or _net_server is not None)
        return {"listening": prefs["listen"] and _net_client is not None,
                "servers": len(prefs["servers"]), "hosting": _net_server is not None,
                "problems": problems}
    return _call(kotlin_json, run)


def net_set_listen(on):
    """Listen for settings hosts on WiFi, or not. Servers stay connected."""
    def run():
        want = on in (True, "true", "1", 1)
        _save_net_prefs(listen=want)
        _start_client()
        _hold_multicast_lock(_net_client is not None or _net_server is not None)
        return {"listening": want and _net_client is not None}
    return _call("", run)


def host_set(on):
    """Be a settings host on this WiFi, or stop."""
    def run():
        global _net_server
        want = on in (True, "true", "1", 1)
        if want:
            _start_server()
        elif _net_server is not None:
            _net_server.stop()
            _net_server = None
        _save_net_prefs(hosting=want)
        _hold_multicast_lock(_net_client is not None or _net_server is not None)
        return {"hosting": _net_server is not None}
    return _call("", run)


def host_push(options_json, kotlin_json=""):
    """Push this phone's settings to every phone connected: the groups ticked
    in {"lora", "coverage", "fm", "passphrase"}, switching in "switch_in_s"."""
    def run():
        if _net_server is None:
            raise ss.Refused("This phone is not hosting")
        opts = json.loads(options_json or "{}")
        cur = _current()
        settings = {}
        if opts.get("lora"):
            for k in ("lora_preset", "lora_freq_mhz", "lora_tx_dbm"):
                settings[k] = cur[k]
        if opts.get("coverage") and cur["coverage"]:
            settings["coverage"] = cur["coverage"]
        settings.update(_fm_to_share(opts, cur))
        passphrase = cur["passphrase"] if opts.get("passphrase") else None
        if not settings and not passphrase:
            raise ss.Refused("Tick at least one group of settings to push")
        switch_in_s = opts.get("switch_in_s", ss.DEFAULT_SWITCH_DELAY_S)
        rev = _net_server.push(settings, passphrase=passphrase,
                               switch_in_s=None if switch_in_s is None else int(switch_in_s))
        return {"rev": rev, "roster": _net_server.roster()}
    return _call(kotlin_json, run)


def host_roster():
    def run():
        h = _get_host()
        return {"hosting": _net_server is not None,
                "host_id": h.keys.host_id, "name": h.name,
                "port": _net_server.port if _net_server else None,
                "last_push": _net_server.last_push() if _net_server else None,
                "roster": _net_server.roster() if _net_server else []}
    return _call("", run)
