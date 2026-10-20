"""GUI tests for plmapview.py on the synthetic demo (window kept hidden).

    python test_plmapview.py
"""
import csv
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import pldemo  # noqa: E402
import plmapview as V  # noqa: E402


class ViewerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.app = V.Viewer([("demo", src)])
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        work = self.app.ev.work
        self.app.on_close()
        self.assertFalse(os.path.exists(work), "temporary copies should be removed on close")
        self.tmp.cleanup()

    def select(self, vid, index):
        self.app.show(vid, select_index=index)
        self.app.on_row()

    def test_every_view_opens_and_rows_select(self):
        for vid, v in self.app.g.views.items():
            self.app.show(vid)
            self.assertEqual(self.app.cur_vid, vid)
            self.assertEqual(len(self.app.table.get_children()), len(v.rows), vid)
            if v.rows:
                self.select(vid, 0)
                self.assertTrue(self.app.links.get_children(), f"{vid}: links panel empty")

    def test_jump_from_call_to_contact(self):
        self.app.show_cross.set(True)  # call -> contact is a cross-database link
        calls = self.app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        self.select("E_calls", idx)
        target = next(iid for iid, t in self.app.link_targets.items() if t == ("contact", 1))
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "E_contacts")
        row = self.app.g.views["E_contacts"].rows[int(self.app.table.selection()[0])]
        self.assertEqual(row["Name"], "Alice Example")

    def test_relation_row_links_to_its_entities(self):
        self.app.show_cross.set(True)
        self.select("R20", 0)  # photo taken during a call
        targets = set(self.app.link_targets.values())
        self.assertIn(("call", 1), targets)
        self.assertIn(("photo", 1), targets)

    def test_rcs_thread_does_not_link_to_unrelated_sms_thread(self):
        rcs = self.app.g.views["E_rcs"].rows
        idx = next(i for i, r in enumerate(rcs) if r["RCSThreadID"] == 51)  # fallback missing
        self.select("E_rcs", idx)
        self.assertFalse([t for t in self.app.link_targets.values() if t[0] == "thread"])

    def test_filter_sort_rebuild_export(self):
        self.app.show("E_calls")
        self.app.filter_var.set("Alice")
        self.assertEqual(len(self.app.table.get_children()), 2)
        self.app.sort_by("CallID")
        self.app.sort_by("CallID")
        before = len(self.app.g.views["R21"].rows)
        self.app.window_min.set(1)
        self.app.rebuild()
        self.assertLessEqual(len(self.app.g.views["R21"].rows), before)
        out = os.path.join(self.tmp.name, "export.csv")
        V.filedialog.asksaveasfilename = lambda **k: out
        self.app.show("E_calls")
        self.app.filter_var.set("Alice")
        self.app.export()
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        self.assertEqual(rows[0], self.app.g.views["E_calls"].cols)
        self.assertEqual(len(rows), 3)


class DatabaseFirstTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.app = V.Viewer([("demo", pldemo.build(os.path.join(self.tmp.name, "device")))])
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.on_close()
        self.tmp.cleanup()

    def nav_ids(self, parent=""):
        out = []
        for i in self.app.nav.get_children(parent):
            out += [i] + self.nav_ids(i)
        return out

    def test_opens_on_notifications_grouped_by_database(self):
        self.assertEqual(self.app.cur_vid, "N_notifications")
        ids = self.nav_ids()
        self.assertIn("db:notifications.db", ids)
        self.assertNotIn("R13", ids)  # cross-database links hidden by default
        self.app.show_cross.set(True)
        self.app.fill_nav()
        self.assertIn("R13", self.nav_ids())

    def test_notification_links_are_inside_its_database(self):
        rows = self.app.g.views["N_notifications"].rows
        idx = next(i for i, r in enumerate(rows) if r["Source"] == "live" and r["NotificationID"] == "n-1")
        self.app.show("N_notifications", select_index=idx)
        self.app.on_row()
        scopes = {self.app.links.set(i, "scope") for i in self.app.links.get_children()}
        self.assertEqual(scopes, {"same table", "other table, same database"})
        self.assertIn("Raw JSON", self.app.detail.get("1.0", "end"))
        toast = next(i for i, t in self.app.link_targets.items() if t[0] == "toast")
        self.app.links.selection_set(toast)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "N_toasts")


class LinkedPhonesViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pldemo.build_package(self.tmp.name)
        self.app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root))
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        pc_work = self.app.pc.work
        self.app.on_close()
        self.assertFalse(os.path.exists(pc_work))
        self.tmp.cleanup()

    def test_linked_phones_section_and_links(self):
        self.assertIn("PC_devices", self.app.nav.get_children("sec:pc"))
        rows = self.app.g.views["PC_devices"].rows
        idx = next(i for i, r in enumerate(rows) if r["Status"] == "data present")
        self.app.show("PC_devices", select_index=idx)
        self.app.on_row()
        kinds = {t[0] for t in self.app.link_targets.values()}
        self.assertEqual(kinds, {"streamed", "phone"})
        target = next(i for i, t in self.app.link_targets.items() if t[0] == "phone")
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "PC_phones")



class ConnectionsViewTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = pldemo.build_package(self.tmp.name)
        self.app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                            connections=V.C.demo_source())
        self.app.withdraw()
        self.app.update()

    def tearDown(self):
        self.app.on_close()
        self.tmp.cleanup()

    def test_connections_section_summary_first_and_links(self):
        kids = self.app.nav.get_children("sec:conn")
        self.assertEqual(kids[0], "C_phones")
        rows = self.app.g.views["C_usb"].rows
        idx = next(i for i, r in enumerate(rows) if r["PhoneEvidence"])
        self.app.show("C_usb", select_index=idx)
        self.app.on_row()
        kinds = {t[0] for t in self.app.link_targets.values()}
        self.assertTrue({"wpd", "autoplay", "install", "event"} <= kinds)
        target = next(i for i, t in self.app.link_targets.items() if t[0] == "install")
        self.app.links.selection_set(target)
        self.app.on_jump()
        self.assertEqual(self.app.cur_vid, "C_install")

    def test_hidden_without_a_source(self):
        app = V.Viewer(self.app.devices, localcache=self.app.localcache)
        app.withdraw()
        try:
            self.assertFalse(app.nav.exists("sec:conn"))
        finally:
            app.on_close()


if __name__ == "__main__":
    unittest.main()
