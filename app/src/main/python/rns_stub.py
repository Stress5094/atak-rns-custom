"""A minimal stand-in for RNS and pyserial, so the desktop suite can import the
modules that need them.

WHY THIS IS SHARED RATHER THAN COPIED, which is the whole reason it is a module
and not six lines at the top of a test. Two test modules used to carry their own
copy of this stub -- test_rns_bridge_wiring.py and the old test_fm_interface.py
-- and whichever one unittest discovery imported first installed the copy the
other then got. They drifted, and the symptom was a test that passed or failed
depending on alphabetical order of filenames.

NOT NAMED test_*.py on purpose: discovery would import it as a test module,
which works but means a helper shows up as an empty test case. It ships in the
APK alongside the test modules, which is 2 kB and the price of having one copy.

DELIBERATELY TINY. The suite that uses this is checking that the bridge's links,
gates, budgets and policies are built in the right order and agree with each
other -- not that RNS works. Anything that needs real Reticulum behaviour is the
wrong thing to test here.
"""

import sys
import types


def install():
    """Install the stubs, unless the real modules are already importable.

    Idempotent, and safe to call from several test modules: whichever runs
    first installs, the rest see `"RNS" in sys.modules` and leave it alone --
    which is exactly the sharing this module exists to provide, since they all
    now get the SAME stub rather than one of several copies.
    """
    if "RNS" not in sys.modules:
        rns = types.ModuleType("RNS")
        for name in ("LOG_ERROR", "LOG_DEBUG", "LOG_INFO", "LOG_WARNING",
                     "LOG_NOTICE", "LOG_CRITICAL", "LOG_EXTREME", "LOG_VERBOSE"):
            setattr(rns, name, 0)
        rns.log = lambda *a, **k: None
        for name in ("Reticulum", "Identity", "Destination", "Transport"):
            setattr(rns, name, type(name, (), {"__init__": lambda s, *a, **k: None}))

        # RNS.Interfaces has to be a real package here, not just an attribute,
        # because a from-scratch Interface subclass does `from
        # RNS.Interfaces.Interface import Interface` -- an import statement
        # resolves through sys.modules, which a bare attribute does not
        # satisfy. fm_link.py is such a subclass.
        class _StubInterface(object):
            def __init__(self):
                self.rxb = 0
                self.txb = 0
                self.online = False

        interface_mod = types.ModuleType("RNS.Interfaces.Interface")
        interface_mod.Interface = _StubInterface
        interfaces_pkg = types.ModuleType("RNS.Interfaces")
        interfaces_pkg.Interface = interface_mod
        rns.Interfaces = interfaces_pkg
        sys.modules["RNS.Interfaces"] = interfaces_pkg
        sys.modules["RNS.Interfaces.Interface"] = interface_mod

        rns.Destination.IN = rns.Destination.OUT = 0
        rns.Destination.SINGLE = rns.Destination.PLAIN = rns.Destination.GROUP = 0
        rns.Packet = type("Packet", (), {
            "PLAIN_MDU": 464,
            "__init__": lambda s, *a, **k: None,
            "send": lambda s: True})
        sys.modules["RNS"] = rns
    if "serial" not in sys.modules:
        # THE REAL pyserial WINS WHEN IT IS INSTALLED. The stub exists only so
        # rns_bridge can be imported on a desktop that has no pyserial. The
        # real `meshtastic` package (test_mt_port) imports serial.tools.list_ports,
        # which a bare stub module cannot provide ("'serial' is not a package"),
        # and which a stub left in sys.modules by whichever test module was
        # imported first would break for everyone after it.
        try:
            import serial  # noqa: F401
        except ImportError:
            ser = types.ModuleType("serial")
            ser.serial_for_url = lambda *a, **k: None
            ser.Serial = type("Serial", (), {})
            sys.modules["serial"] = ser
