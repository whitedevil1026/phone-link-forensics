"""Tests for plrelate.py on the synthetic pldemo device folder (planted answers).

    python -m unittest test_plrelate -v        (from 03-tools)
"""
import glob
import hashlib
import os
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import plrelate as R  # noqa: E402


def sha_dir(d):
    out = {}
    for f in sorted(os.listdir(d)):
        with open(os.path.join(d, f), "rb") as fh:
            out[f] = hashlib.sha256(fh.read()).hexdigest()
    return out


class Demo(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.src = pldemo.build(os.path.join(cls.tmp.name, "device"))
        cls.before = sha_dir(cls.src)
        cls.ev = R.Evidence(cls.src)
        cls.g = R.build(cls.ev)

    @classmethod
    def tearDownClass(cls):
        cls.ev.close()
        cls.tmp.cleanup()

    def view(self, vid):
        return self.g.views[vid].rows

    def linked(self, entity, rid):
        return {e for r, e, _ in self.g.links(entity) if r == rid}

    # ---- copies
    def test_source_untouched_and_copies_verified(self):
        self.assertEqual(sha_dir(self.src), self.before)
        self.assertTrue(self.ev.verify)
        self.assertTrue(all(v["copy_matches"] == "yes" for v in self.ev.verify))
        self.assertNotEqual(os.path.abspath(self.ev.work), os.path.abspath(self.src))

    def test_connections_are_read_only(self):
        with self.assertRaises(sqlite3.OperationalError):
            self.ev.con("calling.db").execute("DELETE FROM call_history")

    def test_every_relation_present(self):
        self.assertEqual(self.g.relations, [f"R{i:02d}" for i in range(1, 26) if i != 12])  # R12 -> ND1
        for rid in self.g.relations:
            self.assertTrue(self.view(rid), f"{rid} has no rows on the demo")

    # ---- within one database
    def test_r01_contact_details(self):
        tables = {r["Table"] for r in self.view("R01")}
        self.assertEqual(tables, {"phonenumber", "emailaddress"})

    def test_r02_tombstone(self):
        self.assertEqual([(r["ContactID"], r["DeletedFrom"]) for r in self.view("R02")], [(1, "phonenumber")])

    def test_r03_r04_r05_r06_thread_children(self):
        self.assertIn(("sms", 1), self.linked(("thread", 1), "R03"))
        self.assertEqual(self.linked(("thread", 2), "R04"), {("participant", 2), ("participant", 3)})
        self.assertIn(("mms", 1), self.linked(("thread", 1), "R05"))
        self.assertIn(("rcs_chat", 1), self.linked(("rcsthread", 50), "R06"))

    def test_r07_fallback(self):
        got = {r["RCSThreadID"]: r["SMSThreadFound"] for r in self.view("R07")}
        self.assertEqual(got, {50: "yes", 51: "no"})

    def test_r08_r09_r10(self):
        self.assertEqual(self.view("R08")[0]["PinPosition"], 1)
        self.assertIn(("sms", 1), self.linked(("thread", 1), "R09"))
        self.assertEqual({r["MessageID"]: r["SIMFound"] for r in self.view("R10")}, {1: "yes", 2: "yes", 3: "no"})

    def test_r11_apps(self):
        self.assertTrue(all(r["InstalledAppFound"] == "yes" for r in self.view("R11")))

    # ---- across databases
    def test_r13_calls_to_contacts_with_ambiguity(self):
        got = {r["CallID"]: r for r in self.view("R13")}
        self.assertEqual(got[1]["ContactName"], "Alice Example")
        self.assertEqual(got[3]["ContactName"], "Bob Example")
        self.assertEqual(got[4]["Candidates"], 0)
        self.assertEqual(got[5]["Candidates"], 2)
        self.assertIn("AMBIGUOUS", got[5]["Basis"])
        self.assertEqual(self.linked(("call", 5), "R13"), {("contact", 3), ("contact", 4)})

    def test_r14_call_sim(self):
        self.assertEqual({r["CallID"]: r["SIMFound"] for r in self.view("R14")},
                         {1: "yes", 2: "yes", 3: "yes", 4: "no", 5: "yes"})

    def test_r15_r16_r17_messages_threads_participants(self):
        self.assertEqual({r["MessageID"]: r["ContactName"] for r in self.view("R15")}[1], "Alice Example")
        members = [(r["ThreadID"], r["ContactName"]) for r in self.view("R16")]
        self.assertIn((2, "Alice Example"), members)
        self.assertIn((2, "Bob Example"), members)
        self.assertIn((3, ""), members)  # alphanumeric sender: no contact
        self.assertEqual({r["ParticipantID"]: r["ContactName"] for r in self.view("R17")}[3], "Bob Example")

    def test_r18_party_profile(self):
        p = {r["ContactName"] or r["Party"]: r for r in self.view("R18")}
        self.assertEqual((p["Alice Example"]["Calls"], p["Alice Example"]["SMS"]), (2, 1))
        self.assertEqual(p["Alice Example"]["TalkSec"], 630)
        self.assertEqual(p["Alice Example"]["BothChannels"], "yes")
        self.assertEqual(p["Carol Example | Dave Example"]["Calls"], 1)

    def test_r19_package_links(self):
        got = {(r["Source"], r["Package"]): r["InstalledAppFound"] for r in self.view("R19")}
        self.assertEqual(got[("notification", "com.example.chat")], "yes")
        self.assertEqual(got[("notification", "com.example.music")], "no")  # deleted row, app not installed
        self.assertEqual(got[("app setting", "com.example.chat")], "yes")
        self.assertEqual(got[("browser page", "com.example.browser")], "no")
        self.assertEqual(got[("handoff", "com.example.chat")], "yes")

    # ---- time
    def test_r20_photo_during_call(self):
        self.assertEqual([(r["MediaID"], r["CallID"]) for r in self.view("R20")], [(1, 1)])

    def test_r21_near_events(self):
        self.assertIn(("call", 3), self.linked(("photo", 2), "R21"))
        self.assertNotIn(("photo", 3), {e for rid, e, _ in self.g.links(("call", 1))})

    def test_r22_timeline_sorted_and_complete(self):
        t = [r["TimeUTC"] for r in self.view("R22")]
        self.assertEqual(t, sorted(t))
        kinds = {r["Kind"] for r in self.view("R22")}
        self.assertTrue({"call", "sms", "mms", "photo", "notif", "handoff"} <= kinds)

    # ---- integrity
    def test_r23_orphans(self):
        got = {(r["Problem"], r["ID"]) for r in self.view("R23")}
        self.assertEqual(got, {("participant without thread", 4), ("message without thread", 3),
                               ("contact detail without contact", 6), ("RCS thread without SMS fallback thread", 51)})

    def test_r24_unknown_and_dormant(self):
        got = {(r["Finding"], r["Contact"] or r["Party"]) for r in self.view("R24")}
        self.assertIn(("contact with no call or SMS on this PC", "Frank Example"), got)
        self.assertIn(("communicated with, not in contacts", "+1-555-0177"), got)
        self.assertNotIn(("contact with no call or SMS on this PC", "Erin Example"), got)  # no number at all

    def test_r25_sync_completeness(self):
        got = {r["Feed"]: r for r in self.view("R25")}
        self.assertTrue(got["sms"]["Status"].startswith("INCOMPLETE"))
        self.assertEqual(got["call_logs"]["Status"], "complete")
        self.assertEqual(got["sms"]["RowsPresent"], 3)

    # ---- navigation
    def test_links_from_one_call(self):
        rids = {rid for rid, _, _ in self.g.links(("call", 1))}
        self.assertTrue({"R13", "R14", "R18", "R20"} <= rids)
        for _, target, _ in self.g.links(("call", 1)):
            self.assertIn(target, self.g.home, f"{target} has no home view to jump to")

    def test_map_output_runs_without_errors(self):
        maps = [v for v in self.g.views.values() if v.kind == "map"]
        self.assertGreaterEqual(len(maps), 12)
        self.assertFalse([v.vid for v in maps if "ERROR" in v.basis])


class SmapReader(unittest.TestCase):
    def test_reader_matches_yaml_on_every_map(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("PyYAML not installed in this Python")
        files = [f for d in R.DEFAULT_MAP_DIRS for f in glob.glob(os.path.join(d, "*.smap"))]
        self.assertGreaterEqual(len(files), 9)
        for f in files:
            with open(f, encoding="utf-8") as fh:
                ref = yaml.safe_load(fh)
            mine = R.read_smap(f)
            self.assertEqual(mine["IdentifyQuery"], ref["IdentifyQuery"], f)
            self.assertEqual(str(mine["IdentifyValue"]), str(ref["IdentifyValue"]), f)
            self.assertEqual(len(mine["Queries"]), len(ref["Queries"]), f)
            for a, b in zip(mine["Queries"], ref["Queries"]):
                self.assertEqual(a["Query"].strip(), b["Query"].strip(), f)
                self.assertEqual(a["BaseFileName"], b["BaseFileName"], f)
                self.assertEqual([x["BlobColumn"] for x in a.get("BlobColumns", [])],
                                 [x["BlobColumn"] for x in b.get("BlobColumns") or []], f)


class CopyIntegrity(unittest.TestCase):
    def test_mismatching_copy_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            src = pldemo.build(os.path.join(d, "device"))
            real = R.P.sha256
            calls = {"n": 0}

            def lying_sha(path):  # source hashes normally, every copy hashes wrong
                return real(path) if os.path.dirname(os.path.abspath(path)) == os.path.abspath(src) else "0" * 64
            R.P.sha256 = lying_sha
            try:
                with self.assertRaises(IOError):
                    R.Evidence(src)
            finally:
                R.P.sha256 = real


class Notifications(unittest.TestCase):
    """notifications.db: live + carved rows, JSON unpacked, relations inside the table and the database."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.ev = R.Evidence(pldemo.build(os.path.join(cls.tmp.name, "device")))
        cls.g = R.build(cls.ev)
        cls.rows = cls.g.views["N_notifications"].rows

    @classmethod
    def tearDownClass(cls):
        cls.ev.close()
        cls.tmp.cleanup()

    def one(self, source, nid):
        hits = [r for r in self.rows if r["Source"] == source and r["NotificationID"] == nid]
        self.assertEqual(len(hits), 1, (source, nid))
        return hits[0]

    def test_live_and_carved_rows(self):
        got = sorted((r["Source"], r["NotificationID"]) for r in self.rows)
        self.assertEqual(got, [("deleted", "n-1-v1"), ("deleted", "n-2"), ("deleted", "n-music"),
                               ("live", "n-1"), ("live", "n-2"), ("live", "n-mismatch")])
        for r in self.rows:
            self.assertEqual(r["JSONStatus"], "parsed")
            self.assertEqual(bool(r["CarvedFrom"]), r["Source"] == "deleted")

    def test_json_unpacked_and_decoded(self):
        r = self.one("live", "n-1")
        self.assertEqual((r["Messages"], r["Senders"]), (2, "Alice Example | Bob Example"))
        self.assertEqual((r["Actions"], r["InlineReply"]), ("Reply | Mark as read", "yes"))
        self.assertEqual((r["Importance"], r["Priority"]), ("4 (HIGH)", "1 (HIGH)"))
        self.assertIn("AUTO_CANCEL", r["Flags"])
        self.assertIn("ONLY_ALERT_ONCE", r["Flags"])
        self.assertEqual((r["KeyUser"], r["KeyPackage"], r["KeyID"], r["KeyTag"], r["KeyUID"]),
                         ("0", "com.example.chat", "101", "", "10001"))
        self.assertEqual((r["ConversationTitle"], r["Category"], r["Template"]),
                         ("Example Family", "msg", "ExampleStyle"))
        self.assertIn("GROUP_SUMMARY", self.one("live", "n-2")["Flags"])
        self.assertEqual(self.one("live", "n-2")["IsGroup"], "yes")
        self.assertEqual(self.one("deleted", "n-music")["IsOngoing"], "yes")

    def test_nr1_group(self):
        members = {(r["Source"], r["Role"]) for r in self.g.views["NR1"].rows if r["GroupKey"] == "g:chat"}
        self.assertEqual(len([r for r in self.g.views["NR1"].rows if r["GroupKey"] == "g:chat"]), 4)
        self.assertIn(("live", "summary"), members)
        self.assertEqual({r["GroupKey"] for r in self.g.views["NR1"].rows}, {"g:chat"})

    def test_nr2_conversation_and_nr3_versions(self):
        conv = sorted((r["Source"], r["Messages"]) for r in self.g.views["NR2"].rows)
        self.assertEqual(conv, [("deleted", 1), ("live", 2)])
        chat = [r for r in self.g.views["NR3"].rows if "|101|" in r["Key"]]
        self.assertEqual([(r["Version"], r["Source"], r["EventCount"]) for r in chat],
                         [(1, "deleted", 1), (2, "live", 2)])

    def test_nr4_only_the_planted_mismatch_disagrees(self):
        bad = {r["Row"] for r in self.g.views["NR4"].rows if r["Result"] != "agrees"}
        self.assertEqual(len(bad), 1)
        self.assertIn("Example Other", bad.pop())

    def test_nr5_nr6_inside_one_row(self):
        e = self.one("live", "n-1")["_e"]
        rids = [rid for rid, _, _ in self.g.links(e)]
        self.assertEqual(rids.count("NR5"), 2)
        self.assertEqual(rids.count("NR6"), 2)

    def test_nd1_nd2_nd3_between_tables(self):
        e = self.one("live", "n-1")["_e"]
        kinds = {t[0] for rid, t, _ in self.g.links(e) if rid in ("ND1", "ND2")}
        self.assertEqual(kinds, {"toast", "request"})
        dead = self.one("deleted", "n-2")["_e"]
        self.assertIn(self.one("live", "n-2")["_e"], {t for rid, t, _ in self.g.links(dead) if rid == "ND3"})

    def test_views_belong_to_one_database(self):
        for vid, v in self.g.views.items():
            if v.kind in ("table", "in-table", "in-db"):
                self.assertIn(v.db, R.P.DB_NAMES, vid)
        self.assertEqual({v.db for v in self.g.views.values() if v.vid.startswith(("N_", "NR", "ND"))},
                         {"notifications.db"})

if __name__ == "__main__":
    unittest.main()
