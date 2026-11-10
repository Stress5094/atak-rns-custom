"""Desktop tests for mesh_view.py -- what each mark on the Mesh tab may claim."""

import os
import re
import unittest

import mesh_view
from mesh_view import GOOD, WEAK, GONE

CONNECTED_S = 180.0
INTERVAL_S = 60.0
QUIET_S = INTERVAL_S * mesh_view.WEAK_AFTER_INTERVALS

ME = {"node_id": "aaaaaa", "name": "Me", "callsign": "", "radios": ["rnode", "fm"]}


def radio_row(radio, age=5.0, direct_age=5.0, snr=None, rssi=None,
              heard=3, lost=0, link=None):
    return {"radio": radio, "link": link or radio, "age_s": age,
            "direct_age_s": direct_age, "rssi": rssi, "snr": snr,
            "heard": heard, "lost": lost}


def peer(node_id, radios, name="", connected=True, direct=True, hops=1,
         age=5.0):
    return {"node_id": node_id, "name": name, "callsign": "", "age_s": age,
            "connected": connected, "direct": direct, "hops_away": hops,
            "radios": radios}


def grade(age, snr=None, kind="rnode"):
    return mesh_view.grade_line(age, snr, kind, CONNECTED_S, INTERVAL_S)


def build(peers, **kw):
    return mesh_view.build(ME, peers, CONNECTED_S, INTERVAL_S, **kw)


class TestGrades(unittest.TestCase):

    def test_recent_and_strong_is_good(self):
        self.assertEqual(grade(5, snr=12)[0], GOOD)

    def test_gone_only_past_the_connected_window(self):
        self.assertEqual(grade(CONNECTED_S)[0], WEAK)
        self.assertEqual(grade(CONNECTED_S + 0.1)[0], GONE)

    def test_never_heard_directly_is_gone(self):
        self.assertEqual(grade(None)[0], GONE)

    def test_quiet_past_one_and_a_half_intervals_is_weak(self):
        self.assertEqual(grade(QUIET_S, snr=12)[0], GOOD)
        g, reason = grade(QUIET_S + 0.1, snr=12)
        self.assertEqual(g, WEAK)
        self.assertIn("going quiet", reason)

    def test_signal_thresholds(self):
        # The threshold itself is the worse side, as in Kotlin's levelForSignal.
        self.assertEqual(grade(5, snr=mesh_view.SNR_WARN_DB + 0.1)[0], GOOD)
        self.assertEqual(grade(5, snr=mesh_view.SNR_WARN_DB)[0], WEAK)
        g, reason = grade(5, snr=mesh_view.SNR_BAD_DB)
        self.assertEqual(g, WEAK)
        self.assertIn("very weak", reason)
        self.assertNotIn("very weak", grade(5, snr=mesh_view.SNR_BAD_DB + 0.1)[1])

    def test_the_desk_meshtastic_link_reads_good(self):
        # The 2026-10-13 desk run: SNR ~6. The old 10 dB threshold called it weak.
        self.assertEqual(grade(5, snr=6.0, kind="meshtastic")[0], GOOD)

    def test_signal_is_judged_on_meshtastic_too(self):
        self.assertEqual(grade(5, snr=-3.0, kind="meshtastic")[0], WEAK)

    def test_fm_is_judged_on_age_and_says_so(self):
        g, reason = grade(5, kind="fm")
        self.assertEqual(g, GOOD)
        self.assertIn("age only", reason)

    def test_fm_ignores_a_stray_snr(self):
        # FM has no signal reading; a number that somehow arrives must not
        # quietly start grading FM lines against a LoRa threshold.
        self.assertEqual(grade(5, snr=-20.0, kind="fm")[0], GOOD)

    def test_a_missing_snr_on_lora_grades_on_age(self):
        self.assertEqual(grade(5, snr=None)[0], GOOD)


class TestLines(unittest.TestCase):

    def test_one_line_per_radio(self):
        view = build([peer("b", [radio_row("rnode", snr=12),
                                 radio_row("fm", age=100, direct_age=100)])])
        self.assertEqual([(l["radio"], l["grade"]) for l in view["lines"]],
                         [("rnode", GOOD), ("fm", WEAK)])

    def test_a_relayed_line_is_via_mesh_and_measures_nothing(self):
        view = build([peer("b", [radio_row("rnode", age=5, direct_age=None,
                                           snr=20.0, rssi=-40)],
                           direct=False, hops=2)])
        line = view["lines"][0]
        self.assertFalse(line["direct"])
        self.assertIsNone(line["snr"])
        self.assertIsNone(line["rssi"])
        self.assertTrue(line["reason"].startswith("via mesh"))
        self.assertEqual(line["age_s"], 5)

    def test_a_direct_line_ages_on_its_last_direct_frame(self):
        # Relayed copies keep arriving; the first-hand link has gone quiet.
        view = build([peer("b", [radio_row("rnode", age=1, direct_age=200)])])
        self.assertEqual(view["lines"][0]["grade"], GONE)

    def test_unknown_loss_passes_through_as_unknown(self):
        view = build([peer("b", [radio_row("fm", lost=None)])])
        self.assertIsNone(view["lines"][0]["lost"])


class TestNodes(unittest.TestCase):

    def test_a_direct_node_is_on_the_inner_ring(self):
        self.assertEqual(build([peer("b", [radio_row("fm")])])["nodes"][0]["ring"], 1)

    def test_a_relayed_node_is_on_the_outer_ring(self):
        view = build([peer("b", [radio_row("rnode", direct_age=None)],
                           direct=False, hops=2)])
        self.assertEqual(view["nodes"][0]["ring"], 2)

    def test_a_silent_former_neighbour_stays_on_the_inner_ring(self):
        view = build([peer("b", [radio_row("fm", age=300, direct_age=300)],
                           connected=False, direct=False, hops=1, age=300)])
        node = view["nodes"][0]
        self.assertEqual(node["ring"], 1)
        self.assertEqual(node["grade"], GONE)

    def test_a_node_is_as_healthy_as_its_best_line(self):
        view = build([peer("b", [radio_row("rnode", snr=-9.0),
                                 radio_row("fm")])])
        self.assertEqual(view["nodes"][0]["grade"], GOOD)

    def test_label_falls_back_to_the_id(self):
        view = build([peer("b", [], name="")])
        self.assertEqual(view["nodes"][0]["label"], "b")

    def test_a_node_with_no_radio_rows_is_graded_by_connection(self):
        self.assertEqual(build([peer("b", [])])["nodes"][0]["grade"], GOOD)
        self.assertEqual(build([peer("b", [], connected=False)])["nodes"][0]["grade"], GONE)


class TestProbableLines(unittest.TestCase):

    def test_pairs_are_deduplicated_regardless_of_direction(self):
        view = build([peer("b", []), peer("c", [])],
                     probable=[("b", "c"), ("c", "b")])
        self.assertEqual(view["probable"], [{"a": "b", "b": "c"}])

    def test_pairs_with_a_node_not_on_the_picture_are_dropped(self):
        view = build([peer("b", [])], probable=[("b", "zzz"), ("b", "b")])
        self.assertEqual(view["probable"], [])


class TestKotlinAgrees(unittest.TestCase):
    """The node list and the Mesh tab grade the same signal the same way."""

    KT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "java",
                      "io", "github", "stress5094", "customrnsbridge",
                      "MainActivity.kt")

    def constant(self, name):
        with open(self.KT) as f:
            m = re.search(r"const val %s = ([-0-9.]+)" % name, f.read())
        self.assertIsNotNone(m, name)
        return float(m.group(1))

    def test_the_diagram_has_a_style_for_every_kind(self):
        # Each kind needs a colour, a pattern and a tag in MeshDiagramView, or
        # it is drawn in the black fallback with its raw name as the tag. Both
        # drift directions: a kind Kotlin styles and Python never sends is
        # dead code hiding a rename.
        path = os.path.join(os.path.dirname(self.KT), "MeshDiagramView.kt")
        with open(path) as f:
            src = f.read()
        python = {k for k, _, _ in mesh_view.KINDS}
        for fn in ("colourFor", "patternFor", "tagFor"):
            body = src[src.index("private fun %s" % fn):]
            body = body[:body.index("else ->")]
            self.assertEqual(set(re.findall(r'"(\w+)" ->', body)), python, fn)
        # The tags are drawn from Kotlin's copy; they must say what Python's do.
        tag_body = src[src.index("private fun tagFor"):]
        for kind, _, tag in mesh_view.KINDS:
            self.assertIn('"%s" -> "%s"' % (kind, tag), tag_body)

    def test_thresholds_match_the_node_list(self):
        self.assertEqual(self.constant("SNR_WARN"), mesh_view.SNR_WARN_DB)
        self.assertEqual(self.constant("SNR_BAD"), mesh_view.SNR_BAD_DB)


class TestKinds(unittest.TestCase):

    def test_kinds_are_unique_and_cover_every_radio_the_bridge_stamps(self):
        kinds = [k for k, _, _ in mesh_view.KINDS]
        self.assertEqual(len(kinds), len(set(kinds)))
        self.assertEqual(set(kinds), {"rnode", "meshtastic", "fm", "wifi"})

    def test_the_legend_lists_only_the_radios_in_use(self):
        # This phone has RNode and FM: no Meshtastic or WiFi entry.
        self.assertEqual([k["kind"] for k in build([])["kinds"]], ["rnode", "fm"])

    def test_a_peer_heard_on_another_radio_adds_it_to_the_legend(self):
        view = build([peer("bbbbbb", [radio_row("wifi")])])
        self.assertEqual([k["kind"] for k in view["kinds"]], ["rnode", "fm", "wifi"])


if __name__ == "__main__":
    unittest.main()
