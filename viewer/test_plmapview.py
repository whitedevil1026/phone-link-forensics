"""GUI tests for plmapview.py on the synthetic demo (window kept hidden).

    python test_plmapview.py
"""
import csv
import os
import sys
import tempfile
import unittest
from unittest import mock

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
        row = self.app.g.views["R20"].rows[0]
        mine = self.app.row_entities(row)
        self.assertIn(("call", 1), mine)              # the row is about call 1 and photo 1 ...
        self.assertIn(("photo", 1), mine)
        targets = set(self.app.link_targets.values())
        self.assertFalse(set(mine) & targets)         # ... which are not listed as "linked rows"
        self.assertIn(("contact", 1), targets)        # what they link to is

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

    def test_sort_orders_table_keeps_filter_selection_and_export_follows(self):
        self.app.show("E_calls")
        self.app.filter_var.set("")
        v = self.app.g.views["E_calls"]
        shown = lambda: [v.rows[int(k)]["CallID"] for k in self.app.table.get_children()]
        first = self.app.table.get_children()[0]
        self.app.table.selection_set(first)
        self.app.sort_by("CallID")
        self.app.sort_by("CallID")              # second click: descending
        ids = shown()
        self.assertEqual(ids, sorted(ids, key=lambda x: float(x), reverse=True))
        self.assertEqual(self.app.table.selection(), (first,))
        out = os.path.join(self.tmp.name, "sorted.csv")
        V.filedialog.asksaveasfilename = lambda **k: out
        self.app.export()
        with open(out, encoding="utf-8-sig") as f:
            exported = [r[v.cols.index("CallID")] for r in list(csv.reader(f))[1:]]
        self.assertEqual(exported, [str(x) for x in ids])
        self.app.filter_var.set("Alice")        # a filter still applies after a sort
        self.assertEqual(len(self.app.table.get_children()), 2)


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
        self.assertIn("grp:notifications", ids)
        self.assertTrue(self.app.nav.item("grp:notifications", "open"))
        self.assertNotIn("R13", ids)  # cross-database links hidden by default
        self.app.show_cross.set(True)
        self.app.fill_nav()
        self.assertIn("R13", self.nav_ids())

    def test_view_list_uses_plain_names(self):
        self.app.show_cross.set(True)
        self.app.fill_nav()
        views = [i for i in self.nav_ids() if i in self.app.g.views]
        self.assertEqual(sorted(views), sorted(self.app.g.views))          # every view is listed once
        for vid in views:
            text = self.app.nav.item(vid, "text")
            self.assertNotRegex(text, r"^[A-Z]{1,3}\d+\b|\.db\b|_|->", vid)  # no codes, files, raw names
            self.assertEqual(self.app.nav.set(vid, "rows"), f"{len(self.app.g.views[vid].rows):,}")
        unnamed = [v for v in self.app.g.views if v not in V.NAV and not v.startswith("M_")]
        self.assertEqual(unnamed, [], "give these views a plain name in plmapview.NAV")
        self.app.show("R03")
        self.assertEqual(self.app.title_lbl.cget("text"), "Conversation → its messages")
        self.assertIn("Technical name: R03", self.app.basis_lbl.cget("text"))   # nothing technical is lost
        self.app.nav.item("sub:messages:links", open=True)
        self.app.fill_nav()                                                     # a rebuild keeps what was open
        self.assertTrue(self.app.nav.item("sub:messages:links", "open"))

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
        self.assertIn("PC_devices", self.app.nav.get_children("grp:linked"))
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
        kids = self.app.nav.get_children("grp:conn")
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
            self.assertFalse(app.nav.exists("grp:conn"))
        finally:
            app.on_close()



def tkfont_measure(widget, text):
    import tkinter.font as tkfont
    return tkfont.Font(font=widget.cget("font")).measure(text)


class WindowFixesTest(unittest.TestCase):
    """The review fixes for the window: toolbar, cross toggle, nearby box, export, device switch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = pldemo.build(os.path.join(self.tmp.name, "device"))
        self.apps = []
        self.app = self.make([("demo", self.src)])

    def make(self, devices, withdraw=True, **kw):
        app = V.Viewer(devices, **kw)
        if withdraw:
            app.withdraw()
        app.update()
        self.apps.append(app)
        return app

    def tearDown(self):
        for a in self.apps:
            try:
                a.on_close(warn=False)
            except Exception:
                pass
        self.tmp.cleanup()

    # ---- 1 toolbar
    def test_toolbar_controls_stay_usable_with_a_long_source_path(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("a" * 10 + "p" * 140, self.src), ("second", d2)], withdraw=False)
        app.geometry("1256x675")
        app.update()
        controls = {"device picker": app.dev_combo, "Nearby box": app.nearby_spin, "inferred tick": app.cross_chk,
                    "Open folder": app.open_btn, "Export": app.export_btn}
        for name, w in controls.items():
            self.assertTrue(w.winfo_ismapped(), name)
            self.assertGreaterEqual(w.winfo_width(), w.winfo_reqwidth(), name)

    def test_long_source_line_is_shortened_in_the_middle_to_fit(self):
        app = self.make([("a" * 10 + "p" * 140, self.src)], withdraw=False)
        app.geometry("600x500")
        app.update()
        text = app.src_lbl.cget("text")
        self.assertIn("…", text)
        self.assertLess(len(text), 100)
        self.assertTrue(text.startswith("aaaa") and text.endswith(self.src[-6:]))
        self.assertLessEqual(tkfont_measure(app.src_lbl, text), app.src_lbl.winfo_width())

    def test_elide_middle(self):
        self.assertEqual(V.elide_middle("short", 20), "short")
        out = V.elide_middle("a" * 50 + "TAIL", 21)
        self.assertEqual(len(out), 21)
        self.assertTrue(out.startswith("aaaa") and out.endswith("TAIL") and "…" in out)

    # ---- 2 inferred toggle
    def test_unticking_inferred_matches_leaves_an_inferred_view(self):
        app = self.app
        app.show_cross.set(True)
        app.fill_nav()
        app.show("R13")
        self.assertEqual(app.g.views["R13"].kind, "cross")
        app.show_cross.set(False)
        app.on_cross_toggle()
        self.assertNotEqual(app.g.views[app.cur_vid].kind, "cross")
        self.assertEqual(app.cur_vid, "N_notifications")
        self.assertFalse(app.nav.exists("R13"))

    def test_toggling_inferred_matches_refreshes_the_linked_rows(self):
        app = self.app
        calls = app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        scopes = lambda: {app.links.set(i, "scope") for i in app.links.get_children()}
        app.show_cross.set(True)
        app.show("E_calls", select_index=idx)
        app.on_row()
        self.assertIn("other database (inferred)", scopes())
        app.show_cross.set(False)
        app.on_cross_toggle()
        self.assertEqual(app.cur_vid, "E_calls")
        self.assertNotIn("other database (inferred)", scopes())

    # ---- 3 nearby minutes
    def test_nearby_minutes_is_validated_and_clamped(self):
        app = self.app
        for text, want in (("30", 30), ("", 30), ("abc", 30), ("-5", 1), ("99999", 1440), (" 7 ", 7), ("1.5", 7)):
            app.window_min.set(text)
            self.assertEqual(app.nearby(), want, repr(text))
            app.show(app.cur_vid)            # never raises, whatever is in the box
            app.fill_nav()
            app.friendly("R21")

    def test_rebuild_normalises_the_nearby_box(self):
        app = self.app
        app.window_min.set("99999")
        app.rebuild()
        self.assertEqual(app.window_min.get(), "1440")
        app.window_min.set("")
        app.rebuild()
        self.assertEqual(app.window_min.get(), "1440")

    # ---- 4/5 export
    def export_to(self, path, vid="E_calls"):
        self.app.show(vid)
        with mock.patch.object(V.filedialog, "asksaveasfilename", return_value=path):
            self.app.export()

    def test_export_failure_is_shown_and_not_a_success_message(self):
        self.export_to(os.path.join(self.tmp.name, "ok.csv"))
        self.assertIn("Exported", self.app.status.cget("text"))
        bad = os.path.join(self.tmp.name, "no_such_folder", "x.csv")
        with mock.patch.object(V.messagebox, "showerror") as err:
            self.export_to(bad)
        self.assertEqual(err.call_count, 1)
        self.assertNotIn("Exported", self.app.status.cget("text"))
        self.assertIn("FAILED", self.app.status.cget("text"))
        self.assertFalse(os.path.exists(bad))

    def test_export_is_atomic(self):
        out = os.path.join(self.tmp.name, "keep.csv")
        with open(out, "w") as f:
            f.write("old content")
        calls = {"n": 0}
        real = V.csv_cell

        def boom(x):
            calls["n"] += 1
            if calls["n"] > 8:
                raise OSError("disk full")
            return real(x)
        with mock.patch.object(V, "csv_cell", boom), mock.patch.object(V.messagebox, "showerror"):
            self.export_to(out)
        with open(out) as f:
            self.assertEqual(f.read(), "old content")
        self.assertEqual([n for n in os.listdir(self.tmp.name) if n.endswith(".tmp")], [])
        self.assertIn("FAILED", self.app.status.cget("text"))

    def test_csv_cells_are_excel_safe(self):
        c = V.csv_cell
        for danger in ("=1+1", "@SUM(A1)", "\tx", "\rx", "+cmd|x", "-cmd", "+ 1", "-A1",
                       "-2+3+cmd|' /C calc'!A0", "+1+1", "+1-555-0150", "+1 555", "-5-5", "-5.", "+5.5", "- 5", "-",
                       "+", "-5\n", "--5", "+-5", "-٣"):
            self.assertEqual(c(danger), "'" + danger, danger)
        for same in ("+15550100", "-5", "-5.5", "-0.5", "3.14", "hello", "", "a=b", "12", "0", "-0"):
            self.assertEqual(c(same), same, same)
        self.assertEqual(c(None), "")
        self.assertEqual(c(42), "42")
        self.assertEqual(c(-3), "-3")
        self.assertEqual(c(b"\x00\x01\x02"), "<3 bytes>")
        self.assertEqual(c("a\x00b"), "ab")
        self.assertEqual(c("\x00=1"), "'=1")      # NULs go first, then the formula check

    def test_export_writes_safe_cells_and_says_so(self):
        v = self.app.g.views["E_calls"]
        v.rows[0]["ContactName"] = "=HYPERLINK(1)"
        v.rows[1]["ContactName"] = b"\xff\xfe"
        v.rows[2]["ContactName"] = "+15550100"
        out = os.path.join(self.tmp.name, "safe.csv")
        self.export_to(out)
        with open(out, encoding="utf-8-sig") as f:
            rows = list(csv.reader(f))
        col = v.cols.index("ContactName")
        got = [r[col] for r in rows[1:4]]
        self.assertEqual(got, ["'=HYPERLINK(1)", "<2 bytes>", "+15550100"])
        text = self.app.status.cget("text")
        self.assertIn("Excel-safe", text)
        self.assertIn("unless the whole cell is a plain number", text)     # the rule, stated
        self.assertIn("-5", text)
        self.assertIn("<N bytes>", text)

    # ---- 6 failed device switch
    def bad_device(self, name="bad"):
        bad = pldemo.build(os.path.join(self.tmp.name, name))
        with open(os.path.join(bad, "calling.db"), "wb") as f:
            f.write(b"this is not a database" * 100)
        return bad

    def test_failed_device_switch_keeps_the_previous_device(self):
        app = self.make([("good", self.src), ("bad", self.bad_device())])
        old_ev, old_g, old_work = app.ev, app.g, app.ev.work
        before = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("plrelate-")}
        app.dev_combo.current(1)
        with mock.patch.object(V.messagebox, "showerror") as err:
            app.load_device(1)
        self.assertEqual(err.call_count, 1)
        self.assertIs(app.ev, old_ev)
        self.assertIs(app.g, old_g)
        self.assertTrue(os.path.isdir(old_work), "the previous device's copies must stay")
        self.assertEqual(app.dev_combo.current(), 0)
        self.assertIn("good", app.src_lbl.cget("text"))
        self.assertNotIn("bad", app.src_lbl.cget("text"))
        after = {n for n in os.listdir(tempfile.gettempdir()) if n.startswith("plrelate-")}
        self.assertEqual(after - before, set(), "the failed device's partial copy must be removed")
        app.show("E_calls")                   # still fully usable
        self.assertTrue(app.table.get_children())

    def test_successful_device_switch_removes_the_old_copies(self):
        d2 = pldemo.build(os.path.join(self.tmp.name, "device2"))
        app = self.make([("one", self.src), ("two", d2)])
        old = app.ev.work
        app.load_device(1)
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.isdir(app.ev.work))
        self.assertEqual(app.dev_combo.current(), 1)

    def test_failed_open_folder_keeps_the_previous_device_list(self):
        app = self.app
        folder = os.path.join(self.tmp.name, "other")
        bad = self.bad_device("other")
        old_devices = app.devices
        with mock.patch.object(V.filedialog, "askdirectory", return_value=bad), \
                mock.patch.object(V.messagebox, "showerror") as err:
            app.open_folder()
        self.assertEqual(err.call_count, 1)
        self.assertIs(app.devices, old_devices)
        self.assertEqual(app.cur_vid, "N_notifications")
        self.assertIn("demo", app.src_lbl.cget("text"))

    # ---- 8 this-PC detection
    def test_this_pc_detection_is_the_exact_package_folder(self):
        base = os.path.join(self.tmp.name, "Local")
        pkg = os.path.join(base, "Packages", "Microsoft.YourPhone_8wekyb3d8bbwe")
        f = lambda p: V.is_this_pc_package(p, base)
        self.assertTrue(f(pkg))
        self.assertTrue(f(os.path.join(pkg, "LocalCache", "Indexed")))
        self.assertTrue(f(pkg.upper()))
        self.assertFalse(f(os.path.join(base, "Temp", "case", "export")))
        self.assertFalse(f(base + "Low"))
        self.assertFalse(f(os.path.join(base + "Low", "Packages", "Microsoft.YourPhone_8wekyb3d8bbwe")))
        self.assertFalse(f(pkg + "_copy"))
        self.assertFalse(f(os.path.join(base, "Packages", "Other")))

    # ---- 9 callback errors
    def test_callback_errors_are_shown_and_printed(self):
        import contextlib
        import io
        err = io.StringIO()
        with mock.patch.object(V.messagebox, "showerror") as box, contextlib.redirect_stderr(err):
            self.app.after(0, lambda: 1 / 0)
            self.app.update()
        self.assertEqual(box.call_count, 1)
        self.assertIn("ZeroDivisionError", box.call_args[0][1])
        self.assertIn("Traceback", err.getvalue())

    # ---- 10 small fixes
    def test_handlers_do_nothing_without_a_loaded_device(self):
        with mock.patch.object(V.messagebox, "showerror"):
            app = self.make([("bad", self.bad_device())])
        self.assertIsNone(app.g)
        with mock.patch.object(V.messagebox, "showerror") as err, \
                mock.patch.object(V.filedialog, "asksaveasfilename", return_value="x.csv"):
            for fn in (app.on_row, app.on_jump, app.on_nav, app.export, app.rebuild, app.populate, app.fill_nav,
                       app.on_cross_toggle, lambda: app.sort_by("x"), lambda: app.show("N_notifications")):
                fn()
        self.assertEqual(err.call_count, 0)

    def test_sort_direction_is_remembered_per_view(self):
        app = self.app
        ids = lambda: [float(app.g.views[app.cur_vid].rows[int(k)]["CallID"]) for k in app.table.get_children()]
        app.show("E_calls")
        app.sort_by("CallID")                  # E_calls is now sorted ascending; its next click is descending
        self.assertEqual(ids(), sorted(ids()))
        other = next(v for v in app.g.views if v != "E_calls" and "CallID" in app.g.views[v].cols
                     and len(app.g.views[v].rows) > 1)
        app.show(other)
        app.sort_by("CallID")                  # first click in this view: ascending, whatever E_calls did
        self.assertEqual(ids(), sorted(ids()))

    def test_selected_row_box_is_read_only_but_selectable(self):
        app = self.app
        app.show("E_calls", select_index=0)
        app.on_row()
        self.assertEqual(app.detail.cget("state"), "disabled")
        text = app.detail.get("1.0", "end").strip()
        self.assertTrue(text)
        app.detail.tag_add("sel", "1.0", "end")
        self.assertTrue(app.detail.tag_ranges("sel"))
        app.detail.insert("end", "typed")      # a disabled Text ignores inserts
        self.assertEqual(app.detail.get("1.0", "end").strip(), text)
        app.populate()                         # clearing on refill still works
        self.assertEqual(app.detail.get("1.0", "end").strip(), "")

    def test_enter_on_a_linked_row_jumps(self):
        app = self.make([("demo", self.src)], withdraw=False)    # key events need a mapped window
        app.show_cross.set(True)
        calls = app.g.views["E_calls"].rows
        idx = next(i for i, r in enumerate(calls) if r["CallID"] == 1)
        app.show("E_calls", select_index=idx)
        app.update()                          # let the table's selection event run first
        app.on_row()
        target = next(iid for iid, t in app.link_targets.items() if t == ("contact", 1))
        app.links.selection_set(target)
        self.assertTrue(app.links.bind("<Return>"))
        app.links.focus_force()
        app.links.event_generate("<Return>")
        app.update()
        self.assertEqual(app.cur_vid, "E_contacts")

    def test_linked_rows_panel_names_r21_with_the_current_window(self):
        app = self.app
        app.window_min.set("17")
        app.rebuild()
        app.show_cross.set(True)
        app.g.link("R21", ("call", 1), ("call", 2), "")        # a link with no note of its own: the panel names the view
        idx = next(i for i, r in enumerate(app.g.views["E_calls"].rows) if r["CallID"] == 1)
        app.show("E_calls", select_index=idx)
        app.on_row()
        hows = [app.links.set(k, "how") for k in app.links.get_children()]
        self.assertIn("Things within 17 min of each other", hows)

    # ---- 11 snapshot warning
    def test_snapshot_note_is_a_warning_in_the_status_bar(self):
        class Noted(V.R.Evidence):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                self.snapshot_note = "SNAPSHOT-NOTE-TEXT"
        self.assertNotIn("WARNING", self.app.status.cget("text"))
        with mock.patch.object(V.R, "Evidence", Noted):
            self.app.load_device(0)
        self.assertIn("WARNING", self.app.status.cget("text"))
        self.assertIn("SNAPSHOT-NOTE-TEXT", self.app.status.cget("text"))


class LinkedPhonesFailureTest(unittest.TestCase):
    def test_package_copy_is_closed_when_the_connections_build_fails(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as scratch:
            root = pldemo.build_package(tmp)
            old_tempdir, tempfile.tempdir = tempfile.tempdir, scratch      # count only the folders this test makes
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source())
            app.withdraw()
            app.update()
            try:
                def left():
                    return [n for n in os.listdir(scratch) if n.startswith("pldevices-")]
                g = V.R.build(app.ev)
                kept = len(left())               # the one the open window holds
                with mock.patch.object(V.C, "build", side_effect=KeyError("odd shape")):
                    with self.assertRaises(KeyError):
                        app._add_linked_phones(g)
                self.assertEqual(len(left()), kept)      # the new package copy was closed, not leaked
            finally:
                app.on_close(warn=False)
                tempfile.tempdir = old_tempdir

    def test_failed_load_with_a_broken_connections_build_keeps_the_old_device(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as scratch:
            root = pldemo.build_package(tmp)
            old_tempdir, tempfile.tempdir = tempfile.tempdir, scratch
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source())
            app.withdraw()
            app.update()
            try:
                before = sorted(os.listdir(scratch))
                with mock.patch.object(V.C, "build", side_effect=KeyError("odd shape")),                         mock.patch.object(V.messagebox, "showerror") as err:
                    self.assertFalse(app.load_device(0))
                self.assertEqual(err.call_count, 1)
                self.assertEqual(sorted(os.listdir(scratch)), before)
            finally:
                app.on_close(warn=False)
                tempfile.tempdir = old_tempdir


class DemoRedactionTest(unittest.TestCase):
    def test_demo_mode_never_shows_a_local_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pldemo.build_package(tmp)
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source(), redact=True)
            app.withdraw()
            app.update()
            try:
                needles = {os.path.normcase(x) for x in (tmp, tempfile.gettempdir(), app.ev.work, app.pc.work)}
                texts = {"status": app.status.cget("text"), "source": app.src_lbl.cget("text")}
                app.show_cross.set(True)
                app.fill_nav()
                for vid in app.g.views:
                    app.show(vid)
                    texts["banner " + vid] = app.basis_lbl.cget("text")
                    texts["status " + vid] = app.status.cget("text")
                for what, text in texts.items():
                    low = os.path.normcase(text)
                    for n in needles:
                        self.assertNotIn(n, low, what)
                self.assertIn("synthetic demo folder", texts["banner P_copies"])
                self.assertIn("synthetic demo folder", texts["banner PC_copies"])
                shown = []
                with mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)), \
                        mock.patch.object(V.filedialog, "asksaveasfilename",
                                          return_value=os.path.join(tmp, "nope", "x.csv")):
                    app.export()
                self.assertTrue(shown)
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(shown[0]))
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(app.status.cget("text")))
            finally:
                app.on_close(warn=False)

    def test_empty_folder_dialog_is_redacted_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = pldemo.build_package(tmp)
            app = V.Viewer(V.P.find_device_dirs(root), localcache=V.D.find_localcache(root),
                           connections=V.C.demo_source(), redact=True)
            app.withdraw()
            app.update()
            try:
                empty = os.path.join(tmp, "somebody-else-case")
                os.makedirs(empty)
                shown = []
                with mock.patch.object(V.filedialog, "askdirectory", return_value=empty),                         mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)):
                    app.open_folder()
                self.assertEqual(len(shown), 1)
                self.assertNotIn("somebody-else-case", shown[0])
                self.assertNotIn(os.path.normcase(tmp), os.path.normcase(shown[0]))
                self.assertIn("synthetic demo folder", shown[0])
            finally:
                app.on_close(warn=False)

    def test_empty_folder_dialog_names_the_folder_outside_demo_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = pldemo.build(os.path.join(tmp, "device"))
            app = V.Viewer([("demo", src)])
            app.withdraw()
            app.update()
            try:
                empty = os.path.join(tmp, "empty-case")
                os.makedirs(empty)
                shown = []
                with mock.patch.object(V.filedialog, "askdirectory", return_value=empty),                         mock.patch.object(V.messagebox, "showerror", lambda t, m: shown.append(m)):
                    app.open_folder()
                self.assertIn("empty-case", shown[0])
            finally:
                app.on_close(warn=False)


if __name__ == "__main__":
    unittest.main()
