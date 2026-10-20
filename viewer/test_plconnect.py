"""Tests for plconnect.py - phones connected by USB cable or Bluetooth. Synthetic registry only.

    python test_plconnect.py
"""
import os
import sys
import unittest

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
        ev = self.rows("C_events")
        self.assertEqual(len(ev), 1)           # the non-device PCI event is dropped
        self.assertEqual(ev[0]["MatchedDevice"], self.g.label[dev])

    def test_phone_summary(self):
        got = sorted((r["How"], r["Evidence"]) for r in self.rows("C_phones"))
        self.assertEqual(got, [("Bluetooth", "Bluetooth class of device = Phone"),
                               ("USB cable", "MTP phone/camera driver"),
                               ("USB cable (MTP)", "same ContainerID as a USB phone")])

    def test_cached_source_reads_once(self):
        src = Recording(C.demo_source())
        cached = C.CachedSource(src)
        C.build(cached, R.Graph(), R.View)
        n = len(src.read)
        C.build(cached, R.Graph(), R.View)
        self.assertEqual(len(src.read), n)


if __name__ == "__main__":
    unittest.main()
