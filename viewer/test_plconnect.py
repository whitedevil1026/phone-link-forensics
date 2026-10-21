"""Tests for plconnect.py - phones connected by USB cable or Bluetooth. Synthetic registry only.

    python test_plconnect.py
"""
import os
import sys
import tempfile
import types
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import plconnect as C  # noqa: E402
import plrelate as R  # noqa: E402


class Recording(C.FakeSource):
    """Records every registry path read, so we can prove secrets are never touched."""

    def __init__(self, base):
        super().__init__(base.keys, base._text, base._events)
        self.read = []

    def subkeys(self, hive, path):
        self.read.append(path)
        return super().subkeys(hive, path)

    def values(self, hive, path):
        self.read.append(path)
        return super().values(hive, path)


class Decoding(unittest.TestCase):
    def test_filetime_int_and_bytes(self):
        ft = 133500000000000000
        self.assertEqual(C.filetime(ft), C.filetime(ft.to_bytes(8, "little")))
        self.assertTrue(C.filetime(ft).startswith("2024-"))
        self.assertEqual(C.filetime(0), "")
        self.assertEqual(C.filetime(None), "")

    def test_names_mac_class(self):
        self.assertEqual(C.bt_name(b"Example Phone\x00junk"), "Example Phone")
        self.assertEqual(C.mac("00005e005301"), "00:00:5E:00:53:01")
        self.assertEqual(C.bt_class(0x000204), ("Phone (COD 0x000204)", True))
        self.assertFalse(C.bt_class(0x000418)[1])
        self.assertEqual(C.clean_desc("@oem1.inf,%phone%;Example Phone"), "Example Phone")

    def test_setupapi_and_events_parsing(self):
        text = (">>>  [Device Install (Hardware initiated) - USB\\VID_1234&PID_0F01\\X1]\n"
                ">>>  Section start 2030/01/01 12:00:00.000\n"
                ">>>  [Device Install (DiInstallDriver) - C:\\Example\\driver.inf]\n"
                ">>>  Section start 2030/01/02 12:00:00.000\n")
        got = C.parse_setupapi(text)
        self.assertEqual(got[0], ("USB\\VID_1234&PID_0F01\\X1", "2030-01-01 12:00:00", "Device Install (Hardware initiated)"))
        xml = ('<Event xmlns="http://schemas.microsoft.com/win/2004/08/events/event"><System><EventID>410</EventID>'
               '<TimeCreated SystemTime="2030-01-02T09:00:00.000Z"/></System><EventData>'
               '<Data Name="DeviceInstanceId">USB\\VID_1234&amp;PID_0F01\\X1</Data></EventData></Event>')
        self.assertEqual(C.parse_events_xml(xml), [("2030-01-02 09:00:00", "410",
                                                    {"DeviceInstanceId": "USB\\VID_1234&PID_0F01\\X1"})])
        self.assertEqual(C.parse_events_xml("not xml <"), [])


class Build(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = Recording(C.demo_source())
        cls.g = R.Graph()
        cls.vids = C.build(cls.src, cls.g, R.View)

    def rows(self, vid):
        return self.g.views[vid].rows

    def test_link_keys_are_never_read(self):
        self.assertFalse([p for p in self.src.read if "BTHPORT\\Parameters\\Keys" in p])
        self.assertNotIn("MUST-NOT-BE-READ", str([v.rows for v in self.g.views.values()]))

    def test_usb_rows_and_phone_evidence(self):
        usb = {r["SerialOrInstance"]: r for r in self.rows("C_usb")}
        p = usb["EXAMPLEPHONE01"]
        self.assertEqual((p["Name"], p["VID"], p["PID"], p["Driver"]), ("Example Phone", "1234", "0F01", "WUDFWpdMtp"))
        self.assertEqual(p["PhoneEvidence"], "MTP phone/camera driver")
        self.assertEqual(p["FirstInstallLocal"], "2030-01-01 12:00:00")
        self.assertTrue(p["FirstInstallUTC"])
        self.assertTrue(p["KeyLastWriteUTC"])
        self.assertEqual(usb["EXAMPLESTICK01"]["PhoneEvidence"], "")

    def test_bluetooth_rows(self):
        bt = {r["Address"]: r for r in self.rows("C_bt")}
        p = bt["00:00:5E:00:53:01"]
        self.assertEqual((p["Name"], p["IsPhone"]), ("Example Phone", "yes"))
        self.assertTrue(p["LastConnectedUTC"] and p["LastSeenUTC"])
        h = bt["00:00:5E:00:53:02"]
        self.assertEqual((h["IsPhone"], h["LastConnectedUTC"]), ("", ""))   # 0 = never recorded
        self.assertTrue(h["LastSeenUTC"])

    def test_services_linked_to_device(self):
        names = sorted(r["Service"] for r in self.rows("C_btsvc"))
        self.assertEqual(names, ["Hands-Free Audio Gateway", "Phonebook Access (server)"])
        linked = [t for rid, t, _ in self.g.links(("bt", "00005e005301")) if rid == "CR2"]
        self.assertEqual(len(linked), 2)

    def test_same_device_by_container(self):
        sources = {r["Source"] for r in self.rows("CR1") if r["ContainerID"].endswith("0001}")}
        self.assertEqual(sources, {"usb", "wpd", "autoplay"})

    def test_install_and_event_links(self):
        dev = ("usb", "USB\\VID_1234&PID_0F01\\EXAMPLEPHONE01")
        rids = {rid for rid, _, _ in self.g.links(dev)}
        self.assertTrue({"CR1", "CR3", "CR4"} <= rids)
        ev = [r for r in self.rows("C_events") if r["Log"] == "Kernel-PnP"]
        self.assertEqual(len(ev), 1)           # the non-device PCI event is dropped
        self.assertEqual(ev[0]["MatchedDevice"], self.g.label[dev])

    def test_phone_summary(self):
        got = sorted((r["How"], r["Evidence"].split(" (")[0]) for r in self.rows("C_phones"))
        self.assertEqual(got, [("Bluetooth", "Bluetooth class of device = Phone"),
                               ("USB cable", "MTP phone/camera driver"),
                               ("USB cable (MTP event 1005)", "MTP driver customized for this phone"),
                               ("USB cable (MTP)", "same ContainerID as a USB phone")])

    def test_event_meanings_from_windows_text(self):
        ev = {(r["Log"], r["EventID"]): r for r in self.rows("C_events")}
        self.assertEqual(ev[("Kernel-PnP", "410")]["Meaning"], "device was started")
        self.assertEqual(ev[("WPD-MTPClassDriver", "1001")]["Meaning"], "device will suspend when idle")
        self.assertTrue(all(r["Meaning"] for r in self.rows("C_events")))

    def test_event_1005_names_the_phone(self):
        p = [r for r in self.rows("C_phones") if "1005" in r["How"]]
        self.assertEqual(len(p), 1)            # two events, one phone
        self.assertEqual(p[0]["Device"], "ExampleCorp, Example Phone 2, 9.9")
        self.assertEqual((p[0]["FirstSeenUTC"], p[0]["LastConnectedUTC"]), ("2030-01-03 10:00:00", "2030-01-04 10:00:00"))
        links = [t for rid, t, _ in self.g.links(p[0]["_e"]) if rid == "CR4"]
        self.assertEqual(len(links), 2)

    def test_bluetooth_le(self):
        le = self.rows("C_btle")
        self.assertEqual([(r["Kind"], r["Name"], r["Address"]) for r in le],
                         [("LE device", "Example Watch", "00:00:5E:00:53:03")])
        self.assertIn(le[0]["_e"], [t for rid, t, _ in self.g.links(("bt", "00005e005303")) if rid == "CR2"])

    def test_cached_source_reads_once(self):
        src = Recording(C.demo_source())
        cached = C.CachedSource(src)
        C.build(cached, R.Graph(), R.View)
        n = len(src.read)
        C.build(cached, R.Graph(), R.View)
        self.assertEqual(len(src.read), n)


FT = 133500000000000000


def keys_of(*items):
    """{(hive, path): {...}} from (path, values, subkeys) tuples; every key under HKLM."""
    return {(C.HKLM, p): {"values": v or {}, "subkeys": list(sub), "lw": FT} for p, v, sub in items}


def build_from(src):
    g = R.Graph()
    C.build(src, g, R.View)
    return g


def phones_of(g):
    return g.views["C_phones"].rows


class Composite(unittest.TestCase):
    CID = "{e5e5e5e5-0000-4000-a000-000000000002}"

    def usb(self, parent_extra=None, parent_cid=None):
        p = {"Service": "usbccgp", "DeviceDesc": "USB Composite Device", "ContainerID": parent_cid or self.CID}
        p.update(parent_extra or {})
        return keys_of(
            (C.USB, None, ["VID_1234&PID_0F03", "VID_1234&PID_0F03&MI_00"]),
            (C.USB + r"\VID_1234&PID_0F03", None, ["R58N12345AB"]),
            (C.USB + r"\VID_1234&PID_0F03\R58N12345AB", p, []),
            (C.USB + r"\VID_1234&PID_0F03&MI_00", None, ["7&2a3b4c5d&0&0000"]),
            (C.USB + r"\VID_1234&PID_0F03&MI_00\7&2a3b4c5d&0&0000",
             {"Service": "WUDFWpdMtp", "DeviceDesc": "Example MTP", "ContainerID": self.CID}, []))

    def check(self, g):
        usb = {r["SerialOrInstance"]: r for r in g.views["C_usb"].rows}
        self.assertIn("usbccgp", usb["R58N12345AB"]["PhoneEvidence"])
        rows = phones_of(g)
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["Serial"] for r in rows], ["R58N12345AB", "R58N12345AB"])   # parent, and the MTP interface

    def test_parent_found_by_parent_id_prefix(self):
        keys = self.usb({"ParentIdPrefix": "7&2a3b4c5d&0"}, parent_cid="{e5e5e5e5-0000-4000-a000-000000000003}")
        self.check(build_from(C.FakeSource(keys)))

    def test_parent_found_by_container_without_prefix(self):
        g = build_from(C.FakeSource(self.usb()))
        self.check(g)
        self.assertEqual({r["PhysicalDevice"] for r in phones_of(g)}, {self.CID})

    def test_an_unrelated_composite_device_is_not_marked(self):
        keys = self.usb({"ParentIdPrefix": "7&ffffffff&0"}, parent_cid="{e5e5e5e5-0000-4000-a000-000000000003}")
        usb = {r["SerialOrInstance"]: r for r in build_from(C.FakeSource(keys)).views["C_usb"].rows}
        self.assertEqual(usb["R58N12345AB"]["PhoneEvidence"], "")


class SmallFixes(unittest.TestCase):
    def test_apple_usb_driver_marks_a_phone(self):
        keys = keys_of((C.USB, None, ["VID_1234&PID_0F04"]), (C.USB + r"\VID_1234&PID_0F04", None, ["0000EXAMPLE"]),
                       (C.USB + r"\VID_1234&PID_0F04\0000EXAMPLE", {"Service": "usbaapl64"}, []))
        self.assertEqual(len(phones_of(build_from(C.FakeSource(keys)))), 1)

    def test_bthledevice_installs_and_events_are_listed(self):
        dev = "BTHLEDEVICE\\{0000111f-0000-1000-8000-00805f9b34fb}_Dev_VID&001234_PID&0F07_REV&0006_00005e005309\\7&1&0&0000"
        text = f">>>  [Device Install (Hardware initiated) - {dev}]\n>>>  Section start 2030/01/01 12:00:00.000\n"
        events = {C.CHANNELS[0]: [("2030-01-02 09:00:00", "400", {"DeviceInstanceId": dev})]}
        g = build_from(C.FakeSource({}, text, events))
        self.assertEqual([r["DeviceID"] for r in g.views["C_install"].rows], [dev.upper()])
        self.assertEqual(len(g.views["C_events"].rows), 1)

    def test_bthledevice_address_is_the_12_digits_after_the_last_underscore(self):
        sub = "{0000111f-0000-1000-8000-00805f9b34fb}_Dev_VID&001234_PID&0f07_REV&0006_00005e005309"
        keys = keys_of((C.BTLE_SVC, None, [sub]), (C.BTLE_SVC + "\\" + sub, None, ["8&2f1e3d4c&0&0000"]),
                       (C.BTLE_SVC + "\\" + sub + r"\8&2f1e3d4c&0&0000", {"FriendlyName": "Example Band"}, []))
        rows = build_from(C.FakeSource(keys)).views["C_btle"].rows
        self.assertEqual([r["Address"] for r in rows], [C.mac("00005e005309")])
        self.assertEqual(rows[0]["Address"], "00:00:5E:00:53:09")

    def test_dates_before_1970_do_not_raise(self):
        self.assertEqual(C.local_to_utc("1969-12-31 23:00:00"), "")
        self.assertEqual(C.local_to_utc("0001-01-01 00:00:00"), "")
        text = ">>>  [Device Install (Hardware initiated) - USB\\VID_1&PID_2\\X]\n>>>  Section start 1960/01/01 00:00:00.000\n"
        g = build_from(C.FakeSource({}, text))
        self.assertEqual(g.views["C_install"].rows[0]["FirstInstallLocal"], "1960-01-01 00:00:00")

    def test_log_decoding(self):
        text = ">>>  [Device Install (Hardware initiated) - USB\\VID_1&PID_2\\X]"
        self.assertEqual(C.decode_log(text.encode("utf-16")), text)            # BOM written by .encode("utf-16")
        self.assertEqual(C.decode_log(b"\xef\xbb\xbf" + text.encode("utf-8")), text)
        self.assertEqual(C.decode_log("caf\u00e9".encode("utf-8")), "caf\u00e9")
        self.assertEqual(C.decode_log("caf\u00e9".encode("cp1252")), "caf\u00e9")   # not UTF-8: cp1252 fallback

    def test_live_text_decodes_utf16_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "setupapi.dev.log")
            with open(p, "wb") as f:
                f.write("hello\r\nworld".encode("utf-16"))
            fake = types.SimpleNamespace(denied=[])
            self.assertEqual(C.LiveSource.text(fake, p), "hello\r\nworld")   # unbound call: never touches this PC
            self.assertEqual(fake.denied, [])


class NullContainer(unittest.TestCase):
    NULL = "{00000000-0000-0000-%s-%s}" % ("ffff", "f" * 12)   # the documented value, built from parts

    def keys(self):
        return keys_of(
            (C.USB, None, ["VID_1234&PID_0F05", "VID_1234&PID_0F06"]),
            (C.USB + r"\VID_1234&PID_0F05", None, ["AAA"]),
            (C.USB + r"\VID_1234&PID_0F05\AAA", {"Service": "WUDFWpdMtp", "ContainerID": self.NULL}, []),
            (C.USB + r"\VID_1234&PID_0F06", None, ["BBB"]),
            (C.USB + r"\VID_1234&PID_0F06\BBB", {"Service": "USBSTOR", "ContainerID": self.NULL}, []),
            (C.WPD, None, ["ROOT_ENUM"]),
            (C.WPD + r"\ROOT_ENUM", {"FriendlyName": "Some volume", "ContainerID": self.NULL}, []))

    def test_null_container_never_joins_or_flags_a_phone(self):
        g = build_from(C.FakeSource(self.keys()))
        self.assertEqual(g.views["CR1"].rows, [])
        self.assertEqual([r["How"] for r in phones_of(g)], ["USB cable"])           # the portable device is not a phone
        self.assertEqual(phones_of(g)[0]["PhysicalDevice"], "USB\\VID_1234&PID_0F05\\AAA")   # falls back to the device id
        self.assertEqual(g.views["C_usb"].rows[0]["ContainerID"], self.NULL)         # still shown as stored


class PhysicalDevice(unittest.TestCase):
    def test_grouping_value_per_source(self):
        g = build_from(C.demo_source())
        by_how = {r["How"]: r for r in phones_of(g)}
        self.assertEqual(by_how["USB cable"]["PhysicalDevice"], "{e5e5e5e5-0000-4000-a000-000000000001}")
        self.assertEqual(by_how["USB cable (MTP)"]["PhysicalDevice"], by_how["USB cable"]["PhysicalDevice"])
        self.assertEqual(by_how["Bluetooth"]["PhysicalDevice"], "00:00:5E:00:53:01")

    def test_event_1005_rows_are_not_one_physical_device(self):
        r = next(r for r in phones_of(build_from(C.demo_source())) if "1005" in r["How"])
        self.assertEqual(r["PhysicalDevice"], "")
        self.assertIn("same make/model/OS \u2013 may be more than one phone", r["Evidence"])


class Failures(unittest.TestCase):
    def run_events(self, run):
        fake = types.SimpleNamespace(denied=[])
        with mock.patch.object(C.subprocess, "run", run):
            out = C.LiveSource.events(fake, "Example/Channel")       # unbound call: nothing reads this PC
        return out, fake.denied

    def completed(self, rc=0, out="", err=""):
        return lambda *a, **k: types.SimpleNamespace(returncode=rc, stdout=out, stderr=err)

    def test_failed_wevtutil_and_missing_channel_are_reported(self):
        out, denied = self.run_events(self.completed(15007, "", "The specified channel could not be found.\n"))
        self.assertEqual(out, [])
        self.assertEqual(len(denied), 1)
        self.assertIn("Example/Channel", denied[0])
        self.assertIn("could not be found", denied[0])

    def test_wevtutil_missing_or_timed_out(self):
        def boom(*a, **k):
            raise FileNotFoundError("wevtutil")

        def slow(*a, **k):
            raise C.subprocess.TimeoutExpired("wevtutil", 60)

        for run in (boom, slow):
            out, denied = self.run_events(run)
            self.assertEqual((out, len(denied)), ([], 1))

    def test_xml_parse_error_is_reported(self):
        out, denied = self.run_events(self.completed(0, "<Event><broken"))
        self.assertEqual(out, [])
        self.assertIn("XML", denied[0])

    def test_event_count_is_the_cap(self):
        seen = []

        def run(cmd, **k):
            seen.append(cmd)
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")

        self.run_events(run)
        self.assertEqual(C.EVENT_CAP, 10000)
        self.assertIn("/c:10000", seen[0])

    def test_unreadable_setupapi_log_is_reported(self):
        fake = types.SimpleNamespace(denied=[])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(C.LiveSource.text(fake, os.path.join(tmp, "missing.log")), "")
        self.assertEqual(len(fake.denied), 1)
        self.assertIn("missing.log", fake.denied[0])

    def test_failures_show_up_in_the_denied_view_and_cap_in_basis(self):
        src = C.FakeSource({}, "", {}, failures={C.CHANNELS[1]: "channel not found"})
        g = build_from(src)
        self.assertEqual([r["Location"] for r in g.views["C_denied"].rows],
                         [f"event log {C.CHANNELS[1]}: channel not found"])
        self.assertIn("10,000", g.views["C_events"].basis)

    def test_a_source_that_raises_does_not_sink_the_build(self):
        class Broken(C.FakeSource):
            def subkeys(self, hive, path):
                if path == C.BTPORT:
                    raise RuntimeError("registry went away")
                return super().subkeys(hive, path)

        base = C.demo_source()
        g = build_from(Broken(base.keys, base._text, base._events))
        self.assertTrue(g.views["C_usb"].rows)
        self.assertEqual(g.views["C_bt"].rows, [])
        self.assertTrue(any("registry went away" in r["Location"] for r in g.views["C_denied"].rows))


if __name__ == "__main__":
    unittest.main()
