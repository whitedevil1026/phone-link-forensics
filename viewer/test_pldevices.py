"""Tests for pldevices.py - every phone ever linked to the PC. Synthetic package only.

    python test_pldevices.py
"""
import calendar
import hashlib
import json
import os
import sqlite3
import stat
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import pldevices as D  # noqa: E402
import plrelate as R  # noqa: E402


def tree_hashes(root):
    out = {}
    for dirpath, _, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                out[os.path.relpath(p, root)] = hashlib.sha256(fh.read()).hexdigest()
    return out


G = ["aaaaaaaa-0000-4000-8000-00000000000%d" % i for i in range(1, 8)]


def make_pkg(root, meta=None, trust=None, folders=()):
    """A tiny synthetic LocalCache: folders = [(guid, {relative file: bytes or [(ddl, row sql), ...]})]."""
    lc = os.path.join(root, "pkg", "LocalCache")
    os.makedirs(os.path.join(lc, "Indexed"), exist_ok=True)
    for guid, files in folders:
        d = os.path.join(lc, "Indexed", guid)
        os.makedirs(d, exist_ok=True)
        for rel, content in files.items():
            p = os.path.join(d, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            if isinstance(content, bytes):
                with open(p, "wb") as f:
                    f.write(content)
            else:
                con = sqlite3.connect(p)
                for ddl, rows in content:
                    con.execute(ddl)
                    for r in rows:
                        con.execute(r)
                con.commit()
                con.close()
    for name, obj in ((D.META, meta), (D.TRUST, trust)):
        if obj is not None:
            with open(os.path.join(lc, name), "w", encoding="utf-8") as f:
                f.write(obj if isinstance(obj, str) else json.dumps(obj))
    return lc


class WorkFolders:
    """Point tempfile at a private folder so the test can count the pldevices-* folders it leaves."""

    def __enter__(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old, tempfile.tempdir = tempfile.tempdir, self.tmp.name
        return self

    def left(self):
        return [n for n in os.listdir(self.tmp.name) if n.startswith("pldevices-")]

    def __exit__(self, *a):
        tempfile.tempdir = self.old
        self.tmp.cleanup()


class IsoTimes(unittest.TestCase):
    def test_seven_digit_fraction_with_offset(self):
        text, ts = D._iso("2026-09-21T17:17:49.1234567+05:30")
        self.assertEqual(text, "2026-09-21 11:47:49")
        self.assertAlmostEqual(ts, calendar.timegm((2026, 9, 21, 11, 47, 49)) + 0.123456, places=5)

    def test_current_format_without_fraction_and_z(self):
        want = calendar.timegm((2026, 9, 21, 11, 47, 49))
        self.assertEqual(D._iso("2026-09-21T17:17:49+05:30"), ("2026-09-21 11:47:49", want))
        self.assertEqual(D._iso("2026-09-21T11:47:49Z"), ("2026-09-21 11:47:49", want))
        self.assertEqual(D._iso("2026-09-21T11:47:49.5+00:00")[0], "2026-09-21 11:47:49")

    def test_no_offset_is_never_presented_as_utc(self):
        self.assertEqual(D._iso("2026-09-21T17:17:49"), ("2026-09-21 17:17:49 (no zone)", None))
        self.assertEqual(D._iso("2026-09-21T17:17:49.1234567"), ("2026-09-21 17:17:49 (no zone)", None))

    def test_junk_and_empty(self):
        self.assertEqual(D._iso(""), ("", None))
        self.assertEqual(D._iso("not a time"), ("not a time", None))


class PairingStatus(unittest.TestCase):
    def test_each_state_is_told_apart(self):
        msg = [("CREATE TABLE message (id INTEGER, timestamp INTEGER)", [])]
        other = [("CREATE TABLE settings (k TEXT)", ["INSERT INTO settings VALUES ('x')"])]
        folders = [(G[0], {}),
                   (G[1], {"notes.txt": b"hello"}),
                   (G[2], {"System/Database/phone.db": b"this is not a database at all, just bytes" * 4}),
                   (G[3], {"System/Database/settings.db": other}),
                   (G[4], {"System/Database/phone.db": msg}),
                   (G[5], {"System/Database/phone.db": msg + [("CREATE TABLE conversation (id INTEGER)",
                                                               ["INSERT INTO conversation VALUES (1)"])]})]
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=folders)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        got = {r["DeviceFolder"]: r["Status"] for r in g.views["PC_devices"].rows}
        self.assertEqual(got[G[0]], "folder only - its data was removed")
        self.assertEqual(got[G[1]], "files present but no databases")
        self.assertEqual(got[G[2]], "unreadable database")
        self.assertEqual(got[G[3]], "data in other tables")
        self.assertEqual(got[G[4]], "databases present but empty")
        self.assertEqual(got[G[5]], "data present")


class BadSources(unittest.TestCase):
    META = {"DeviceMetadatas": {"HW1": [{"IsLinked": True, "Metadata": {"Id": "id-1", "Metadata": {"DisplayName": "P1"}}}]}}
    TRUST = {"TrustedClients": {"id-1": {"PartnerClientId": "pre_id-1", "TrustType": 3}}}
    OK_FOLDER = (G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER, timestamp INTEGER)",
                                                      ["INSERT INTO message VALUES (1, 5)"])]})

    def build(self, **kw):
        with WorkFolders(), tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, folders=kw.pop("folders", [self.OK_FOLDER]), **kw)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
            return g

    def assert_error_row(self, g, vid):
        self.assertTrue(any(r.get("Error") or "could not read" in str(r.get("Note", "")) for r in g.views[vid].rows),
                        g.views[vid].rows)

    def test_corrupt_fulltrust_db(self):
        g = self.build(meta=self.META, trust=self.TRUST,
                       folders=[self.OK_FOLDER, ("FullTrust", {"phone_apps.db": b"garbage" * 30})])
        self.assert_error_row(g, "PC_streamed")
        self.assertEqual(len(g.views["PC_devices"].rows), 1)
        self.assertEqual(len(g.views["PC_phones"].rows), 1)
        self.assertEqual(len(g.views["PC_trust"].rows), 1)

    def test_device_metadatas_list_shape(self):
        g = self.build(meta={"DeviceMetadatas": [{"HW1": []}]}, trust=self.TRUST)
        self.assert_error_row(g, "PC_phones")
        self.assertEqual(len(g.views["PC_trust"].rows), 1)
        self.assertEqual(len(g.views["PC_devices"].rows), 1)

    def test_one_odd_entry_does_not_hide_the_others(self):
        meta = {"DeviceMetadatas": {"BAD": {"not": "a list"}, **self.META["DeviceMetadatas"]}}
        g = self.build(meta=meta, trust=self.TRUST)
        notes = {r["HardwareID"]: r for r in g.views["PC_phones"].rows}
        self.assertIn("could not read", notes["BAD"]["Note"])
        self.assertEqual(notes["HW1"]["DisplayName"], "P1")

    def test_truncated_json(self):
        full = json.dumps(self.META)
        g = self.build(meta=full[:len(full) // 2], trust=json.dumps(self.TRUST)[:20])
        self.assert_error_row(g, "PC_phones")
        self.assert_error_row(g, "PC_trust")
        self.assertEqual(len(g.views["PC_devices"].rows), 1)
        self.assertIn("PC_copies", g.views)

    def test_package_copy_closed_when_build_fails(self):
        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, folders=[self.OK_FOLDER])
            with mock.patch.object(D, "_db_summary", side_effect=RuntimeError("boom")):
                with self.assertRaises(RuntimeError):
                    D.build(lc, R.Graph(), R.View, R.ts_seconds, R.iso)
            self.assertEqual(w.left(), [])

    def test_package_copy_init_removes_work_folder_when_copy_fails(self):
        with WorkFolders() as w, tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=self.META, trust=self.TRUST, folders=[self.OK_FOLDER])
            real = D.shutil.copy2
            calls = []

            def flaky(a, b, *x, **k):
                calls.append(a)
                if len(calls) == 3:
                    raise OSError("disk full")
                return real(a, b, *x, **k)

            with mock.patch.object(D.shutil, "copy2", flaky):
                with self.assertRaises(OSError):
                    D.PackageCopy(lc)
            self.assertGreaterEqual(len(calls), 3)
            self.assertEqual(w.left(), [])


class Links(unittest.TestCase):
    def link(self, target, link):
        try:
            import _winapi
            _winapi.CreateJunction(target, link)
            return
        except (ImportError, AttributeError, OSError):
            pass
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError, AttributeError):
            self.skipTest("neither a junction nor a symlink can be made here")

    def test_junctions_and_symlinks_are_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = os.path.join(tmp, "outside")
            os.makedirs(outside)
            with open(os.path.join(outside, "secret.db"), "wb") as f:
                f.write(b"outside the package")
            ok = (G[0], {"System/Database/phone.db": [("CREATE TABLE message (id INTEGER)", [])]})
            lc = make_pkg(tmp, folders=[ok])
            self.link(outside, os.path.join(lc, "Indexed", G[0], "linked"))     # a link inside a pairing folder
            self.link(outside, os.path.join(lc, "Indexed", G[1]))              # a pairing folder that is a link
            with WorkFolders():
                g = R.Graph()
                _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
                try:
                    copied = [os.path.join(r, f) for r, _, fs in os.walk(pc.work) for f in fs]
                    self.assertFalse([c for c in copied if "secret" in c], copied)
                    self.assertEqual([r["DeviceFolder"] for r in g.views["PC_devices"].rows], [G[0]])
                    report = " | ".join(f"{r['file']} {r['copy_matches']}" for r in g.views["PC_copies"].rows)
                    self.assertIn("linked", report)
                    self.assertIn(G[1], report)
                    self.assertIn("not followed", report)
                finally:
                    pc.close()
            self.assertTrue(os.path.isfile(os.path.join(outside, "secret.db")))   # the target is untouched


class TrustMatch(unittest.TestCase):
    def test_partner_id_must_end_with_underscore_and_id(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {"Id": "abc", "Metadata": {"DisplayName": "P1"}}}],
                                    "HW2": [{"Metadata": {"Id": "a_b_c", "Metadata": {"DisplayName": "P2"}}}]}}
        trust = {"TrustedClients": {"t1": {"PartnerClientId": "pre_a_b_c"},      # id contains '_'
                                    "t2": {"PartnerClientId": "pre_xabc"}}}      # only ends with the same letters
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta, trust=trust)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        self.assertEqual([t for rid, t, _ in g.links(("phone", "a_b_c")) if rid == "PR1"], [("trust", "t1")])
        self.assertEqual([t for rid, t, _ in g.links(("phone", "abc")) if rid == "PR1"], [])


class LastSeenInHistory(unittest.TestCase):
    def test_last_seen_event_is_in_the_history_with_its_utc_time(self):
        meta = {"DeviceMetadatas": {"HW1": [{"Metadata": {
            "Id": "id-1", "RegistrationTime": "2026-09-21T17:00:00+05:30",
            "LastSeenTime": "2026-09-21T17:17:49+05:30", "Metadata": {"DisplayName": "P1"}}}]}}
        with tempfile.TemporaryDirectory() as tmp:
            lc = make_pkg(tmp, meta=meta)
            g = R.Graph()
            _, pc = D.build(lc, g, R.View, R.ts_seconds, R.iso)
            pc.close()
        self.assertEqual(g.views["PC_phones"].rows[0]["LastSeenUTC"], "2026-09-21 11:47:49")
        seen = [r["TimeUTC"] for r in g.views["PR4"].rows if r["Event"] == "phone last seen (metadata)"]
        self.assertEqual(seen, ["2026-09-21 11:47:49"])


class LinkDetection(unittest.TestCase):
    def test_reparse_point_attribute_is_a_link_even_without_isjunction(self):
        # Python 3.8-3.11 has no os.path.isjunction and lstat does not flag junctions: the attribute must
        fake = types.SimpleNamespace(st_mode=0o040000, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        plain = types.SimpleNamespace(st_mode=0o040000, st_file_attributes=stat.FILE_ATTRIBUTE_DIRECTORY)
        with mock.patch.object(D.os, "lstat", return_value=fake),                 mock.patch.object(D.os.path, "isjunction", None, create=True):
            self.assertTrue(D._is_link("x"))
        with mock.patch.object(D.os, "lstat", return_value=plain),                 mock.patch.object(D.os.path, "isjunction", None, create=True):
            self.assertFalse(D._is_link("x"))


class LinkedPhones(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.root = pldemo.build_package(cls.tmp.name)
        cls.lc = D.find_localcache(cls.root)
        cls.before = tree_hashes(cls.root)
        cls.g = R.Graph()
        cls.vids, cls.pc = D.build(cls.lc, cls.g, R.View, R.ts_seconds, R.iso)
        cls.work = cls.pc.work

    @classmethod
    def tearDownClass(cls):
        cls.pc.close()   # the class-level package copy, or every run leaves one in %TEMP%
        cls.tmp.cleanup()

    def rows(self, vid):
        return self.g.views[vid].rows

    def test_finds_localcache_from_any_input_shape(self):
        idx = os.path.join(self.lc, "Indexed")
        dbdir = os.path.join(idx, pldemo.PAIRING_GUIDS[2], "System", "Database")
        for p in (self.root, self.lc, idx, dbdir):
            self.assertEqual(os.path.normcase(D.find_localcache(p)), os.path.normcase(self.lc), p)
        with tempfile.TemporaryDirectory() as other:
            self.assertIsNone(D.find_localcache(other))

    def test_copies_verified_keys_not_copied_source_untouched(self):
        self.assertTrue(self.pc.verify)
        self.assertTrue(all(v["copy_matches"] == "yes" for v in self.pc.verify))
        self.assertFalse(any("PlatformEncryptedKeyStorage" in v["file"] for v in self.pc.verify))
        self.assertFalse(os.path.exists(os.path.join(self.work, "PlatformEncryptedKeyStorage.json")))
        self.assertEqual(tree_hashes(self.root), self.before)

    def test_every_pairing_in_order_with_status(self):
        got = [(r["#"], r["DeviceFolder"], r["Status"]) for r in self.rows("PC_devices")]
        self.assertEqual(got, [(1, pldemo.PAIRING_GUIDS[0], "folder only - its data was removed"),
                               (2, pldemo.PAIRING_GUIDS[1], "databases present but empty"),
                               (3, pldemo.PAIRING_GUIDS[2], "data present")])
        cur = self.rows("PC_devices")[2]
        self.assertGreater(cur["DataRows"], 0)
        self.assertEqual(cur["SIM"], "Example Carrier +1-555-0100")
        self.assertEqual(cur["StreamedApps"], 2)
        self.assertTrue(cur["FirstActivityUTC"] <= cur["LastActivityUTC"])
        self.assertTrue(cur["FirstActivityUTC"].startswith("2030-01-01"))

    def test_phone_details_including_removed_one(self):
        phones = {r["HardwareID"]: r for r in self.rows("PC_phones")}
        self.assertIn("details removed", phones["00000000EXAMPLE1"]["Note"])
        p = phones["00000000EXAMPLE2"]
        self.assertEqual((p["DisplayName"], p["Manufacturer"], p["Model"], p["OS"], p["Linked"]),
                         ("Example Phone", "ExampleCorp", "EX-1", "Android 99", "yes"))
        self.assertEqual(p["Certificates"], 1)
        self.assertNotIn("example-cert", str(p))  # certificate bodies are never shown
        self.assertTrue(p["RegisteredUTC"])

    def test_trust_and_stored_links(self):
        t = self.rows("PC_trust")[0]
        self.assertEqual((t["TrustType"], t["MsaCid"]), (3, "demo-msa-0001"))
        phone = ("phone", pldemo.DEMO_PHONE_ID)
        self.assertEqual([x for rid, x, _ in self.g.links(phone) if rid == "PR1"], [("trust", pldemo.DEMO_PHONE_ID)])
        pairing = ("pairing", pldemo.PAIRING_GUIDS[2])
        self.assertEqual(len([x for rid, x, _ in self.g.links(pairing) if rid == "PR2"]), 2)

    def test_inferred_by_time_to_closest_folder_only(self):
        rel = self.rows("PR3")
        self.assertEqual(len(rel), 1)
        self.assertIn(pldemo.PAIRING_GUIDS[2][:8], rel[0]["Pairing"])
        self.assertIn("inferred", rel[0]["Basis"])
        self.assertLessEqual(rel[0]["GapSeconds"], D.INFER_WINDOW_S)

    def test_history_in_time_order(self):
        t = [r["TimeUTC"] for r in self.rows("PR4")]
        self.assertEqual(t, sorted(t))
        self.assertEqual(sum(1 for r in self.rows("PR4") if r["Event"].startswith("phone linked")), 3)

    def test_close_removes_copies(self):
        g = R.Graph()
        _, pc = D.build(self.lc, g, R.View, R.ts_seconds, R.iso)
        self.assertIsNone(pc.close())
        self.assertFalse(os.path.exists(pc.work))

    def test_single_device_folder_input(self):
        g = R.Graph()
        vids, pc = D.build(None, g, R.View, R.ts_seconds, R.iso)
        self.assertIsNone(pc)
        self.assertEqual(vids, ["PC_devices"])
        self.assertIn("package", g.views["PC_devices"].basis)


if __name__ == "__main__":
    unittest.main()
