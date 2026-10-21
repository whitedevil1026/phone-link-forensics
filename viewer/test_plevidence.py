"""Tests for the evidence layer: staging, carving and read-only opens. Synthetic data only.

    python -B viewer/test_plevidence.py
"""
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import plcarve  # noqa: E402
import plparse as P  # noqa: E402
import plrelate as R  # noqa: E402


def wal_source(folder):
    """A phone.db in WAL mode. Returns the writer connection, kept open so the -wal file survives."""
    os.makedirs(folder, exist_ok=True)
    w = sqlite3.connect(os.path.join(folder, "phone.db"))
    w.execute("PRAGMA journal_mode=WAL")
    w.execute("PRAGMA wal_autocheckpoint=0")
    w.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    w.executemany("INSERT INTO t (v) VALUES (?)", [("row%d" % i,) for i in range(5)])
    w.commit()
    return w


class SnapshotStability(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = os.path.join(self.tmp.name, "device")
        self.w = wal_source(self.src)
        self.real_copy = shutil.copy2
        self.db_copies = 0

    def tearDown(self):
        shutil.copy2 = self.real_copy
        self.w.close()
        self.tmp.cleanup()

    def hook(self, after_db_copy):
        def copy2(s, d, *a, **k):
            r = self.real_copy(s, d, *a, **k)
            if os.path.basename(s) == "phone.db":
                self.db_copies += 1
                after_db_copy(self.db_copies)
            return r
        shutil.copy2 = copy2

    def test_quiet_source_is_stable(self):
        ev = R.Evidence(self.src)
        try:
            self.assertEqual(ev.snapshot_note, "")
            self.assertTrue(ev.verify)
            self.assertTrue(all(v["source_stable"] == "yes" for v in ev.verify))
            self.assertEqual(self.db_copies, 0)
        finally:
            ev.close()

    def test_demo_folder_is_stable(self):
        demo = pldemo.build(os.path.join(self.tmp.name, "demo"))
        ev = R.Evidence(demo)
        try:
            self.assertEqual(ev.snapshot_note, "")
        finally:
            ev.close()

    def test_source_that_keeps_changing_is_flagged(self):
        def writer(n):
            self.w.execute("INSERT INTO t (v) VALUES (?)", ("late%d" % n,))
            self.w.commit()
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.hook(writer)
        ev = R.Evidence(self.src)       # must not raise: the copy is kept, with a warning
        try:
            self.assertEqual(self.db_copies, 3)          # the whole set was retried, then given up
            self.assertIn("phone.db", ev.snapshot_note)
            self.assertTrue(ev.verify)
            self.assertTrue(all(v["source_stable"] == "NO - source changed during copy" for v in ev.verify
                                if v["file"].startswith("phone.db")))
        finally:
            ev.close()

    def test_unstable_set_does_not_claim_a_source_hash(self):
        def writer(n):
            self.w.execute("INSERT INTO t (v) VALUES (?)", ("late%d" % n,))
            self.w.commit()
            self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        self.hook(writer)
        ev = R.Evidence(self.src)
        try:
            row = [a for a in ev.acq if a["file"] == "phone.db"][0]
            self.assertEqual(row["sha256"], "")                  # no source hash was ever settled
            self.assertEqual(row["copy_sha256"], P.sha256(os.path.join(ev.work, "phone.db")))
            for v in ev.verify:
                self.assertEqual(v["source_sha256"], "")
                self.assertEqual(v["copy_matches"], "n/a - source changed during copy")
                self.assertEqual(v["copy_sha256"], P.sha256(os.path.join(ev.work, v["file"])))
            g = R.build(ev)
            view = g.views["P_copies"]
            self.assertEqual(view.cols, ["file", "source_sha256", "copy_sha256", "copy_matches", "source_stable"])
            self.assertTrue(all(r["copy_matches"].startswith("n/a") for r in view.rows))
            self.assertTrue(all(r["source_stable"] == P.UNSTABLE for r in view.rows))
        finally:
            ev.close()

    def test_stable_set_keeps_its_source_hash(self):
        ev = R.Evidence(self.src)
        try:
            row = [a for a in ev.acq if a["file"] == "phone.db"][0]
            self.assertEqual(row["sha256"], P.sha256(os.path.join(self.src, "phone.db")))
            self.assertNotIn("copy_sha256", row)
            for v in ev.verify:
                self.assertEqual(v["copy_matches"], "yes")
                self.assertTrue(v["source_sha256"])
            g = R.build(ev)
            view = g.views["P_copies"]
            self.assertEqual(view.cols, ["file", "source_sha256", "copy_sha256", "copy_matches", "source_stable"])
            self.assertTrue(all(r["copy_matches"] == "yes" and r["source_stable"] == "yes" for r in view.rows))
        finally:
            ev.close()

    def test_one_off_change_is_retried_and_copy_holds_the_data(self):
        def once(n):
            if n == 1:
                self.w.execute("INSERT INTO t (v) VALUES ('late')")
                self.w.commit()
                self.w.execute("PRAGMA wal_checkpoint(TRUNCATE)")   # WAL emptied between .db and -wal copies
        self.hook(once)
        ev = R.Evidence(self.src)
        try:
            self.assertEqual(self.db_copies, 2)
            self.assertEqual(ev.snapshot_note, "")
            vals = [r["v"] for r in ev.rows("phone.db", "SELECT v FROM t", "t")]
            self.assertIn("late", vals)
            self.assertEqual(len(vals), 6)
        finally:
            ev.close()


class OddFolderNames(unittest.TestCase):
    def test_hash_and_percent_in_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "case#1 %41")
            os.makedirs(folder)
            path = os.path.join(folder, "x.db")
            c = sqlite3.connect(path)
            c.execute("CREATE TABLE t (a TEXT)")
            c.execute("INSERT INTO t VALUES ('right row')")
            c.commit()
            c.close()
            before = sorted(os.listdir(folder))
            u = P.ro_uri(path)
            self.assertTrue(u.endswith("?mode=ro"))
            ro = sqlite3.connect(u, uri=True)
            try:
                self.assertEqual(ro.execute("SELECT a FROM t").fetchall(), [("right row",)])
                with self.assertRaises(sqlite3.OperationalError):
                    ro.execute("INSERT INTO t VALUES ('x')")
            finally:
                ro.close()
            self.assertEqual(sorted(os.listdir(folder)), before)

    def test_carve_reads_schema_from_odd_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = os.path.join(tmp, "case#1 %41")
            os.makedirs(folder)
            path = os.path.join(folder, "x.db")
            make_deleted_notes(path, [("tag", "hello deleted note")] * 40)
            recs, st = plcarve.carve_db(path)
            self.assertTrue(recs, st)
            self.assertEqual({r["table"] for r in recs}, {"notes"})


def read_bytes(path):
    with open(path, "rb") as fh:
        return fh.read()


def make_deleted_notes(path, rows):
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, tag TEXT, body TEXT)")
    c.executemany("INSERT INTO notes (tag, body) VALUES (?, ?)", rows)
    c.commit()
    c.execute("DELETE FROM notes")      # a full delete frees the pages whole, bytes intact
    c.commit()
    c.close()


class Overflow(unittest.TestCase):
    def test_local_payload_size(self):
        # U=4096: X=4061, M=489. A 6,000-byte payload keeps K=489+((6000-489)%4092)=1928 locally.
        self.assertEqual(plcarve.local_payload(4096, 100), 100)
        self.assertEqual(plcarve.local_payload(4096, 4061), 4061)
        self.assertEqual(plcarve.local_payload(4096, 6000), 489 + ((6000 - 489) % 4092))
        self.assertEqual(plcarve.local_payload(4096, 4092 + 489 + 4061 + 10), 489)   # K > X falls back to M

    def test_long_text_is_cut_at_the_local_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            rows = [("long", "A" * 6000), ("other", "B" * 6000)] + [("s", "short %d" % i) for i in range(30)]
            make_deleted_notes(path, rows)
            recs, _ = plcarve.carve_db(path)
            longs = [r for r in recs if r.get("tag") == "long"]
            self.assertTrue(longs, "the overflowed record should still be recovered, truncated")
            for r in longs:
                self.assertEqual(r["overflow_lost"], "yes")
                self.assertEqual(len(r["body"]), 1908)       # local payload 1917 - 9 bytes of record header
                self.assertLessEqual(set(r["body"]), {"A", chr(0)})   # no bytes from another page
            self.assertIn("A" * 1908, [r["body"] for r in longs])
            for r in recs:
                if r.get("tag") == "s":
                    self.assertNotIn("overflow_lost", r)


class UnreadableLiveRows(unittest.TestCase):
    def test_status_is_unknown_not_deleted(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "gone %d" % i) for i in range(20)])
            recs, st = plcarve.carve_db(path)
            self.assertTrue(recs)
            self.assertEqual({r["status"] for r in recs}, {"deleted_or_overwritten"})   # unchanged when readable
            real_live = plcarve.live_rowids
            plcarve.live_rowids = lambda db, table, errors=None: None     # as when the live table cannot be read
            try:
                recs, st = plcarve.carve_db(path)
            finally:
                plcarve.live_rowids = real_live
            self.assertTrue(recs)
            self.assertEqual({r["status"] for r in recs}, {plcarve.UNKNOWN_LIVE})
            self.assertEqual(plcarve.UNKNOWN_LIVE, "unknown - live rows unreadable")

    def test_live_rowids_returns_none_when_unreadable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "x")])
            errs = []
            self.assertIsNone(plcarve.live_rowids(path, "no_such_table", errs))
            self.assertTrue(errs)
            self.assertEqual(plcarve.live_rowids(path, "notes"), set())


class OverflowOtherPageSizes(unittest.TestCase):
    """local_payload with a 1,024-byte page and with reserved bytes at the end of each page (header byte 20)."""
    TEXT = 3000

    def make(self, folder):
        path = os.path.join(folder, "n.db")
        c = sqlite3.connect(path)
        c.execute("PRAGMA page_size=1024")
        c.execute("CREATE TABLE notes (id INTEGER PRIMARY KEY, tag TEXT, body TEXT)")
        c.executemany("INSERT INTO notes (tag, body) VALUES (?, ?)",
                      [("long", "A" * self.TEXT), ("lone", "B" * self.TEXT)] + [("s", "short %d" % i) for i in range(40)])
        c.commit()
        c.execute("DELETE FROM notes")
        c.commit()
        c.close()
        return path

    def expected_body_len(self, usable):
        hsz = 1 + 1 + 1 + 2                 # header size byte, NULL id, 4-char tag, 3,000-char body (2-byte serial)
        psz = hsz + 4 + self.TEXT
        return plcarve.local_payload(usable, psz) - hsz - 4

    def test_local_payload_values_on_1024_pages(self):
        self.assertEqual(plcarve.local_payload(1024, 989), 989)                # exactly X = U - 35
        self.assertEqual(plcarve.local_payload(1024, 990), 103)                # K = 990 > X, so M = 103
        self.assertEqual(plcarve.local_payload(1024, 2000), 103 + ((2000 - 103) % 1020))   # K = 980 <= X
        self.assertEqual(plcarve.local_payload(1008, 3000), 101)               # reserved 16: M = 101, K = 992 > X = 973
        self.assertNotEqual(plcarve.local_payload(1008, 3000), plcarve.local_payload(1024, 3000))

    def test_carve_cuts_at_the_local_payload_of_a_1024_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs, st = plcarve.carve_db(self.make(tmp))
            self.assertEqual(st["page_size"], 1024)
            longs = [r for r in recs if r.get("tag") in ("long", "lone")]
            self.assertTrue(longs)
            for r in longs:
                self.assertEqual(r["overflow_lost"], "yes")
                self.assertEqual(len(r["body"]), self.expected_body_len(1024))
            for r in recs:
                if r.get("tag") == "s":
                    self.assertNotIn("overflow_lost", r)

    def test_carve_honours_reserved_bytes_in_the_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self.make(tmp)
            shapes = plcarve.table_shapes(path)
            data = bytearray(read_bytes(path))
            data[20] = 16                       # 16 reserved bytes: usable = 1008
            with open(path, "wb") as fh:
                fh.write(data)
            real = plcarve.table_shapes
            plcarve.table_shapes = lambda *a, **k: shapes      # SQLite itself would call the patched page 1 corrupt
            try:
                recs, st = plcarve.carve_db(path)
            finally:
                plcarve.table_shapes = real
            longs = [r for r in recs if r.get("tag") in ("long", "lone")]
            self.assertTrue(longs, st)
            self.assertNotEqual(self.expected_body_len(1008), self.expected_body_len(1024))
            for r in longs:
                self.assertEqual(len(r["body"]), self.expected_body_len(1008))


class CarveNeverCrashes(unittest.TestCase):
    def test_corrupt_schema_name_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            path = os.path.join(src, "notifications.db")
            data = bytearray(read_bytes(path))
            i = data.find(b"tablenotification_toastmappings") + 5     # the sqlite_master name column
            self.assertGreater(i, 5)
            data[i + 3] = 0xFF                                         # invalid UTF-8
            with open(path, "wb") as fh:
                fh.write(data)
            recs, st = plcarve.carve_db(path)
            self.assertIsInstance(recs, list)
            self.assertTrue(st.get("errors"), st)

    def test_connections_are_closed(self):
        # an unreleased handle would keep this file locked on Windows
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "n.db")
            make_deleted_notes(path, [("t", "x text")] * 5)
            data = bytearray(read_bytes(path))
            i = data.find(b"tablenotes") + 5
            data[i + 2] = 0xFF
            with open(path, "wb") as fh:
                fh.write(data)
            plcarve.carve_db(path)
            os.remove(path)


class FailedStaging(unittest.TestCase):
    def test_work_folder_removed_when_stage_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            work = os.path.join(tmp, "work")
            real = P.stage

            def broken(src_dir, w, acq):
                os.makedirs(w, exist_ok=True)
                shutil.copy2(os.path.join(src_dir, "phone.db"), os.path.join(w, "phone.db"))
                raise RuntimeError("disk full")
            P.stage = broken
            try:
                with self.assertRaises(RuntimeError):
                    R.Evidence(src, work=work)
            finally:
                P.stage = real
            self.assertFalse(os.path.exists(work))

    def test_default_work_folder_removed_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            made = []
            real_mk, real_stage = R.tempfile.mkdtemp, P.stage

            def mk(*a, **k):
                d = real_mk(*a, **k)
                made.append(d)
                return d

            def broken(src_dir, w, acq):
                shutil.copy2(os.path.join(src_dir, "phone.db"), os.path.join(w, "phone.db"))
                raise RuntimeError("boom")
            R.tempfile.mkdtemp, P.stage = mk, broken
            try:
                with self.assertRaises(RuntimeError):
                    R.Evidence(src)
            finally:
                R.tempfile.mkdtemp, P.stage = real_mk, real_stage
            self.assertEqual(len(made), 1)
            self.assertFalse(os.path.exists(made[0]))


if __name__ == "__main__":
    unittest.main()
