"""Tests for plrelate.py on the synthetic pldemo device folder (planted answers).

    python -m unittest test_plrelate -v        (from 03-tools)
"""
import glob
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import time
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


def build_edited(case, edits=None, **kw):
    """pldemo device in a folder of its own, optional SQL edits {db: [sql | (sql, params)]}, built graph."""
    tmp = tempfile.mkdtemp(prefix="plrelate-test-")
    case.addCleanup(shutil.rmtree, tmp, True)
    src = pldemo.build(os.path.join(tmp, "device"))
    for db, stmts in (edits or {}).items():
        con = sqlite3.connect(os.path.join(src, db))
        for st in stmts:
            con.execute(*((st,) if isinstance(st, str) else st))
        con.commit()
        con.close()
    ev = R.Evidence(src)
    case.addCleanup(ev.close)
    return ev, R.build(ev, **kw)


def linked_to(g, entity, rid):
    return {e for r, e, _ in g.links(entity) if r == rid}


def drop_column(path, table, col):
    """Remove one column from a database copy: ALTER, or rebuild the table without constraints."""
    con = sqlite3.connect(path)
    try:
        con.execute(f'ALTER TABLE "{table}" DROP COLUMN "{col}"')
    except sqlite3.OperationalError:     # unique, indexed or foreign-key columns cannot be altered away
        keep = [r for r in con.execute(f'PRAGMA table_info("{table}")') if r[1] != col]
        names = ", ".join('"%s"' % r[1] for r in keep)
        defs = ", ".join('"%s" %s' % (r[1], r[2]) for r in keep)
        con.execute(f'CREATE TABLE "_new" ({defs})')
        con.execute(f'INSERT INTO "_new" ({names}) SELECT {names} FROM "{table}"')
        con.execute(f'DROP TABLE "{table}"')
        con.execute(f'ALTER TABLE "_new" RENAME TO "{table}"')
    con.commit()
    con.close()


class SchemaTolerance(unittest.TestCase):
    """A Phone Link build that lacks one column must not stop the other views from being built."""

    def test_dropping_any_single_column_still_builds(self):
        with tempfile.TemporaryDirectory() as d:
            pristine = pldemo.build(os.path.join(d, "device"))
            ev0 = R.Evidence(pristine)
            base = {v for v in R.build(ev0).views if not v.startswith("M_")}
            ev0.close()
            cases = []
            for db in sorted(os.listdir(pristine)):
                con = sqlite3.connect(os.path.join(pristine, db))
                tables = [r[0] for r in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                for table in tables:
                    for r in con.execute(f'PRAGMA table_info("{table}")'):
                        if not r[5]:                      # not part of the primary key
                            cases.append((db, table, r[1]))
                con.close()
            self.assertGreater(len(cases), 150)
            failures = []
            for n, (db, table, col) in enumerate(cases):
                copy = os.path.join(d, f"c{n}")
                shutil.copytree(pristine, copy)
                drop_column(os.path.join(copy, db), table, col)
                try:
                    ev = R.Evidence(copy)
                    try:
                        got = {v for v in R.build(ev).views if not v.startswith("M_")}
                    finally:
                        ev.close()
                    if got != base:
                        failures.append(f"{db}.{table}.{col}: views differ {sorted(got ^ base)}")
                except Exception as e:  # noqa: BLE001 - the point is that nothing may be raised
                    failures.append(f"{db}.{table}.{col}: {type(e).__name__}: {e}")
                shutil.rmtree(copy, ignore_errors=True)
            self.assertEqual(failures, [])

    def test_missing_column_is_stated_and_the_rest_is_shown(self):
        ev, g = build_edited(self, {"phone.db": ["ALTER TABLE message DROP COLUMN body"]})
        v = g.views["E_sms"]
        self.assertEqual(len(v.rows), 3)
        self.assertEqual(v.rows[0]["Address"], pldemo.ALICE)
        self.assertIn("message.body", v.basis)
        self.assertNotIn("message.body", g.views["E_calls"].basis)

    def test_missing_notification_column_is_stated(self):
        ev, g = build_edited(self, {"notifications.db": ["ALTER TABLE notifications DROP COLUMN state"]})
        self.assertGreaterEqual(len(g.views["N_notifications"].rows), 3)   # the live rows
        self.assertIn("notifications.state", g.views["N_notifications"].basis)
        self.assertIn("notifications.state", g.views["NR3"].basis)

    def test_missing_key_column_does_not_invent_orphans(self):
        ev, g = build_edited(self, {"phone.db": ["ALTER TABLE message DROP COLUMN thread_id"]})
        self.assertFalse([r for r in g.views["R23"].rows if r["Problem"] == "message without thread"])
        self.assertIn("message.thread_id", g.views["E_sms"].basis)
        self.assertIn("message.thread_id", g.views["R23"].basis)

    def test_missing_contact_link_column(self):
        ev, g = build_edited(self, {"contacts.db": ["ALTER TABLE phonenumber DROP COLUMN contact_id"]})
        self.assertIn("phonenumber.contact_id", g.views["R13"].basis)
        self.assertEqual(len(g.views["E_calls"].rows), 5)


class Ambiguity(unittest.TestCase):
    NUM = "+1-555-0150"

    def call(self, n):
        return ("INSERT INTO call_history (call_id, phone_number, duration, call_type, start_time) VALUES "
                f"({n}, '{self.NUM}', 5, 1, {pldemo.ft(pldemo.at(900))})")

    def number(self, n, contact):
        return ("INSERT INTO phonenumber (phone_number_id, contact_id, phone_number, display_phone_number) VALUES "
                f"({n}, {contact}, '{self.NUM}', '{self.NUM}')")

    def test_two_contacts_with_the_same_name_are_ambiguous(self):
        ev, g = build_edited(self, {"contacts.db": [
            "INSERT INTO contact (contact_id, display_name) VALUES (10, 'Mum')",
            "INSERT INTO contact (contact_id, display_name) VALUES (11, 'Mum')",
            self.number(20, 10), self.number(21, 11)], "calling.db": [self.call(10)]})
        row = [r for r in g.views["R13"].rows if r["CallID"] == 10][0]
        self.assertEqual(row["Candidates"], 2)
        self.assertIn("AMBIGUOUS", row["Basis"])
        self.assertIn("2 contacts", row["Basis"])
        self.assertEqual(linked_to(g, ("call", 10), "R13"), {("contact", 10), ("contact", 11)})

    def test_one_unnamed_contact_is_one_candidate(self):
        ev, g = build_edited(self, {"contacts.db": [
            "INSERT INTO contact (contact_id, display_name) VALUES (12, NULL)", self.number(22, 12)],
            "calling.db": [self.call(10)]})
        row = [r for r in g.views["R13"].rows if r["CallID"] == 10][0]
        self.assertEqual(row["Candidates"], 1)
        self.assertNotIn("AMBIGUOUS", row["Basis"])
        self.assertEqual(row["ContactName"], "contact 12")
        party = {r["Party"] for r in g.views["R24"].rows if r["Finding"].startswith("communicated")}
        self.assertNotIn(self.NUM, party)      # it is in the contacts, just without a name


class R21Bounded(unittest.TestCase):
    def burst(self, n, gap_ms=100, minute=2000):
        base = pldemo.ms(pldemo.at(minute))
        sql = ("INSERT INTO message (message_id, thread_id, from_address, type, body, timestamp, subscription_id)"
               " VALUES (?, 1, ?, 1, 'x', ?, 1)")
        return {"phone.db": [(sql, (1000 + i, pldemo.ALICE, base + i * gap_ms)) for i in range(n)]}

    def test_big_burst_is_capped_per_event_and_counts_what_it_left_out(self):
        t0 = time.time()
        ev, g = build_edited(self, self.burst(1000))
        self.assertLess(time.time() - t0, 10)
        per = [len(linked_to(g, ("sms", 1000 + i), "R21")) for i in range(1000)]
        self.assertLessEqual(max(per), R.R21_MAX_PER_EVENT)
        self.assertGreater(min(per[100:900]), 40)
        v = g.views["R21"]
        omitted = int(re.search(r"left out by that limit: (\d+)", v.basis).group(1))
        self.assertGreater(omitted, 0)
        _, plain = build_edited(self)      # the demo's own events are far from the burst: their pairs are all kept
        self.assertEqual(len(v.rows) + omitted, 1000 * 999 // 2 + len(plain.views["R21"].rows))
        self.assertTrue({("sms", 1499), ("sms", 1501)} <= linked_to(g, ("sms", 1500), "R21"))   # nearest first

    def test_pairs_match_a_plain_double_loop_when_nothing_is_capped(self):
        import random
        rnd = random.Random(5)
        for _ in range(200):
            ev = sorted(((rnd.choice([0, 3, 50, 100, 400, 700, 1000]) + rnd.randint(0, 5) * 7,
                          (rnd.choice(["call", "sms", "photo", "photo"]), i)) for i in range(rnd.randint(0, 60))),
                        key=lambda x: x[0])
            w = rnd.choice([0, 30, 300])
            plain = [(i, j) for i in range(len(ev)) for j in range(i + 1, len(ev))
                     if ev[j][0] - ev[i][0] <= w and not (ev[i][1][0] == "photo" and ev[j][1][0] == "photo")]
            self.assertEqual(R.near_pairs(ev, w, 1000), (plain, 0))
            cap = rnd.choice([1, 2, 5])
            pairs, left = R.near_pairs(ev, w, cap)
            self.assertEqual(len(pairs) + left, len(plain))
            self.assertTrue(set(pairs) <= set(plain))
            used = [x for p in pairs for x in p]
            self.assertTrue(all(used.count(x) <= cap for x in set(used)))

    def test_window_edge_and_wording(self):
        ev, g = build_edited(self, self.burst(2, gap_ms=300000, minute=3000))
        self.assertIn(("sms", 1001), linked_to(g, ("sms", 1000), "R21"))   # exactly 5 minutes apart
        self.assertIn("at most 5 minutes apart", g.views["R21"].basis)
        self.assertIn("left out by that limit: 0", g.views["R21"].basis)


class RcsTimeline(unittest.TestCase):
    def test_rcs_chats_and_file_transfers_are_in_r22_and_r21(self):
        ev, g = build_edited(self, {"phone.db": [
            "INSERT INTO rcs_filetransfer (message_id, thread_id, from_address, timestamp) VALUES "
            f"(7, 50, '{pldemo.BOB}', {pldemo.ft(pldemo.at(12))})"]})
        kinds = {r["Kind"] for r in g.views["R22"].rows}
        self.assertTrue({"rcs_chat", "rcs_filetransfer"} <= kinds)
        t = [r["TimeUTC"] for r in g.views["R22"].rows]
        self.assertEqual(t, sorted(t))
        self.assertIn(("mms", 1), linked_to(g, ("rcs_chat", 1), "R21"))
        self.assertIn(("rcs_filetransfer", 7), linked_to(g, ("rcs_chat", 1), "R21"))
        self.assertIn(("rcs_chat", 1), g.home)


class NotificationEdgeCases(unittest.TestCase):
    def notif(self, nid, key, minute=10, **extra):
        o = json.loads(pldemo.njson("com.example.chat", "x", minute, **extra))
        o["key"] = key
        return ("INSERT INTO notifications (notification_id, package_name, json, post_time) VALUES (?, ?, ?, ?)",
                (nid, "com.example.chat", json.dumps(o), pldemo.ms(pldemo.at(minute))))

    def test_nd2_keeps_every_row_with_the_same_key(self):
        ev, g = build_edited(self, {"notifications.db": [
            ("INSERT INTO phone_requests VALUES (2, 'req-2', 'unrelated', ?, '{}', 'example-reply', 1, ?, ?, ?, ?, 2)",
             (pldemo.CHAT_KEY, pldemo.ms(pldemo.at(2)), pldemo.ms(pldemo.at(2)), pldemo.ms(pldemo.at(3)),
              pldemo.ms(pldemo.at(60))))]})
        shared = {r["Source"] for r in g.views["N_notifications"].rows if r["Key"] == pldemo.CHAT_KEY}
        self.assertEqual(shared, {"live", "deleted"})          # the planted live row and its earlier deleted version
        req = [r["_e"] for r in g.views["N_requests"].rows if r["RequestID"] == "req-2"][0]
        self.assertEqual({e[1].split(":")[0] for e in linked_to(g, req, "ND2")}, {"live", "deleted"})
        self.assertEqual(len([r for r in g.views["ND2"].rows if r["RequestID"] == "req-2"]), 2)

    def test_list_and_dict_json_values_do_not_crash(self):
        ev, g = build_edited(self, {"notifications.db": [
            self.notif("odd-1", {"a": 1}, groupKey=["g", 1], conversationTitle=["t"]),
            self.notif("odd-2", {"a": 1}, groupKey=["g", 1], conversationTitle=["t"])]})
        self.assertEqual(len([r for r in g.views["NR1"].rows if r["GroupKey"].startswith("[")]), 2)
        self.assertEqual(len([r for r in g.views["NR3"].rows if r["Key"].startswith("{")]), 2)
        self.assertEqual(len([r for r in g.views["NR2"].rows if r["Conversation"].startswith("[")]), 2)

    def test_mixed_text_and_number_event_counts_sort(self):
        stmts = [self.notif(f"ec-{i}", "0|com.example.chat|55|null|1", 10 + i % 5,
                            eventCount=(i if i % 2 else str(i)), groupKey="g:big") for i in range(52)]
        ev, g = build_edited(self, {"notifications.db": stmts})
        self.assertEqual(len([r for r in g.views["NR3"].rows if r["Key"].endswith("|55|null|1")]), 52)
        self.assertEqual(len([r for r in g.views["NR1"].rows if r["GroupKey"] == "g:big"]), 52)   # > 50: chained

    def deleted_with_long_text(self, extra_edits=()):
        long_json = pldemo.njson("com.example.chat", "0|com.example.chat|900|null|1", 20, text="A" * 6000)
        stmts = ["DELETE FROM notifications"]
        stmts += [("INSERT INTO notifications (notification_id, package_name, json, post_time) VALUES (?, ?, ?, ?)",
                   (f"cut-{i}", "com.example.chat", long_json, pldemo.ms(pldemo.at(20)))) for i in range(2)]
        stmts += [("INSERT INTO notifications (notification_id, package_name, json, post_time) VALUES (?, ?, ?, ?)",
                   (f"short-{i}", "com.example.chat",
                    pldemo.njson("com.example.chat", f"0|com.example.chat|{i}|null|1", 21, text=f"short {i}"),
                    pldemo.ms(pldemo.at(21)))) for i in range(10)]
        stmts += ["DELETE FROM notifications"]      # a full delete frees the pages whole, bytes intact
        return build_edited(self, {"notifications.db": stmts})

    def test_value_cut_at_an_overflow_boundary_is_marked(self):
        ev, g = self.deleted_with_long_text()
        rows = [r for r in g.views["N_notifications"].rows if r["Source"] == "deleted"]
        cut = [r for r in rows if "value cut" in r["CarvedFrom"]]
        self.assertTrue(cut, "no carved row was marked as cut")
        for r in cut:
            self.assertIn("value cut - rest was on overflow pages (not read)", r["CarvedFrom"])
            self.assertIn("overflow", r["JSONStatus"])
            self.assertNotIn("overwritten", r["JSONStatus"])
        whole = [r for r in rows if "value cut" not in r["CarvedFrom"]]
        self.assertTrue(whole)
        for r in whole:
            self.assertEqual(r["JSONStatus"], "parsed")

    def test_cut_request_payload_is_marked(self):
        ins = ("INSERT INTO phone_requests (request_id, request_external_id1, request_payload, request_content_type,"
               " request_status, request_state, request_time_added, request_expiration_time)"
               " VALUES (?, 'x', ?, 'example-reply', 1, 1, ?, ?)")
        t = pldemo.ms(pldemo.at(5))
        stmts = ["DELETE FROM phone_requests"]
        stmts += [(ins, (f"long-{i}", "P" * 6000, t, t)) for i in range(2)]
        stmts += [(ins, (f"short-{i}", f"short {i}", t, t)) for i in range(10)] + ["DELETE FROM phone_requests"]
        ev, g = build_edited(self, {"notifications.db": stmts})
        rows = [r for r in g.views["N_requests"].rows if r["Source"] == "deleted"]
        cut = [r for r in rows if "value cut - rest was on overflow pages (not read)" in str(r["Payload"])]
        self.assertTrue(cut)
        for r in rows:
            if r["RequestID"] and r["RequestID"].startswith("short"):
                self.assertNotIn("value cut", str(r["Payload"]))

    def test_unknown_live_status_keeps_the_carved_row(self):
        real = R.plnotif.plcarve.live_rowids
        R.plnotif.plcarve.live_rowids = lambda *a, **k: None       # the live rows could not be read
        try:
            ev, g = self.deleted_with_long_text()
        finally:
            R.plnotif.plcarve.live_rowids = real
        rows = [r for r in g.views["N_notifications"].rows if r["Source"] == "deleted"]
        self.assertTrue(rows)
        self.assertTrue(all("unreadable" in r["CarvedFrom"] for r in rows))
        recs, st = R.plnotif.plcarve.carve_db(ev.paths["notifications.db"])
        self.assertTrue(recs)

    def test_carving_errors_are_shown(self):
        real = R.plnotif.plcarve.carve_db
        R.plnotif.plcarve.carve_db = lambda *a, **k: ([], {"errors": ["table 'notifications': boom"]})
        try:
            ev, g = build_edited(self)
        finally:
            R.plnotif.plcarve.carve_db = real
        self.assertIn("boom", g.views["N_notifications"].basis)
        self.assertIn("carving", g.views["N_notifications"].basis.lower())


class OddValues(unittest.TestCase):
    def test_text_sequence_and_expected(self):
        ev, g = build_edited(self, {"calling.db": ["UPDATE content_sequence SET sequence = 'abc', expected = 'x'"],
                                    "phone.db": ["UPDATE content_sequence SET expected = 'many' WHERE content_type = 'sms'"]})
        got = {r["Feed"]: r for r in g.views["R25"].rows}
        self.assertEqual(got["call_logs"]["Sequence"], "abc")
        self.assertNotEqual(got["call_logs"]["Status"], "complete")
        self.assertEqual(got["conversation"]["Status"], "complete")

    def test_text_call_duration(self):
        ev, g = build_edited(self, {"calling.db": ["UPDATE call_history SET duration = 'about a minute' WHERE call_id = 1"]})
        alice = [r for r in g.views["R18"].rows if r["ContactName"] == "Alice Example"][0]
        self.assertEqual(alice["TalkSec"], 30)
        self.assertEqual(g.views["R20"].rows, [])       # the length of the call the photo fell in is unreadable


if __name__ == "__main__":
    unittest.main()
