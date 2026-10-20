"""Tests for pldevices.py - every phone ever linked to the PC. Synthetic package only.

    python test_pldevices.py
"""
import hashlib
import os
import sys
import tempfile
import unittest

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
